"""The setup script's pure helpers and the local dev server. setup_db.main() is never called here."""
import http.client
import json
import os
import stat
import threading
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from conftest import ROOT, FakeClock, FakeGeoSearch, FakeStore, load_module


@pytest.fixture(scope="module")
def setup_db():
    return load_module(ROOT / "scripts" / "setup_db.py", "place_setup_for_tests")


@pytest.fixture(scope="module")
def dev_api():
    return load_module(ROOT / "scripts" / "dev_api.py", "place_dev_api_for_tests")


OWNER = "postgresql://neondb_owner:ownerpw@ep-cool-frog-123456.us-east-2.aws.neon.tech/neondb?sslmode=require&options=endpoint%3Dx"


def test_pooled_host_only_changes_neon_hosts(setup_db):
    assert setup_db.pooled_host("ep-a-1.us-east-2.aws.neon.tech") == "ep-a-1-pooler.us-east-2.aws.neon.tech"
    assert setup_db.pooled_host("ep-a-1-pooler.us-east-2.aws.neon.tech") == "ep-a-1-pooler.us-east-2.aws.neon.tech"
    assert setup_db.pooled_host("127.0.0.1") == "127.0.0.1"


def test_role_url_is_pooled_without_options(setup_db):
    url = setup_db.role_url(OWNER, "p@ss/w:rd")
    parts = urlsplit(url)
    assert parts.hostname == "ep-cool-frog-123456-pooler.us-east-2.aws.neon.tech"
    assert parts.username == "places_app"
    assert setup_db.existing_password(url) == "p@ss/w:rd"
    query = parse_qs(parts.query)
    assert "options" not in query and query["sslmode"] == ["require"]
    assert parts.path == "/neondb"


def test_plan_generates_once_and_then_reuses(setup_db):
    updates, password = setup_db.plan({}, OWNER)
    assert set(updates) == {"PLACES_HASH_SALT", "PLACES_DB_URL"}
    assert len(updates["PLACES_HASH_SALT"]) == 64 and len(password) >= 32
    again, same = setup_db.plan(updates, OWNER)
    assert again == {} and same == password


def test_write_env_keeps_other_lines_and_is_private(setup_db, tmp_path):
    path = tmp_path / "private" / ".env.places"
    path.parent.mkdir()
    path.write_text("# notes\nOTHER=1\nPLACES_DB_URL=old\n", encoding="utf-8")
    setup_db.write_env({"PLACES_DB_URL": "new", "PLACES_HASH_SALT": "s"}, path)
    assert path.read_text(encoding="utf-8") == "# notes\nOTHER=1\nPLACES_DB_URL=new\nPLACES_HASH_SALT=s\n"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert setup_db.read_env(path) == {"OTHER": "1", "PLACES_DB_URL": "new", "PLACES_HASH_SALT": "s"}


def test_default_env_file_sits_beside_the_repo(setup_db):
    assert setup_db.DEFAULT_ENV_FILE == ROOT.parent / "site_private" / ".env.places"


def test_function_list_matches_the_schema_grants(setup_db):
    schema = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    granted = sorted(line.split("function ", 1)[1].split(" to ")[0]
                     for line in schema.splitlines() if line.startswith("grant execute on function"))
    assert granted == sorted(setup_db.FUNCTIONS)


# ------------------------------------------------------------------ dev server


def test_dev_env_parsing(dev_api, tmp_path):
    path = tmp_path / "dev.env"
    path.write_text("# c\nexport A=1\nB='two'\nC=\"3\"\nbad line\n", encoding="utf-8")
    assert dev_api.parse_env_file(path) == {"A": "1", "B": "two", "C": "3"}


@pytest.fixture
def dev_server(dev_api, monkeypatch):
    search = dev_api.load_search()
    clock = FakeClock()
    store = FakeStore(clock, search)

    @contextmanager
    def fake_open_store(url):
        yield store

    monkeypatch.setattr(search, "open_store", fake_open_store)
    monkeypatch.setattr(search, "http_request", FakeGeoSearch(clock, search.Reply))
    monkeypatch.setenv("PLACES_DB_URL", "postgresql://fake")
    monkeypatch.setenv("PLACES_HASH_SALT", "s")
    monkeypatch.delenv("PLACES_ALLOWED_ORIGINS", raising=False)
    dev_api.load_env([])
    handler = dev_api.make_handler(search)
    handler.log_message = lambda *args: None
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def post(port, path, origin="http://localhost:5174"):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    body = json.dumps({"what": "coffee", "where": "Greenwich Village"})
    conn.request("POST", path, body=body, headers={"Content-Type": "application/json", "Origin": origin})
    resp = conn.getresponse()
    return resp.status, json.loads(resp.read())


def test_dev_server_serves_the_function_for_the_vite_origin(dev_server):
    status, body = post(dev_server, "/api/search")
    assert status == 200 and body["ok"] is True
    assert post(dev_server, "/api/search", origin="http://localhost:9999")[0] == 403


def test_dev_server_answers_only_the_deployed_path(dev_server):
    assert post(dev_server, "/api/other")[0] == 404


# ------------------------------------------------------------------ the data pipeline


@pytest.fixture(scope="module")
def pipeline():
    return load_module(ROOT / "scripts" / "load_places.py", "place_pipeline_for_tests")


@pytest.mark.parametrize("name,expected", [
    ("Greenwich Village", {"greenwich village": "Greenwich Village"}),
    ("SoHo-Little Italy-Hudson Square", {"soho little italy hudson square": "SoHo-Little Italy-Hudson Square",
                                         "soho": "SoHo", "little italy": "Little Italy", "hudson square": "Hudson Square"}),
    ("Bedford-Stuyvesant (East)", {"bedford stuyvesant": "Bedford-Stuyvesant", "bedford": "Bedford",
                                   "stuyvesant": "Stuyvesant"}),
    ("St. Michael's Cemetery", {"st michaels cemetery": "St. Michael's Cemetery"}),
])
def test_neighborhood_aliases(pipeline, name, expected):
    assert pipeline.nta_aliases(name) == expected


def test_aliases_fold_like_the_search_does(pipeline, search_mod):
    for alias in pipeline.nta_aliases("Astoria (North)-Ditmars-Steinway"):
        assert alias == search_mod.fold(alias)


@pytest.mark.parametrize("text,mib", [
    ("HTTPFS HTTP Stats in: 138.7 MiB #GET: 40", 138.7), ("in: 512.0 KiB", 0.5), ("in: 2.0 GiB", 2048.0), ("none", 0.0),
])
def test_bytes_read_comes_from_the_duckdb_profile(pipeline, text, mib):
    assert pipeline.http_mib(text) == pytest.approx(mib)


def test_default_floor_and_size_target(pipeline):
    assert pipeline.DEFAULT_MIN_CONFIDENCE == 0.7
    assert pipeline.DEFAULT_MAX_MB == 200
    assert pipeline.DEFAULT_RELEASE == "2026-10-21.0"
