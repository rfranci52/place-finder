"""Shared fakes. Apart from test_schema_pg.py, which needs a local PostGIS, nothing in the suite
touches a network or a database: the geocoder, the database and the clock are replaced in memory."""
import email.message
import importlib.util
import io
import json
import math
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
RELEASE = "2026-10-21.0"


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
    """Monotonic seconds and wall-clock time that move only when told to."""

    def __init__(self):
        self.t = 1000.0
        self.wall = datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)

    def monotonic(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds
        self.wall += timedelta(seconds=seconds)


# ------------------------------------------------------------------ the database


def place(n, name, category, lat, lng, **extra):
    row = {"id": f"00000000-0000-4000-8000-{n:012d}", "name": name, "category": category,
           "street": f"{n} Test St", "locality": "New York", "postcode": "10012", "phone": None,
           "website": None, "borough": "Manhattan", "lat": lat, "lng": lng}
    row.update(extra)
    return row


# Around Washington Square Park, which the fake neighborhood "greenwich village" points at.
PLACES = [
    place(1, "Cafe Uno", "coffee_shop", 40.7310, -73.9970, phone="+12125550100", website="https://cafe-uno.example"),
    place(2, "Bean Bar", "cafe", 40.7320, -73.9980),
    place(3, "Joe's Pizza", "pizza_restaurant", 40.7305, -74.0020),
    place(4, "Thai Garden", "thai_restaurant", 40.7290, -73.9990),
    place(5, "Village Pharmacy", "pharmacy", 40.7330, -73.9960),
    place(6, "Far Away Cafe", "coffee_shop", 40.7600, -73.9800),  # about 3.5 km away
]
NEIGHBORHOODS = {
    ("greenwich village", 1): ("Greenwich Village", "Manhattan", 40.7307, -73.9957),
    ("williamsburg", 3): ("Williamsburg", "Brooklyn", 40.7153, -73.9599),
    ("chelsea", 1): ("Chelsea", "Manhattan", 40.7501, -74.0021),
    ("chelsea", 5): ("Chelsea", "Staten Island", 40.5940, -74.1890),
    ("brooklyn", 3): ("Brooklyn", "Brooklyn", 40.6484, -73.9528),
}


def distance_m(lat1, lng1, lat2, lng2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(a))


class FakeStore:
    """The database calls, with the same rules as db/schema.sql and the search SQL, in memory.

    test_schema_pg.py runs the same behaviors against the real SQL, so the two stay honest.
    """

    def __init__(self, clock, search_mod):
        self.clock = clock
        self.mod = search_mod
        self.geocodes = {}
        self.counts = {}
        self.places = [dict(p) for p in PLACES]
        self.neighborhoods = dict(NEIGHBORHOODS)
        self.release = RELEASE
        self.calls = []
        self.fail_on = set()  # method names that raise, to simulate a database error

    def _call(self, name, *args):
        self.calls.append((name, args))
        if name in self.fail_on:
            raise RuntimeError(f"simulated database failure in {name}")

    @property
    def today(self):
        return self.clock.wall.date()

    def count(self, key):
        return self.counts.get((self.today, key), 0)

    def take_slot(self, kind, visitor, per_visitor, per_day):
        self._call("take_slot", kind, visitor, per_visitor, per_day)
        assert kind in ("search", "geocode")
        vkey, skey = (self.today, f"{kind}:v:{visitor}"), (self.today, f"{kind}:site")
        if per_visitor <= 0 or self.counts.get(vkey, 0) >= per_visitor:
            return "visitor_limit"
        if per_day <= 0 or self.counts.get(skey, 0) >= per_day:
            return "site_limit"
        self.counts[vkey] = self.counts.get(vkey, 0) + 1
        self.counts[skey] = self.counts.get(skey, 0) + 1
        return "ok"

    def neighborhood(self, alias, borocode):
        self._call("neighborhood", alias, borocode)
        matches = sorted((code, row) for (a, code), row in self.neighborhoods.items()
                         if a == alias and (borocode is None or code == borocode))
        return matches[0][1] if matches else None

    def geocode_get(self, query):
        self._call("geocode_get", query)
        hit = self.geocodes.get(query)
        if hit and self.clock.wall - hit[1] < timedelta(days=30):
            return hit[0]
        return None

    def geocode_put(self, query, geo):
        self._call("geocode_put", query, geo)
        self.geocodes[query] = (geo, self.clock.wall)

    def area_of(self, lat, lng):
        self._call("area_of", lat, lng)
        inside = 40.49 <= lat <= 40.92 and -74.26 <= lng <= -73.70 and not (lng < -74.03 and lat > 40.65)
        return ("Manhattan" if inside else None), self.release

    def search_places(self, term, lat, lng, radius, limit):
        self._call("search_places", term, lat, lng, radius, limit)
        sql, params = self.mod.search_params(term, lat, lng, radius, limit)  # the real parameter builder
        if term.kind == "category":
            match = lambda p: p["category"] in params["categories"]  # noqa: E731
        else:
            slug = params["slug"]
            match = lambda p: (term.key in p["name"].lower() or p["category"] == slug  # noqa: E731
                               or p["category"].startswith(slug + "_") or p["category"].endswith("_" + slug))
        rows = []
        for p in self.places:
            d = distance_m(lat, lng, p["lat"], p["lng"])
            if d <= radius and match(p):
                rows.append((d, (p["id"], p["name"], p["category"], p["street"], p["locality"], p["postcode"],
                                 p["phone"], p["website"], p["borough"], p["lat"], p["lng"], d)))
        rows.sort(key=lambda r: r[0])
        return [r for _, r in rows[:limit]]


# ------------------------------------------------------------------ the geocoder


@dataclass
class Call:
    url: str
    timeout: float
    at: float

    @property
    def params(self):
        return {k: v[0] for k, v in parse_qs(urlsplit(self.url).query).items()}


def geosearch_body(label="350 5 AVENUE, New York, NY, USA", lat=40.748441, lng=-73.985656, confidence=0.8):
    return json.dumps({"type": "FeatureCollection", "features": [{
        "type": "Feature", "geometry": {"type": "Point", "coordinates": [lng, lat]},
        "properties": {"label": label, "confidence": confidence, "layer": "venue", "match_type": "fallback"},
    }]}).encode()


@dataclass
class FakeGeoSearch:
    """Scripted (status, body) replies; by default an exact echo of the query at Midtown."""

    clock: FakeClock
    reply_type: type
    scripts: list = field(default_factory=list)
    calls: list = field(default_factory=list)

    def script(self, *replies):
        self.scripts.extend(replies)

    def __call__(self, url, *, timeout):
        self.calls.append(Call(url, timeout, self.clock.monotonic()))
        if self.scripts:
            status, body = self.scripts.pop(0)
        else:
            text = parse_qs(urlsplit(url).query)["text"][0]
            status, body = 200, geosearch_body(label=f"{text.upper()}, New York, NY, USA")
        return self.reply_type(status, body)

    def count(self):
        return len(self.calls)


# ------------------------------------------------------------------ wiring


@dataclass
class World:
    mod: object
    clock: FakeClock
    store: FakeStore
    geosearch: FakeGeoSearch
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
    store = FakeStore(clock, search_mod)
    geosearch = FakeGeoSearch(clock, search_mod.Reply)

    @contextmanager
    def fake_open_store(url):
        if url == "unreachable":
            raise OSError("connection refused")
        yield store

    monkeypatch.setattr(search_mod, "open_store", fake_open_store)
    monkeypatch.setattr(search_mod, "http_request", geosearch)
    monkeypatch.setattr(search_mod, "monotonic", clock.monotonic)
    for key in list(__import__("os").environ):
        if key.startswith("PLACES_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("PLACES_DB_URL", "postgresql://fake")
    monkeypatch.setenv("PLACES_HASH_SALT", SALT)
    monkeypatch.setenv("PLACES_ALLOWED_ORIGINS", ORIGIN)
    return World(search_mod, clock, store, geosearch, monkeypatch)


NEAR_THE_PARK = {"what": "Coffee", "where": "Greenwich Village"}
ADDRESS = {"what": "Coffee", "where": "350 5th Ave"}
