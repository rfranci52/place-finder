"""db/schema.sql against a real, throwaway Postgres, through the same Store class the function uses.

Skipped unless PLACES_TEST_ADMIN_DSN points at a superuser on a local cluster, for example

    PLACES_TEST_ADMIN_DSN=postgresql://postgres:PASSWORD@127.0.0.1:54329/postgres

Each run creates a fresh database with a random name, so nothing is shared between runs. The
DSN must point at localhost, and the cluster should be a throwaway one: this suite creates
databases, and it sets the places_app password, which (like every Postgres role) is shared by
every database in the cluster, so a development database on the same cluster loses its login.
"""
import json
import os
import secrets
import threading
import time
from contextlib import contextmanager
from urllib.parse import quote, urlsplit, urlunsplit

import pytest

from conftest import ORIGIN, ROOT, FakeClock, FakeUpstream, load_module, run_handler

ADMIN_DSN = os.environ.get("PLACES_TEST_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not ADMIN_DSN, reason="PLACES_TEST_ADMIN_DSN is not set")

psycopg = pytest.importorskip("psycopg")


def _with_db(dsn, dbname, user=None, password=None):
    parts = urlsplit(dsn)
    netloc = parts.netloc
    if user is not None:
        netloc = f"{quote(user, safe='')}:{quote(password, safe='')}@{parts.hostname}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, "/" + dbname, parts.query, ""))


@pytest.fixture(scope="module")
def pg():
    host = urlsplit(ADMIN_DSN).hostname
    assert host in ("127.0.0.1", "localhost", "::1"), "refusing to run against a non-local database"
    dbname = "places_test_" + secrets.token_hex(4)
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(f'create database "{dbname}"')
    owner_dsn = _with_db(ADMIN_DSN, dbname)
    schema = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    password = secrets.token_urlsafe(16)
    with psycopg.connect(owner_dsn, autocommit=True) as owner:
        for _ in range(2):  # the file must be safe to apply repeatedly
            with owner.transaction():
                owner.execute(schema)
        owner.execute(psycopg.sql.SQL("alter role places_app login password {}").format(psycopg.sql.Literal(password)))
    app_dsn = _with_db(ADMIN_DSN, dbname, "places_app", password)
    yield {"owner": owner_dsn, "app": app_dsn, "dbname": dbname}


@pytest.fixture
def owner(pg):
    with psycopg.connect(pg["owner"], autocommit=True) as conn:
        conn.execute("truncate places.geocode_cache, places.results_cache, places.daily_counts")
        conn.execute("update places.upstreams set next_at = 'epoch'")
        yield conn


@pytest.fixture
def store(pg, owner, search_mod):
    with search_mod.open_store(pg["app"]) as s:
        yield s


@contextmanager
def app_conn(pg):
    with psycopg.connect(pg["app"], autocommit=True, prepare_threshold=None) as conn:
        yield conn


# ------------------------------------------------------------------ the role


@pytest.mark.parametrize("statement", [
    "select * from places.geocode_cache",
    "select * from places.daily_counts",
    "insert into places.daily_counts (day, key, n) values (current_date, 'upstream', 0)",
    "update places.upstreams set next_at = now()",
    "select places.slot_for('x', 10)",
    "create table places.mine (x int)",
    "create table public.mine (x int)",
])
def test_the_app_role_cannot_touch_tables_or_internals(pg, owner, statement):
    with app_conn(pg) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(statement)


def test_the_app_role_has_short_timeouts(pg):
    with app_conn(pg) as conn:
        assert conn.execute("show statement_timeout").fetchone()[0] == "5s"
        assert conn.execute("show lock_timeout").fetchone()[0] == "2s"


def test_setup_script_grant_check_passes(pg, owner):
    setup = load_module(ROOT / "scripts" / "setup_db.py", "place_setup_for_pg_tests")
    assert setup.verify(owner) == []


def test_grant_check_notices_a_stray_grant(pg, owner):
    setup = load_module(ROOT / "scripts" / "setup_db.py", "place_setup_for_pg_tests_2")
    owner.execute("grant select on places.daily_counts to places_app")
    try:
        assert setup.verify(owner) == ["places_app: unexpected select on places.daily_counts"]
    finally:
        owner.execute("revoke select on places.daily_counts from places_app")


# ------------------------------------------------------------------ caches


def test_geocode_round_trip_and_30_day_lifetime(store, owner, search_mod):
    geo = search_mod.Geocode(True, 40.7308, -73.9973, "Washington Square Park, Manhattan")
    store.geocode_put("washington square park", geo)
    assert store.geocode_get("washington square park") == geo
    assert store.geocode_get("washington square park ") is None
    owner.execute("update places.geocode_cache set fetched_at = now() - interval '29 days'")
    assert store.geocode_get("washington square park") == geo
    owner.execute("update places.geocode_cache set fetched_at = now() - interval '31 days'")
    assert store.geocode_get("washington square park") is None


def test_not_found_is_stored(store, search_mod):
    store.geocode_put("nowhere at all", search_mod.Geocode(False))
    assert store.geocode_get("nowhere at all") == search_mod.Geocode(False, None, None, None)


def test_results_round_trip_and_24_hour_lifetime(store, owner):
    places = [{"id": "node/1", "name": "Cafe é \"quoted\"", "lat": 40.7, "lng": -73.9}]
    fetched = store.results_put("c:coffee", 40731, -73997, 1500, places, True)
    got = store.results_get("c:coffee", 40731, -73997, 1500)
    assert got == (places, True, fetched)
    assert store.results_get("c:coffee", 40731, -73997, 1000) is None
    assert store.results_get("c:pizza", 40731, -73997, 1500) is None
    owner.execute("update places.results_cache set fetched_at = now() - interval '25 hours'")
    assert store.results_get("c:coffee", 40731, -73997, 1500) is None


@pytest.mark.parametrize("args", [
    ("c:coffee", 40731, -73997, 1500, [{}] * 61, False),  # more than 60 places
    ("c:coffee", 40731, -73997, 2000, [], False),  # a radius the app does not offer
    ("c:coffee", 99999, -73997, 1500, [], False),  # latitude out of range
    ("x", 1, 1, 1500, [], False),  # term key too short to be one of ours
])
def test_cache_rows_are_checked_by_the_database(store, args):
    with pytest.raises(psycopg.errors.CheckViolation):
        store.results_put(*args)


def test_keys_sharing_a_slot_cost_a_miss_not_a_wrong_answer(store, owner, search_mod):
    a, b = owner.execute(
        "select min(q), max(q) from (select 'place ' || g as q, places.slot_for('place ' || g, 16384) as s "
        "from generate_series(1, 40000) g) t group by s having count(*) > 1 limit 1"
    ).fetchone()
    store.geocode_put(a, search_mod.Geocode(True, 1.0, 1.0, "A"))
    store.geocode_put(b, search_mod.Geocode(True, 2.0, 2.0, "B"))
    assert store.geocode_get(a) is None
    assert store.geocode_get(b).label == "B"
    assert owner.execute("select count(*) from places.geocode_cache").fetchone()[0] == 1


def test_cache_size_is_bounded(store, owner, search_mod):
    for n in range(300):
        store.geocode_put(f"q{n}", search_mod.Geocode(False))
    rows, top = owner.execute("select count(*), max(slot) from places.geocode_cache").fetchone()
    assert rows <= 300 and top < 16384


# ------------------------------------------------------------------ limits


def counts(owner):
    return dict(owner.execute(
        "select key, n from places.daily_counts where day = (now() at time zone 'utc')::date").fetchall())


def test_visitor_first_then_site_with_a_refund(store, owner):
    assert store.begin_live_search("aaa", 2, 3) == "ok"
    assert store.begin_live_search("aaa", 2, 3) == "ok"
    assert store.begin_live_search("aaa", 2, 3) == "visitor_limit"
    assert counts(owner) == {"v:aaa": 2, "upstream": 2}  # the refused visitor spent nothing
    assert store.take_upstream_slot(3) is True
    assert store.take_upstream_slot(3) is False
    assert store.begin_live_search("bbb", 2, 3) == "global_limit"
    assert counts(owner) == {"v:aaa": 2, "upstream": 3, "v:bbb": 0}  # refunded


def test_zero_or_negative_caps_refuse(store):
    assert store.begin_live_search("ccc", 0, 10) == "visitor_limit"
    assert store.begin_live_search("ccc", 10, 0) == "global_limit"
    assert store.take_upstream_slot(-1) is False


def test_concurrent_visitors_never_pass_the_cap(pg, owner, search_mod):
    results = []

    def worker():
        with search_mod.open_store(pg["app"]) as s:
            results.append(s.begin_live_search("racer", 5, 1000))

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("ok") == 5 and results.count("visitor_limit") == 7
    assert counts(owner)["v:racer"] == 5


# ------------------------------------------------------------------ pacing


def test_reservations_are_spaced_by_the_interval(store):
    waits = [store.reserve_call("nominatim", 3000) for _ in range(4)]
    assert waits[0] == 0
    assert 1000 <= waits[1] <= 1100 and 2100 <= waits[2] <= 2200
    assert waits[3] is None  # 3300 ms would pass the 3000 ms maximum


def test_concurrent_reservations_take_turns(pg, owner, search_mod):
    barrier = threading.Barrier(6)
    starts = []

    def worker():
        with app_conn(pg) as conn:
            barrier.wait()
            wait_ms, now = conn.execute("select places.reserve_call('nominatim', 60000), clock_timestamp()").fetchone()
            starts.append(now.timestamp() + wait_ms / 1000)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    starts.sort()
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert len(gaps) == 5 and all(gap >= 1.08 for gap in gaps), gaps


def test_unknown_upstream_gets_no_slot(store):
    assert store.reserve_call("somewhere-else", 1000) is None


def test_pause_blocks_then_clears_and_is_capped(store, owner):
    assert store.reserve_call("overpass", 0) == 0
    store.pause_upstream("overpass", 30)
    assert store.reserve_call("overpass", 2000) is None
    assert store.reserve_call("overpass-mirror", 2000) == 0  # the mirror is not paused
    store.pause_upstream("overpass", 10**6)
    ahead = owner.execute(
        "select extract(epoch from next_at - clock_timestamp()) from places.upstreams where name = 'overpass'"
    ).fetchone()[0]
    assert 290 < ahead <= 300
    owner.execute("update places.upstreams set next_at = clock_timestamp() - interval '1 second'")
    assert store.reserve_call("overpass", 0) == 0


def test_the_database_refuses_a_nominatim_interval_under_one_second(owner):
    with pytest.raises(psycopg.errors.CheckViolation):
        owner.execute("update places.upstreams set min_interval_ms = 500 where name = 'nominatim'")


# ------------------------------------------------------------------ the whole function


def test_a_search_end_to_end_against_postgres(pg, owner, search_mod, monkeypatch):
    clock = FakeClock()
    upstream = FakeUpstream(clock, search_mod.Reply)
    monkeypatch.setattr(search_mod, "http_request", upstream)
    monkeypatch.setenv("PLACES_DB_URL", pg["app"])
    monkeypatch.setenv("PLACES_HASH_SALT", "pg-test-salt")
    monkeypatch.setenv("PLACES_ALLOWED_ORIGINS", ORIGIN)
    body = json.dumps({"what": "Coffee", "where": "Washington Square Park, New York"}).encode()
    headers = {"Origin": ORIGIN, "Content-Type": "application/json", "Content-Length": str(len(body)),
               "X-Forwarded-For": "203.0.113.42"}

    status, _, first = run_handler(search_mod.handler, "POST", headers, body)
    first = json.loads(first)
    assert status == 200 and first["cached"] is False and len(first["places"]) == 2
    started = time.monotonic()
    status, _, second = run_handler(search_mod.handler, "POST", headers, body)
    second = json.loads(second)
    assert status == 200 and second["cached"] is True and second["places"] == first["places"]
    assert time.monotonic() - started < 2
    assert upstream.count() == 2
    assert counts(owner)["upstream"] == 2
    # No address anywhere in the database, only the salted hash.
    dump = json.dumps([owner.execute(f"select r::text from places.{t} r").fetchall()
                       for t in ("geocode_cache", "results_cache", "daily_counts", "upstreams")])
    assert "203.0.113.42" not in dump
    assert "v:" + search_mod.visitor_key("203.0.113.42", "pg-test-salt") in dump
