"""Overpass: [timeout:10], one retry on the documented mirror after a busy answer, and a 30 second
pause for the instance that refused."""
import pytest

from conftest import COFFEE_NEAR_PARK, DEFAULT_ELEMENTS, overpass_body


def test_the_query_asks_for_a_ten_second_timeout(world):
    world.post(COFFEE_NEAR_PARK)
    assert world.upstream.calls[-1].query.startswith("[out:json][timeout:10];")


def test_the_mirror_is_the_documented_private_coffee_instance(search_mod):
    assert search_mod.OVERPASS_URL == "https://overpass-api.de/api/interpreter"
    assert search_mod.OVERPASS_MIRROR_URL == "https://overpass.private.coffee/api/interpreter"
    source = open(search_mod.__file__, encoding="utf-8").read()
    assert "wiki.openstreetmap.org/wiki/Overpass_API#Public_Overpass_API_instances" in source


@pytest.mark.parametrize("status", [429, 504, 502, 503, None])
def test_busy_main_instance_is_retried_once_on_the_mirror(world, status):
    world.upstream.script("overpass", (status, b""))
    resp = world.post(COFFEE_NEAR_PARK)
    assert resp.status == 200 and len(resp.json["places"]) == 2
    assert world.upstream.count("overpass") == 1
    assert world.upstream.count("overpass-mirror") == 1
    assert ("pause_upstream", ("overpass", 30)) in world.store.calls
    # The mirror got the same query.
    main, mirror = [c for c in world.upstream.calls if c.service.startswith("overpass")]
    assert main.query == mirror.query


def test_a_runtime_error_remark_counts_as_busy(world):
    world.upstream.script("overpass", (200, overpass_body([], remark="runtime error: Query timed out in \"query\"")))
    assert world.post(COFFEE_NEAR_PARK).status == 200
    assert world.upstream.count("overpass-mirror") == 1


def test_an_error_page_with_status_200_counts_as_busy(world):
    world.upstream.script("overpass", (200, b"<html>Dispatcher_Client::request_read_and_idx::timeout</html>"))
    assert world.post(COFFEE_NEAR_PARK).status == 200
    assert world.upstream.count("overpass-mirror") == 1


def test_both_busy_is_upstream_busy_and_nothing_is_cached(world):
    world.upstream.script("overpass", (429, b""))
    world.upstream.script("overpass-mirror", (429, b""))
    resp = world.post(COFFEE_NEAR_PARK)
    assert resp.status == 503 and resp.code == "upstream_busy"
    assert world.upstream.count("overpass") + world.upstream.count("overpass-mirror") == 2
    assert world.store.results == {}


def test_a_bad_request_is_not_retried(world):
    world.upstream.script("overpass", (400, b"parse error"))
    assert world.post(COFFEE_NEAR_PARK).code == "upstream_busy"
    assert world.upstream.count("overpass-mirror") == 0


def test_after_a_refusal_the_main_instance_is_left_alone_for_30_seconds(world):
    world.upstream.script("overpass", (429, b""))
    world.post(COFFEE_NEAR_PARK)
    world.clock.advance(10)
    world.post({"what": "pizza", "where": COFFEE_NEAR_PARK["where"]})
    assert world.upstream.count("overpass") == 1  # skipped during the pause
    assert world.upstream.count("overpass-mirror") == 2
    world.clock.advance(25)
    world.post({"what": "bakery", "where": COFFEE_NEAR_PARK["where"]})
    assert world.upstream.count("overpass") == 2


def test_no_time_left_means_no_mirror_attempt(world, search_mod, monkeypatch):
    monkeypatch.setattr(search_mod, "REQUEST_BUDGET_S", 5)
    original = world.upstream.__call__

    def slow(url, **kwargs):
        reply = original(url, **kwargs)
        world.clock.advance(3)
        return reply

    world.upstream.script("overpass", (504, b""))
    monkeypatch.setattr(search_mod, "http_request", slow)
    resp = world.post({"what": "coffee", "lat": 40.7, "lng": -74.0})
    assert resp.code == "upstream_busy"
    assert world.upstream.count("overpass-mirror") == 0


def test_mirror_url_can_be_configured(world):
    world.env(PLACES_OVERPASS_MIRROR_URL="https://mirror.example.test/api/interpreter")
    world.upstream.script("overpass", (429, b""))
    world.post(COFFEE_NEAR_PARK)
    assert world.upstream.calls[-1].url == "https://mirror.example.test/api/interpreter"


def test_elements_from_the_mirror_are_used(world):
    world.upstream.script("overpass", (504, b""))
    world.upstream.script("overpass-mirror", (200, overpass_body(DEFAULT_ELEMENTS[:1])))
    resp = world.post(COFFEE_NEAR_PARK)
    assert [p["name"] for p in resp.json["places"]] == ["Cafe Uno"]
