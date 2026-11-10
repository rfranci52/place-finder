"""Checks on the files around the code: page metadata, deployment headers and house style."""
import json
import re
import subprocess

from conftest import ROOT

TEXT_SUFFIXES = {".py", ".ts", ".tsx", ".css", ".html", ".json", ".sql", ".md", ".svg", ".toml", ".txt", ".sh"}
SKIP_PARTS = {"node_modules", "dist", ".venv", ".git", "__pycache__", ".pytest_cache", ".hypothesis"}


def project_files():
    for path in ROOT.rglob("*"):
        if path.is_file() and path.suffix in TEXT_SUFFIXES and not SKIP_PARTS & set(path.relative_to(ROOT).parts):
            if path.name != "package-lock.json" and path.name != "uv.lock":
                yield path


def test_no_em_dashes_anywhere():
    offenders = [str(p.relative_to(ROOT)) for p in project_files() if "\u2014" in p.read_text(encoding="utf-8")]
    assert offenders == []


def test_page_metadata():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    for needle in ('name="description"', 'property="og:title"', 'property="og:description"', 'property="og:type"',
                   'rel="icon" type="image/svg+xml" href="/favicon.svg"', '<html lang="en">'):
        assert needle in html
    assert (ROOT / "frontend" / "public" / "favicon.svg").read_text(encoding="utf-8").startswith("<svg")


def test_required_links_and_attribution_are_on_the_page():
    app = (ROOT / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
    assert 'href="https://www.rakimfrancis.com"' in app and "rakimfrancis.com" in app
    assert 'href="https://github.com/rfranci52/place-finder"' in app
    assert 'href="https://github.com/rfranci52/react-google-maps"' in app and "Rebuilt from a 2019 bootcamp project" in app
    assert "OpenStreetMap contributors" in app
    map_view = (ROOT / "frontend" / "src" / "MapView.tsx").read_text(encoding="utf-8")
    assert "&copy; <a href=\"https://www.openstreetmap.org/copyright\">OpenStreetMap</a> contributors" in map_view
    assert "operations.osmfoundation.org/policies/tiles" in map_view


def test_vercel_headers():
    config = json.loads((ROOT / "frontend" / "vercel.json").read_text(encoding="utf-8"))
    headers = {h["key"]: h["value"] for h in config["headers"][0]["headers"]}
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "geolocation=()" in headers["Permissions-Policy"]
    csp = headers["Content-Security-Policy-Report-Only"]
    assert "img-src 'self' data: https://tile.openstreetmap.org;" in csp
    assert "connect-src 'self';" in csp and "frame-ancestors 'none'" in csp
    assert config["functions"]["api/search.py"]["maxDuration"] == 30


def test_the_function_is_self_contained():
    """Vercel does not reliably bundle sibling modules, so search.py imports only stdlib and psycopg."""
    source = (ROOT / "frontend" / "api" / "search.py").read_text(encoding="utf-8")
    imports = set(re.findall(r"^\s*(?:import|from) ([\w.]+)", source, re.M))
    third_party = {name.split(".")[0] for name in imports} - set(__import__("sys").stdlib_module_names)
    assert third_party == {"psycopg"}
    requirements = (ROOT / "frontend" / "requirements.txt").read_text(encoding="utf-8")
    assert requirements.strip() == "psycopg[binary]>=3.1,<4"


def test_no_secrets_or_env_files_are_tracked_or_trackable():
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".env" in ignore and ".env.*" in ignore
    if (ROOT / ".git").exists():
        tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
        assert not [line for line in tracked.splitlines() if line.split("/")[-1].startswith(".env")]
