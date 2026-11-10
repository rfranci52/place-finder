"""Cached answers cost no upstream call, and expire on time."""
from datetime import timedelta

from conftest import COFFEE_NEAR_PARK


def test_first_search_is_live_and_the_repeat_is_free(world):
    first = world.post(COFFEE_NEAR_PARK)
    assert first.status == 200 and first.json["cached"] is False
    assert world.upstream.count("nominatim") == 1 and world.upstream.count("overpass") == 1
    second = world.post(COFFEE_NEAR_PARK)
    assert second.status == 200 and second.json["cached"] is True
    assert second.json["places"] == first.json["places"]
    assert world.upstream.count() == 2


def test_cached_hits_take_no_daily_allowance(world):
    world.post(COFFEE_NEAR_PARK)
    counts = dict(world.store.counts)
    for _ in range(5):
        assert world.post(COFFEE_NEAR_PARK).json["cached"] is True
    assert world.store.counts == counts
    assert not any(name in ("begin_live_search", "take_upstream_slot") for name, _ in world.store.calls[-10:])


def test_geocode_key_ignores_case_spacing_and_comma_style(world):
    world.post({"what": "coffee", "where": "Washington Square Park, New York"})
    for where in ("washington square park,new york", "  WASHINGTON   Square Park ,  New York  "):
        resp = world.post({"what": "coffee", "where": where})
        assert resp.json["cached"] is True
    assert world.upstream.count("nominatim") == 1
    assert list(world.store.geocodes) == ["washington square park, new york"]
    # Nominatim is sent the key itself, so the cached answer belongs to exactly that key.
    assert world.upstream.calls[0].params["q"] == "washington square park, new york"


def test_geocodes_live_30_days(world):
    world.post(COFFEE_NEAR_PARK)
    world.clock.advance(timedelta(days=29).total_seconds())
    world.post({"what": "pizza", "where": COFFEE_NEAR_PARK["where"]})
    assert world.upstream.count("nominatim") == 1
    world.clock.advance(timedelta(days=2).total_seconds())
    world.post({"what": "bakery", "where": COFFEE_NEAR_PARK["where"]})
    assert world.upstream.count("nominatim") == 2


def test_place_lists_live_24_hours(world):
    world.post(COFFEE_NEAR_PARK)
    world.clock.advance(timedelta(hours=23).total_seconds())
    assert world.post(COFFEE_NEAR_PARK).json["cached"] is True
    world.clock.advance(timedelta(hours=2).total_seconds())
    resp = world.post(COFFEE_NEAR_PARK)
    assert resp.json["cached"] is False
    assert world.upstream.count("overpass") == 2
    assert world.upstream.count("nominatim") == 1  # the geocode is still fresh


def test_results_key_is_term_rounded_point_and_radius(world):
    world.post({"what": "coffee", "lat": 40.73049, "lng": -73.99749})
    # Same 0.001 degree cell: a hit.
    assert world.post({"what": "cafe", "lat": 40.73010, "lng": -73.99710}).json["cached"] is True
    # Next cell over, a different radius, or a different term: misses.
    assert world.post({"what": "coffee", "lat": 40.7316, "lng": -73.9975}).json["cached"] is False
    assert world.post({"what": "coffee", "lat": 40.7306, "lng": -73.9976, "radius": 500}).json["cached"] is False
    assert world.post({"what": "pizza", "lat": 40.7306, "lng": -73.9976}).json["cached"] is False
    assert set(world.store.results) == {
        ("c:coffee", 40730, -73997, 1500),
        ("c:coffee", 40732, -73998, 1500),
        ("c:coffee", 40731, -73998, 500),
        ("c:pizza", 40731, -73998, 1500),
    }
    assert world.upstream.count("overpass") == 4


def test_the_query_runs_around_the_rounded_point(world):
    resp = world.post({"what": "coffee", "lat": 40.73049, "lng": -73.99749})
    assert resp.json["center"] == {"lat": 40.73, "lng": -73.997}
    assert "(around:1500,40.730,-73.997)" in world.upstream.calls[-1].query


def test_not_found_is_cached_too(world):
    world.upstream.script("nominatim", (200, b"[]"))
    first = world.post({"what": "coffee", "where": "zzqqxx nowhere"})
    assert first.status == 404 and first.code == "not_found"
    second = world.post({"what": "coffee", "where": "ZZQQXX   nowhere"})
    assert second.code == "not_found"
    assert world.upstream.count() == 1


def test_a_failed_cache_write_still_returns_the_results(world):
    world.store.fail_on = {"results_put", "geocode_put"}
    resp = world.post(COFFEE_NEAR_PARK)
    assert resp.status == 200 and len(resp.json["places"]) == 2
    assert resp.json["cached"] is False


def test_partial_results_are_never_cached(world):
    from conftest import DEFAULT_ELEMENTS, overpass_body

    world.upstream.script("overpass", (200, overpass_body(DEFAULT_ELEMENTS[:1], remark="runtime error: Query timed out")))
    world.upstream.script("overpass-mirror", (200, overpass_body(DEFAULT_ELEMENTS)))
    resp = world.post(COFFEE_NEAR_PARK)
    assert len(resp.json["places"]) == 2
    (places, _, _), = world.store.results.values()
    assert len(places) == 2
