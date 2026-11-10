"""Request handling: origin (403), JSON only (415), size cap (413), POST only (405), and no stack
traces in any response."""
import http.client
import json
import socket
import threading
from http.server import ThreadingHTTPServer

import pytest

from conftest import COFFEE_NEAR_PARK, ORIGIN


@pytest.mark.parametrize("origin", [
    None, "", "null", "https://evil.example", "https://places.example.test.evil.example",
    "http://places.example.test", "https://places.example.test/path", "https://user@places.example.test",
    "https://places.example.test:8443", "javascript:alert(1)",
])
def test_foreign_or_missing_origin_is_403(world, origin):
    resp = world.post(COFFEE_NEAR_PARK, origin=origin)
    assert resp.status == 403 and resp.code == "forbidden"
    assert world.upstream.count() == 0


@pytest.mark.parametrize("origin", [ORIGIN, "HTTPS://Places.Example.Test", ORIGIN + "/"])
def test_the_configured_origin_passes(world, origin):
    assert world.post(COFFEE_NEAR_PARK, origin=origin).status == 200


def test_plain_http_origins_are_only_for_local_hosts(search_mod):
    allowed = search_mod.parse_origins("http://localhost:5174, http://example.com, https://ok.example, junk")
    assert allowed == {"http://localhost:5174", "https://ok.example"}
    assert search_mod.origin_allowed("http://localhost:5174", allowed)
    assert not search_mod.origin_allowed("http://localhost:5175", allowed)


def test_no_origins_configured_refuses_everything(world):
    world.env(PLACES_ALLOWED_ORIGINS=None)
    assert world.post(COFFEE_NEAR_PARK).status == 403


@pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded", "", "application/jsonp"])
def test_non_json_is_415(world, content_type):
    resp = world.post(COFFEE_NEAR_PARK, content_type=content_type)
    assert resp.status == 415 and resp.code == "invalid"


def test_json_with_charset_is_fine(world):
    assert world.post(COFFEE_NEAR_PARK, content_type="Application/JSON; charset=utf-8").status == 200


def test_oversized_body_is_413(world):
    resp = world.post(None, raw=b"{" + b" " * 3000 + b"}")
    assert resp.status == 413 and resp.code == "invalid"


@pytest.mark.parametrize("length", ["-1", "abc", "1e3"])
def test_bad_content_length_is_400(world, length):
    resp = world.post(COFFEE_NEAR_PARK, headers={"Content-Length": length})
    assert resp.status == 400


@pytest.mark.parametrize("raw", [b"not json", b"[1, 2]", b'"text"', b"null", b"{\"what\": NaN}", b"[" * 1500 + b"]" * 1500])
def test_unreadable_or_non_object_bodies_are_400(world, raw):
    resp = world.post(None, raw=raw[:2048])
    assert resp.status in (400, 413)
    assert resp.code == "invalid"


def test_responses_are_json_and_not_cacheable(world):
    for resp in (world.post(COFFEE_NEAR_PARK), world.post({"what": "x"}), world.post(COFFEE_NEAR_PARK, origin=None)):
        assert resp.headers["Content-Type"] == "application/json; charset=utf-8"
        assert resp.headers["Cache-Control"] == "no-store"
        assert int(resp.headers["Content-Length"]) > 0


def test_an_unexpected_error_leaks_nothing(world, search_mod, monkeypatch):
    def explode(*args):
        raise RuntimeError("secret detail at /var/task/search.py line 99")

    monkeypatch.setattr(search_mod, "normalize_places", explode)
    resp = world.post(COFFEE_NEAR_PARK)
    assert resp.status == 503 and resp.code == "unavailable"
    text = json.dumps(resp.json)
    assert "secret" not in text and "Traceback" not in text and "search.py" not in text


def test_logs_never_carry_the_address_or_search_text(world, capsys):
    world.post({"what": "Joe's", "where": "1 Private Lane, Hometown"}, ip="203.0.113.99")
    out = capsys.readouterr().out
    assert "203.0.113.99" not in out and "Private Lane" not in out and "Joe" not in out


# ------------------------------------------------------------------ over a real socket
# The standard library's default error page is produced by its request parsing, which the
# in-memory bridge skips, so these run a real server on a local port.


@pytest.fixture
def port(search_mod):
    class Quiet(search_mod.handler):
        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def send(port, method, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, "/api/search", body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read()
    finally:
        conn.close()


@pytest.mark.parametrize("method", ["GET", "PUT", "PATCH", "DELETE", "OPTIONS"])
def test_other_methods_get_a_json_405(port, method):
    status, headers, body = send(port, method)
    assert status == 405
    assert headers["Allow"] == "POST"
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    assert json.loads(body) == {"ok": False, "error": {"code": "method_not_allowed", "message": "Use POST for this endpoint."}}


def test_head_gets_a_405_with_no_body(port):
    status, headers, body = send(port, "HEAD")
    assert status == 405 and headers["Allow"] == "POST" and body == b""


def test_a_malformed_request_line_gets_json_not_html(port):
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(b"GET / NOT-A-VERSION\r\n\r\n")
        data = b""
        while chunk := sock.recv(4096):
            data += chunk
    assert b"<html" not in data.lower() and b"<!doctype" not in data.lower()
    assert b'"code": "invalid"' in data


def test_post_reaches_the_handler_over_a_socket(port):
    status, _, body = send(port, "POST", b"{}", {"Content-Type": "application/json", "Origin": "https://evil.example"})
    assert status == 403 and json.loads(body)["error"]["code"] == "forbidden"
