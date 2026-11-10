"""Shared fakes. Apart from test_schema_pg.py, which needs a local Postgres, nothing in the
suite touches a network or a database: the upstream services, the database and the clock
are all replaced in memory."""
import email.message
import importlib.util
import io
import json
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
SEARCH_PY = ROOT / "frontend" / "api" / "search.py"
ORIGIN = "https://places.example.test"
SALT = "test-salt"


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def search_mod():
    return load_module(SEARCH_PY, "place_search_for_tests")


# ------------------------------------------------------------------ a clock


class FakeClock:
    """Monotonic seconds and wall-clock time that move only when told to (or when code sleeps)."""

    def __init__(self):
        self.t = 1000.0
        self.wall = datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)
        self.sleeps = []

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.advance(seconds)

    def advance(self, seconds):
        self.t += seconds
        self.wall += timedelta(seconds=seconds)


# ------------------------------------------------------------------ the database


class FakeStore:
    """The eight database functions, with the same rules as db/schema.sql, held in memory.

    test_schema_pg.py runs the same scenarios against the real SQL, so the two stay honest.
    """

    INTERVALS = {"nominatim": 1.1, "overpass": 1.0, "overpass-mirror": 1.0}

    def __init__(self, clock):
        self.clock = clock
        self.geocodes = {}
        self.results = {}
        self.counts = {}
        self.next_at = {name: float("-inf") for name in self.INTERVALS}
        self.calls = []
        self.fail_on = set()  # method names that raise, to simulate a database error

    def _call(self, name, *args):
        self.calls.append((name, args))
        if name in self.fail_on:
            raise RuntimeError(f"simulated database failure in {name}")

    @property
    def today(self):
        return self.clock.wall.date()

    def geocode_get(self, query):
        self._call("geocode_get", query)
        hit = self.geocodes.get(query)
        if hit and self.clock.wall - hit[1] < timedelta(days=30):
            return hit[0]
        return None

    def geocode_put(self, query, geo):
        self._call("geocode_put", query, geo)
        self.geocodes[query] = (geo, self.clock.wall)

    def results_get(self, term_key, lat_e3, lng_e3, radius):
        self._call("results_get", term_key, lat_e3, lng_e3, radius)
        hit = self.results.get((term_key, lat_e3, lng_e3, radius))
        if hit and self.clock.wall - hit[2] < timedelta(hours=24):
            return hit
        return None

    def results_put(self, term_key, lat_e3, lng_e3, radius, places, truncated):
        self._call("results_put", term_key, lat_e3, lng_e3, radius, places, truncated)
        assert len(places) <= 60
        json.dumps(places)  # must be storable as jsonb
        self.results[(term_key, lat_e3, lng_e3, radius)] = (places, truncated, self.clock.wall)
        return self.clock.wall

    def count(self, key):
        return self.counts.get((self.today, key), 0)

    def begin_live_search(self, visitor, per_visitor, upstream_limit):
        self._call("begin_live_search", visitor, per_visitor, upstream_limit)
        vkey = (self.today, "v:" + visitor)
        gkey = (self.today, "upstream")
        if per_visitor <= 0 or self.counts.get(vkey, 0) >= per_visitor:
            return "visitor_limit"
        if upstream_limit <= 0 or self.counts.get(gkey, 0) >= upstream_limit:
            return "global_limit"
        self.counts[vkey] = self.counts.get(vkey, 0) + 1
        self.counts[gkey] = self.counts.get(gkey, 0) + 1
        return "ok"

    def take_upstream_slot(self, upstream_limit):
        self._call("take_upstream_slot", upstream_limit)
        gkey = (self.today, "upstream")
        if self.counts.get(gkey, 0) >= upstream_limit:
            return False
        self.counts[gkey] = self.counts.get(gkey, 0) + 1
        return True

    def reserve_call(self, upstream, max_wait_ms):
        self._call("reserve_call", upstream, max_wait_ms)
        now = self.clock.monotonic()
        start = max(self.next_at[upstream], now)
        wait_ms = round((start - now) * 1000)
        if wait_ms > max_wait_ms:
            return None
        self.next_at[upstream] = start + self.INTERVALS[upstream]
        return wait_ms

    def pause_upstream(self, upstream, seconds):
        self._call("pause_upstream", upstream, seconds)
        self.next_at[upstream] = max(self.next_at[upstream], self.clock.monotonic() + min(max(seconds, 0), 300))


# ------------------------------------------------------------------ the upstream services


@dataclass
class Call:
    service: str
    url: str
    user_agent: str
    timeout: float
    form: dict | None
    at: float

    @property
    def params(self):
        return {k: v[0] for k, v in parse_qs(urlsplit(self.url).query).items()}

    @property
    def query(self):
        return (self.form or {}).get("data", "")


def nominatim_body(lat=40.7308, lng=-73.9973, name="Washington Square Park, Manhattan, New York County, New York"):
    return json.dumps([{"lat": str(lat), "lon": str(lng), "display_name": name}]).encode()


def overpass_body(elements, remark=None):
    data = {"version": 0.6, "elements": elements}
    if remark:
        data["remark"] = remark
    return json.dumps(data).encode()


def node(osm_id, lat, lng, **tags):
    return {"type": "node", "id": osm_id, "lat": lat, "lon": lng, "tags": tags}


def way(osm_id, lat, lng, **tags):
    return {"type": "way", "id": osm_id, "center": {"lat": lat, "lon": lng}, "nodes": [1, 2, 3], "tags": tags}


DEFAULT_ELEMENTS = [
    node(1, 40.7310, -73.9970, name="Cafe Uno", amenity="cafe", opening_hours="Mo-Fr 07:00-18:00",
         website="https://cafe-uno.example", phone="+1 212 555 0100"),
    way(2, 40.7320, -73.9980, name="Bean Shop", shop="coffee", **{"addr:housenumber": "12", "addr:street": "Main St"}),
]


@dataclass
class FakeUpstream:
    """Scripted (status, body) replies per service, with a default answer for each."""

    clock: FakeClock
    reply_type: type
    scripts: dict = field(default_factory=dict)
    calls: list = field(default_factory=list)

    def service_for(self, url):
        if "nominatim" in url:
            return "nominatim"
        if "private.coffee" in url or "mirror" in url:
            return "overpass-mirror"
        return "overpass"

    def script(self, service, *replies):
        self.scripts.setdefault(service, []).extend(replies)

    def __call__(self, url, *, user_agent, timeout, form=None):
        service = self.service_for(url)
        self.calls.append(Call(service, url, user_agent, timeout, form, self.clock.monotonic()))
        queue = self.scripts.get(service)
        if queue:
            status, body = queue.pop(0)
        elif service == "nominatim":
            status, body = 200, nominatim_body()
        else:
            status, body = 200, overpass_body(DEFAULT_ELEMENTS)
        return self.reply_type(status, body)

    def count(self, service=None):
        return sum(1 for c in self.calls if service is None or c.service == service)


# ------------------------------------------------------------------ wiring


@dataclass
class World:
    mod: object
    clock: FakeClock
    store: FakeStore
    upstream: FakeUpstream
    monkeypatch: object

    def env(self, **values):
        for key, value in values.items():
            if value is None:
                self.monkeypatch.delenv(key, raising=False)
            else:
                self.monkeypatch.setenv(key, str(value))

    def post(self, body, *, origin=ORIGIN, ip="203.0.113.7", content_type="application/json", raw=None, headers=None):
        payload = raw if raw is not None else json.dumps(body).encode()
        all_headers = {"Content-Type": content_type, "Content-Length": str(len(payload)), "X-Forwarded-For": ip}
        if origin is not None:
            all_headers["Origin"] = origin
        all_headers.update(headers or {})
        status, response_headers, data = run_handler(self.mod.handler, "POST", all_headers, payload)
        return Response(status, response_headers, json.loads(data) if data else None)


@dataclass
class Response:
    status: int
    headers: dict
    json: dict | None

    @property
    def code(self):
        return (self.json or {}).get("error", {}).get("code")


def run_handler(handler_class, method, headers, body):
    """Run a BaseHTTPRequestHandler in memory. Returns (status, headers, body bytes)."""

    class Bridge(handler_class):
        def __init__(self):
            self.rfile = io.BytesIO(body)
            self.wfile = io.BytesIO()
            self.headers = email.message.Message()
            for key, value in headers.items():
                self.headers[key] = value
            self.command = method
            self.path = "/api/search"
            self.request_version = "HTTP/1.1"
            self.requestline = f"{method} /api/search HTTP/1.1"
            self.client_address = ("127.0.0.1", 0)

        def log_message(self, *args):
            pass

    bridge = Bridge()
    getattr(bridge, f"do_{method}")()
    head, _, payload = bridge.wfile.getvalue().partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split()[1])
    response_headers = {name.strip(): value.strip() for name, value in (line.split(":", 1) for line in lines[1:] if ":" in line)}
    return status, response_headers, payload


@pytest.fixture
def world(search_mod, monkeypatch):
    clock = FakeClock()
    store = FakeStore(clock)
    upstream = FakeUpstream(clock, search_mod.Reply)

    @contextmanager
    def fake_open_store(url):
        if url == "unreachable":
            raise OSError("connection refused")
        yield store

    monkeypatch.setattr(search_mod, "open_store", fake_open_store)
    monkeypatch.setattr(search_mod, "http_request", upstream)
    monkeypatch.setattr(search_mod, "sleep", clock.sleep)
    monkeypatch.setattr(search_mod, "monotonic", clock.monotonic)
    for key in list(__import__("os").environ):
        if key.startswith("PLACES_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("PLACES_DB_URL", "postgresql://fake")
    monkeypatch.setenv("PLACES_HASH_SALT", SALT)
    monkeypatch.setenv("PLACES_ALLOWED_ORIGINS", ORIGIN)
    return World(search_mod, clock, store, upstream, monkeypatch)


COFFEE_NEAR_PARK = {"what": "Coffee", "where": "Washington Square Park, New York"}
