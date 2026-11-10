"""Input validation and the stable response shapes the page renders."""
import pytest

from conftest import COFFEE_NEAR_PARK

ERROR_KEYS = {"code", "message"}


def assert_error(resp, status, code):
    assert resp.status == status
    assert resp.json["ok"] is False
    assert set(resp.json) == {"ok", "error"}
    assert ERROR_KEYS <= set(resp.json["error"]) <= ERROR_KEYS | {"retry_after"}
    assert resp.json["error"]["code"] == code
    assert isinstance(resp.json["error"]["message"], str) and resp.json["error"]["message"]


@pytest.mark.parametrize("body,fragment", [
    ({}, "Type what"),
    ({"what": "", "where": "nyc"}, "Type what"),
    ({"what": "a", "where": "nyc"}, "Type what"),
    ({"what": "   ", "where": "nyc"}, "Type what"),
    ({"what": "x" * 41, "where": "nyc"}, "40 characters"),
    ({"what": "x" * 500, "where": "nyc"}, "40 characters"),
    ({"what": 7, "where": "nyc"}, "must be text"),
    ({"what": "coffee"}, "Add a location"),
    ({"what": "coffee", "where": "y" * 81}, "80 characters"),
    ({"what": "coffee", "where": ["nyc"]}, "must be text"),
    ({"what": "coffee", "where": "nyc", "lat": 1, "lng": 2}, "not both"),
    ({"what": "coffee", "lat": 40.7}, "'lng' must be a number"),
    ({"what": "coffee", "lat": 91, "lng": 0}, "between -90 and 90"),
    ({"what": "coffee", "lat": 0, "lng": -181}, "between -180 and 180"),
    ({"what": "coffee", "lat": True, "lng": 0}, "must be a number"),
    ({"what": "coffee", "lat": "40", "lng": "-73"}, "must be a number"),
    ({"what": "coffee", "where": "nyc", "radius": 2000}, "radius"),
    ({"what": "coffee", "where": "nyc", "radius": 1500.0}, "radius"),
    ({"what": "coffee", "where": "nyc", "radius": True}, "radius"),
    ({"what": "coffee", "where": "nyc", "extra": 1}, "Unexpected field"),
])
def test_invalid_requests(world, body, fragment):
    resp = world.post(body)
    assert_error(resp, 400, "invalid")
    assert fragment in resp.json["error"]["message"]
    assert world.upstream.count() == 0


@pytest.mark.parametrize("raw,fragment", [
    (b'{"what": "coffee", "lat": 1e400, "lng": 0}', "between"),  # parses as infinity
    (b'{"what": "coffee", "lat": Infinity, "lng": 0}', "not valid JSON"),
    (b'{"what": "coffee", "lat": NaN, "lng": 0}', "not valid JSON"),
])
def test_non_finite_numbers_are_refused(world, raw, fragment):
    resp = world.post(None, raw=raw)
    assert_error(resp, 400, "invalid")
    assert fragment in resp.json["error"]["message"]


def test_limits_count_cleaned_characters(world):
    assert world.post({"what": "c" * 40, "where": "w" * 80}).status == 200
    assert world.post({"what": "  " + "c" * 40 + "  ", "where": "nyc"}).status == 200


def test_success_shape(world):
    resp = world.post(COFFEE_NEAR_PARK)
    assert resp.status == 200
    body = resp.json
    assert set(body) == {"ok", "query", "center", "location", "places", "truncated", "cached", "fetched_at"}
    assert body["ok"] is True
    assert body["query"] == {"what": "Coffee", "term": {"kind": "category", "key": "coffee", "label": "Coffee"},
                             "radius_m": 1500}
    assert body["center"] == {"lat": 40.731, "lng": -73.997}
    assert body["location"] == "Washington Square Park, Manhattan, New York County"
    assert body["fetched_at"].endswith("Z")
    for place in body["places"]:
        assert set(place) == {"id", "name", "category", "address", "phone", "hours", "website", "lat", "lng",
                              "osm_url", "distance_m"}


def test_map_position_search_has_no_location_label(world):
    resp = world.post({"what": "coffee", "lat": 40.7, "lng": -74.0, "radius": 2500})
    assert resp.json["location"] is None
    assert resp.json["query"]["radius_m"] == 2500
    assert world.upstream.count("nominatim") == 0


def test_not_found_shape(world):
    world.upstream.script("nominatim", (200, b"[]"))
    resp = world.post({"what": "coffee", "where": "Qwxz Nowhere"})
    assert_error(resp, 404, "not_found")
    assert "Qwxz Nowhere" in resp.json["error"]["message"]


def test_rate_limited_shape(world):
    world.env(PLACES_VISITOR_DAILY=0)
    resp = world.post(COFFEE_NEAR_PARK)
    assert_error(resp, 429, "rate_limited")
    assert resp.json["error"]["retry_after"] == int(resp.headers["Retry-After"])


def test_upstream_busy_shape(world):
    world.upstream.script("nominatim", (503, b""))
    assert_error(world.post(COFFEE_NEAR_PARK), 503, "upstream_busy")


def test_unavailable_shape(world):
    world.env(PLACES_DB_URL="unreachable")
    assert_error(world.post(COFFEE_NEAR_PARK), 503, "unavailable")


def test_forbidden_shape(world):
    assert_error(world.post(COFFEE_NEAR_PARK, origin="https://evil.example"), 403, "forbidden")


def test_every_code_has_a_status(search_mod):
    assert search_mod.ERRORS == {
        "invalid": 400, "forbidden": 403, "not_found": 404, "method_not_allowed": 405,
        "rate_limited": 429, "upstream_busy": 503, "unavailable": 503,
    }
