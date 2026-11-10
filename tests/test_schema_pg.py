"""The database and the data pipeline against a real, throwaway Postgres with PostGIS.

Skipped unless PLACES_TEST_ADMIN_DSN points at a superuser on a local cluster that has PostGIS:

    PLACES_TEST_ADMIN_DSN=postgresql://postgres:PASSWORD@127.0.0.1:54330/postgres

Each run creates fresh databases with random names. The cluster should be a throwaway one: this
suite creates databases, and it sets the places_app password, which (like every Postgres role)
is shared by every database in the cluster, so a development database there loses its login.

The data is synthetic (about 1,200 places around Washington Square Park, simple borough boxes)
and goes through the real loader, scripts/load_places.py, and the function's real Store class.
"""
import json
import os
import secrets
import threading
import uuid
from argparse import Namespace
from urllib.parse import quote, urlsplit, urlunsplit

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from conftest import ORIGIN, ROOT, FakeClock, FakeGeoSearch, load_module, run_handler

ADMIN_DSN = os.environ.get("PLACES_TEST_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not ADMIN_DSN, reason="PLACES_TEST_ADMIN_DSN is not set")
psycopg = pytest.importorskip("psycopg")
duckdb = pytest.importorskip("duckdb")
RELEASE = "test-release"
PARK = (40.7307, -73.9957)


def _dsn(dbname, user=None, password=None):
    parts = urlsplit(ADMIN_DSN)
    netloc = parts.netloc if user is None else f"{quote(user, safe='')}:{quote(password, safe='')}@{parts.hostname}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, "/" + dbname, parts.query, ""))


def box(xmin, ymin, xmax, ymax):
    return {"type": "MultiPolygon", "coordinates": [[[[xmin, ymin], [xmax, ymin], [xmax, ymax], [xmin, ymax], [xmin, ymin]]]]}


BOROUGHS = [  # simple stand-ins for the five borough outlines; Hoboken (-74.0324, 40.744) is outside
    ("Manhattan", 1, box(-74.03, 40.68, -73.90, 40.88)),
    ("Bronx", 2, box(-73.90, 40.80, -73.75, 40.91)),
    ("Brooklyn", 3, box(-74.05, 40.57, -73.85, 40.679)),
    ("Queens", 4, box(-73.899, 40.68, -73.70, 40.79)),
    ("Staten Island", 5, box(-74.26, 40.49, -74.06, 40.65)),
]
NTAS = [
    ("Greenwich Village", "Manhattan", "0", box(-74.003, 40.726, -73.990, 40.736)),
    ("Chelsea", "Manhattan", "0", box(-74.008, 40.740, -73.995, 40.752)),
    ("Chelsea", "Staten Island", "0", box(-74.20, 40.58, -74.18, 40.60)),
    ("Williamsburg", "Brooklyn", "0", box(-73.97, 40.665, -73.95, 40.675)),
    ("Riverdale", "Bronx", "0", box(-73.91, 40.88, -73.89, 40.90)),
    ("Astoria (Central)", "Queens", "0", box(-73.93, 40.76, -73.91, 40.77)),
    ("Rikers Island", "Bronx", "5", box(-73.89, 40.78, -73.87, 40.79)),
]
SPECIAL = [  # (name, category) placed near the park; the rest are filler
    ("Joe's Pizza", "pizza_restaurant"), ("100% Juice Bar", "smoothie_juice_bar"), ("Under_Score Deli", "delicatessen"),
    ("UnderXScore Deli", "delicatessen"), ("Starbucks", "coffee_shop"), ("Galanga", "thai_restaurant"),
    ("Café Étoile", "cafe"), ("Back\\slash Books", "bookstore"),
]


def write_inputs(data_dir):
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "nyc_borough_boundaries_wi.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"boroname": name, "borocode": str(code)}, "geometry": geom}
        for name, code, geom in BOROUGHS]}))
    (data_dir / "nyc_nta_2020.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"ntaname": name, "boroname": boro, "ntatype": kind}, "geometry": geom}
        for name, boro, kind, geom in NTAS]}))
    rows = []
    for i, (name, category) in enumerate(SPECIAL):
        rows.append((name, category, 0.95, "open", PARK[0] + 0.0005 * (i + 1), PARK[1], ["https://shop.example"]))
    for i in range(1200):
        lat, lng = PARK[0] + (i % 40) * 0.0004 - 0.008, PARK[1] + (i // 40) * 0.0004 - 0.006
        category = ("coffee_shop", "cafe", "pizza_restaurant", "pharmacy", "laundromat")[i % 5]
        status = "permanently_closed" if i % 50 == 0 else ("open" if i % 3 else None)
        confidence = 0.5 if i % 25 == 1 else 0.9
        website = ["http://plain.example"] if i % 7 == 0 else (["Https://Upper.example"] if i % 11 == 0 else [])
        rows.append((f"Filler {category} {i}", category, confidence, status, lat, lng, website))
    con = duckdb.connect()
    con.execute("""CREATE TABLE t (id VARCHAR, name VARCHAR, category VARCHAR, basic_category VARCHAR, confidence DOUBLE,
        operating_status VARCHAR, street VARCHAR, locality VARCHAR, postcode VARCHAR, phones VARCHAR[], websites VARCHAR[],
        datasets VARCHAR[], licenses VARCHAR[], lng DOUBLE, lat DOUBLE, borough VARCHAR)""")
    con.executemany("INSERT INTO t VALUES (?, ?, ?, NULL, ?, ?, '1 Test St', 'New York', '10012', ['+12125550100'], ?, "
                    "['Overture', 'meta'], ['CDLA-Permissive-2.0'], ?, ?, 'Manhattan')",
                    [(str(uuid.uuid4()), n, c, conf, s, w, lng, lat) for n, c, conf, s, lat, lng, w in rows])
    con.execute(f"COPY t TO '{data_dir / f'overture_places_nyc_{RELEASE}.parquet'}' (FORMAT parquet)")
    return len(rows)


@pytest.fixture(scope="module")
def pipeline():
    return load_module(ROOT / "scripts" / "load_places.py", "place_pipeline_for_pg_tests")


def new_database(prefix="places_test_"):
    assert urlsplit(ADMIN_DSN).hostname in ("127.0.0.1", "localhost", "::1"), "refusing a non-local database"
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        if not admin.execute("select 1 from pg_available_extensions where name = 'postgis'").fetchone():
            pytest.skip("PostGIS is not installed on this cluster")
        dbname = prefix + secrets.token_hex(4)
        admin.execute(f'create database "{dbname}"')
    owner = _dsn(dbname)
    schema = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    with psycopg.connect(owner, autocommit=True) as conn:
        for _ in range(2):  # safe to apply repeatedly
            with conn.transaction():
                conn.execute(schema)
    return owner


def run_load(pipeline, monkeypatch, data_dir, owner):
    monkeypatch.setattr(pipeline, "DATA_DIR", data_dir)
    monkeypatch.setenv("PLACES_TEST_OWNER_URL", owner)
    pipeline.load(Namespace(release=RELEASE, min_confidence=0.7, max_mb=200, dsn_env="PLACES_TEST_OWNER_URL", env_file=None))


@pytest.fixture(scope="module")
def pg(pipeline, tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("data")
    written = write_inputs(data_dir)
    owner = new_database()
    mp = pytest.MonkeyPatch()
    try:
        run_load(pipeline, mp, data_dir, owner)
    finally:
        mp.undo()
    password = secrets.token_urlsafe(16)
    with psycopg.connect(owner, autocommit=True) as conn:
        conn.execute(psycopg.sql.SQL("alter role places_app login password {}").format(psycopg.sql.Literal(password)))
    dbname = urlsplit(owner).path.lstrip("/")
    return {"owner": owner, "app": _dsn(dbname, "places_app", password), "data_dir": data_dir, "written": written}


@pytest.fixture
def owner(pg):
    with psycopg.connect(pg["owner"], autocommit=True) as conn:
        yield conn


@pytest.fixture
def store(pg, search_mod):
    with search_mod.open_store(pg["app"]) as s:
        yield s


def fresh_counters(owner_conn):
    owner_conn.execute("update places.daily_counts set n = 0")


# ------------------------------------------------------------------ the role


@pytest.mark.parametrize("statement", [
    "select * from places.geocode_cache",
    "select * from places.daily_counts",
    "insert into places.daily_counts (day, key, n) values (current_date, 'search:site', 0)",
    "update places.places set name = 'x'",
    "insert into places.meta (key, value) values ('k', 'v')",
    "update places.neighborhoods set label = 'x'",
    "select places.slot_for('x', 10)",
    "create table places.mine (x int)",
])
def test_the_app_role_cannot_write_or_read_private_tables(pg, statement):
    with psycopg.connect(pg["app"], autocommit=True) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(statement)


def test_the_app_role_reads_the_public_data(pg):
    with psycopg.connect(pg["app"], autocommit=True) as conn:
        assert conn.execute("select count(*) from places.places").fetchone()[0] > 1000
        assert conn.execute("select count(*) from places.boroughs").fetchone()[0] == 5
        assert conn.execute("select count(*) from places.neighborhoods").fetchone()[0] > 0
        assert conn.execute("show statement_timeout").fetchone()[0] == "5s"


def test_setup_script_grant_check_passes(pg, owner):
    setup = load_module(ROOT / "scripts" / "setup_db.py", "place_setup_for_pg_tests")
    assert setup.verify(owner) == []


# ------------------------------------------------------------------ what the loader wrote


def test_the_loader_kept_the_right_rows(pg, owner):
    rows = owner.execute("select count(*) from places.places").fetchone()[0]
    # Closed places and confidence under 0.7 are left out.
    expected = len(SPECIAL) + sum(1 for i in range(1200) if i % 50 != 0 and i % 25 != 1)
    assert rows == expected
    assert owner.execute("select count(*) from places.places where website like 'http:%'").fetchone()[0] == 0
    assert owner.execute("select count(*) from places.places where website = 'https://Upper.example'").fetchone()[0] > 0
    indexes = {r[0] for r in owner.execute("select indexname from pg_indexes where schemaname = 'places' and tablename = 'places'")}
    assert indexes == {"places_pkey", "places_geog_idx", "places_name_trgm_idx", "places_category_idx"}
    meta = dict(owner.execute("select key, value from places.meta").fetchall())
    assert (meta["release"], meta["min_confidence"], meta["rows"]) == (RELEASE, "0.7", str(rows))


def test_neighborhood_names_were_built_and_rikers_left_out(pg, owner):
    names = dict(owner.execute("select alias || '/' || borocode, label from places.neighborhoods").fetchall())
    assert names["greenwich village/1"] == "Greenwich Village"
    assert names["chelsea/1"] == "Chelsea" and names["chelsea/5"] == "Chelsea"
    assert names["astoria/4"] == "Astoria"
    assert names["manhattan/1"] == "Manhattan" and names["the bronx/2"] == "The Bronx"
    assert not any(k.startswith("rikers") for k in names)


def test_a_second_load_swaps_and_keeps_the_previous_table(pipeline, pg, monkeypatch, owner):
    run_load(pipeline, monkeypatch, pg["data_dir"], pg["owner"])
    tables = {r[0] for r in owner.execute(
        "select tablename from pg_tables where schemaname = 'places' and tablename like 'places%'")}
    assert tables == {"places", "places_previous"}
    assert owner.execute("select has_table_privilege('places_app', 'places.places_previous', 'select')").fetchone()[0] is False
    assert owner.execute("select has_table_privilege('places_app', 'places.places', 'select')").fetchone()[0] is True
    # A third load refuses while the previous table is still there.
    with pytest.raises(SystemExit) as stopped:
        run_load(pipeline, monkeypatch, pg["data_dir"], pg["owner"])
    assert stopped.value.code == 2


def test_a_failed_load_leaves_the_live_table_untouched(pipeline, pg, monkeypatch, tmp_path):
    owner_dsn = new_database("places_fail_")
    run_load(pipeline, monkeypatch, pg["data_dir"], owner_dsn)
    with psycopg.connect(owner_dsn, autocommit=True) as conn:
        before = conn.execute("select count(*), min(id::text) from places.places").fetchone()

    def broken(conn, loaded):
        raise RuntimeError("simulated failure after the copy")

    # A second load would swap; this one fails after copying and indexing, before the swap.
    monkeypatch.setattr(pipeline, "checks", broken)
    with pytest.raises(RuntimeError, match="simulated failure"):
        run_load(pipeline, monkeypatch, pg["data_dir"], owner_dsn)
    with psycopg.connect(owner_dsn, autocommit=True) as conn:
        tables = {r[0] for r in conn.execute(
            "select tablename from pg_tables where schemaname = 'places' and tablename like 'places%'")}
        assert tables == {"places"}  # no staging table left, no previous table made
        assert conn.execute("select count(*), min(id::text) from places.places").fetchone() == before
        assert conn.execute("select has_table_privilege('places_app', 'places.places', 'select')").fetchone()[0]


# ------------------------------------------------------------------ the search query


def names(rows):
    return [r[1] for r in rows]


def test_category_search_is_nearest_first_within_the_radius(store, search_mod):
    rows = store.search_places(search_mod.resolve_term("coffee"), *PARK, 500, 61)
    assert len(rows) == 61
    distances = [r[-1] for r in rows]
    assert distances == sorted(distances) and distances[-1] <= 500
    assert {r[2] for r in rows} <= {"coffee_shop", "cafe", "coffee_roastery"}


def test_name_search_substring_typo_and_category_word(store, search_mod):
    def find(text):
        return names(store.search_places(search_mod.resolve_term(text), *PARK, 1500, 61))

    assert find("joe's") == ["Joe's Pizza"]
    assert find("starbuks")[:1] == ["Starbucks"]  # word similarity catches the typo
    assert "Galanga" in find("thai")  # category key thai_restaurant
    assert "100% Juice Bar" in find("100%")


@pytest.mark.parametrize("text,expected", [
    ("100%", ["100% Juice Bar"]),
    ("under_score", ["Under_Score Deli"]),  # not UnderXScore: the typed _ is literal
    ("back\\slash", ["Back\\slash Books"]),
    ("%", ["100% Juice Bar"]),
])
def test_typed_wildcards_are_literal_in_the_name_pattern(store, search_mod, text, expected):
    """The ILIKE part of the name match on its own (the fuzzy part is meant to be loose)."""
    _, params = search_mod.search_params(search_mod.resolve_term(text), *PARK, 1500, 61)
    rows = store._conn.execute(
        "select name from places.places where name ilike %(contains)s escape '\\' order by name", params).fetchall()
    assert [r[0] for r in rows] == expected


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.text(st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"), min_size=1, max_size=30),
       st.text(st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"), min_size=0, max_size=30))
def test_like_escaping_on_real_postgres(store, search_mod, text, other):
    pattern = search_mod.like_escape(text)
    conn = store._conn
    assert conn.execute("select %s like %s escape '\\'", (text, pattern)).fetchone()[0] is True
    assert conn.execute("select %s like %s escape '\\'", ("x" + text + "y", f"%{pattern}%")).fetchone()[0] is True
    assert conn.execute("select %s like %s escape '\\'", (other, pattern)).fetchone()[0] is (other == text)


def test_borough_check_and_release(store):
    assert store.area_of(*PARK) == ("Manhattan", RELEASE)
    assert store.area_of(40.7440, -74.0324)[0] is None  # Hoboken
    assert store.area_of(42.36, -71.06)[0] is None  # Boston


def test_neighborhood_lookup(store):
    assert store.neighborhood("greenwich village", None)[:2] == ("Greenwich Village", "Manhattan")
    assert store.neighborhood("chelsea", None)[1] == "Manhattan"
    assert store.neighborhood("chelsea", 5)[1] == "Staten Island"
    assert store.neighborhood("nowhere", None) is None


# ------------------------------------------------------------------ caches and limits


def test_geocode_cache_round_trip_and_lifetime(store, owner, search_mod):
    geo = search_mod.Geocode(True, 40.7484, -73.9857, "350 5 AVENUE, New York, NY, USA")
    store.geocode_put("350 5th ave", geo)
    assert store.geocode_get("350 5th ave") == geo
    owner.execute("update places.geocode_cache set fetched_at = now() - interval '31 days'")
    assert store.geocode_get("350 5th ave") is None


def counts(owner_conn):
    return dict(owner_conn.execute(
        "select key, n from places.daily_counts where day = (now() at time zone 'utc')::date").fetchall())


def test_take_slot_visitor_first_site_refund_and_kinds(store, owner):
    fresh_counters(owner)
    assert [store.take_slot("search", "aaa", 2, 3) for _ in range(3)] == ["ok", "ok", "visitor_limit"]
    assert store.take_slot("search", "bbb", 2, 3) == "ok"
    assert store.take_slot("search", "ccc", 2, 3) == "site_limit"
    got = counts(owner)
    assert (got["search:v:aaa"], got["search:v:bbb"], got["search:v:ccc"], got["search:site"]) == (2, 1, 0, 3)
    assert store.take_slot("geocode", "aaa", 1, 1) == "ok"  # a separate allowance
    with pytest.raises(psycopg.errors.RaiseException):
        store.take_slot("other", "aaa", 1, 1)


def test_concurrent_visitors_never_pass_the_cap(pg, owner, search_mod):
    fresh_counters(owner)
    results = []

    def worker():
        with search_mod.open_store(pg["app"]) as s:
            results.append(s.take_slot("search", "racer", 5, 1000))

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("ok") == 5 and results.count("visitor_limit") == 7


# ------------------------------------------------------------------ the whole function


def test_a_request_end_to_end(pg, owner, search_mod, monkeypatch):
    fresh_counters(owner)
    geosearch = FakeGeoSearch(FakeClock(), search_mod.Reply)
    monkeypatch.setattr(search_mod, "http_request", geosearch)
    monkeypatch.setenv("PLACES_DB_URL", pg["app"])
    monkeypatch.setenv("PLACES_HASH_SALT", "pg-test-salt")
    monkeypatch.setenv("PLACES_ALLOWED_ORIGINS", ORIGIN)

    def post(body):
        raw = json.dumps(body).encode()
        headers = {"Origin": ORIGIN, "Content-Type": "application/json", "Content-Length": str(len(raw)),
                   "X-Forwarded-For": "203.0.113.42"}
        status, _, data = run_handler(search_mod.handler, "POST", headers, raw)
        return status, json.loads(data)

    status, body = post({"what": "pizza", "where": "Greenwich Village"})
    assert status == 200 and body["location"] == "Greenwich Village, Manhattan"
    assert "Joe's Pizza" in [p["name"] for p in body["places"]] and body["data_release"] == RELEASE
    assert geosearch.count() == 0
    status, body = post({"what": "coffee", "lat": 40.7440, "lng": -74.0324})
    assert status == 422 and body["error"]["code"] == "out_of_area"
    assert counts(owner)["search:site"] == 2
    dump = json.dumps([owner.execute(f"select r::text from places.{t} r").fetchall()
                       for t in ("geocode_cache", "daily_counts")])
    assert "203.0.113.42" not in dump
    assert "search:v:" + search_mod.visitor_key("203.0.113.42", "pg-test-salt") in dump
