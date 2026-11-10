"""Input validation and the stable response shapes the page renders."""
import pytest

from conftest import NEAR_THE_PARK

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
    ({"what": "coffee"}, "Add a neighborhood or address in New York City"),
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
    assert world.geosearch.count() == 0


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
    assert world.post({"what": "  " + "c" * 40 + "  ", "where": "Greenwich Village"}).status == 200


def test_success_shape(world):
    resp = world.post(NEAR_THE_PARK)
    assert resp.status == 200
    body = resp.json
    assert set(body) == {"ok", "query", "center", "location", "borough", "places", "truncated", "data_release"}
    assert body["ok"] is True
    assert body["query"] == {"what": "Coffee", "term": {"kind": "category", "key": "coffee", "label": "Coffee"},
                             "radius_m": 1500}
    assert body["center"] == {"lat": 40.7307, "lng": -73.9957}
    assert body["location"] == "Greenwich Village, Manhattan"
    assert (body["borough"], body["data_release"]) == ("Manhattan", "2026-10-21.0")
    assert body["places"]
    for place in body["places"]:
        assert set(place) == {"id", "name", "category", "address", "phone", "website", "borough", "lat", "lng",
                              "distance_m"}


def test_map_position_search_has_no_location_label(world):
    resp = world.post({"what": "coffee", "lat": 40.7307, "lng": -73.9957, "radius": 2500})
    assert resp.json["location"] is None
    assert resp.json["query"]["radius_m"] == 2500
    assert world.geosearch.count() == 0


def test_not_found_shape(world):
    world.geosearch.script((200, b'{"type": "FeatureCollection", "features": []}'))
    resp = world.post({"what": "coffee", "where": "Qwxz Nowhere"})
    assert_error(resp, 404, "not_found")
    assert "Qwxz Nowhere" in resp.json["error"]["message"]


def test_rate_limited_shape(world):
    world.env(PLACES_SEARCHES_PER_VISITOR=0)
    resp = world.post(NEAR_THE_PARK)
    assert_error(resp, 429, "rate_limited")
    assert resp.json["error"]["retry_after"] == int(resp.headers["Retry-After"])


def test_upstream_busy_shape(world):
    world.geosearch.script((503, b""))
    assert_error(world.post({"what": "coffee", "where": "350 5th Ave"}), 503, "upstream_busy")


def test_out_of_area_shape(world):
    assert_error(world.post({"what": "coffee", "lat": 42.36, "lng": -71.06}), 422, "out_of_area")


def test_unavailable_shape(world):
    world.env(PLACES_DB_URL="unreachable")
    assert_error(world.post(NEAR_THE_PARK), 503, "unavailable")


def test_forbidden_shape(world):
    assert_error(world.post(NEAR_THE_PARK, origin="https://evil.example"), 403, "forbidden")


def test_every_code_has_a_status(search_mod):
    assert search_mod.ERRORS == {
        "invalid": 400, "forbidden": 403, "not_found": 404, "method_not_allowed": 405, "out_of_area": 422,
        "rate_limited": 429, "upstream_busy": 503, "unavailable": 503,
    }
