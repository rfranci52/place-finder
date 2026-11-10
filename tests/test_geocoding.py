"""Typed locations: NYC neighborhood names first, then NYC GeoSearch, cached; and the five-borough
boundary."""
from datetime import timedelta

import pytest

from conftest import ADDRESS, NEAR_THE_PARK, geosearch_body


# ------------------------------------------------------------------ neighborhoods


def test_a_neighborhood_name_needs_no_geocoder_call(world):
    resp = world.post(NEAR_THE_PARK)
    assert resp.status == 200
    assert resp.json["location"] == "Greenwich Village, Manhattan"
    assert resp.json["center"] == {"lat": 40.7307, "lng": -73.9957}
    assert world.geosearch.count() == 0
    assert world.store.count("geocode:site") == 0


@pytest.mark.parametrize("typed,label", [
    ("greenwich village", "Greenwich Village, Manhattan"),
    ("Greenwich Village, New York, NY", "Greenwich Village, Manhattan"),
    ("Williamsburg, Brooklyn", "Williamsburg, Brooklyn"),
    ("Chelsea", "Chelsea, Manhattan"),
    ("Chelsea, Staten Island", "Chelsea, Staten Island"),
    ("Brooklyn", "Brooklyn"),
])
def test_neighborhood_names_borough_hints_and_city_words(world, typed, label):
    resp = world.post({"what": "coffee", "where": typed})
    assert resp.json["location"] == label
    assert world.geosearch.count() == 0


def test_candidates_strip_city_words_and_read_a_borough(search_mod):
    assert list(search_mod.neighborhood_candidates("Williamsburg, Brooklyn, NY")) == [
        ("williamsburg brooklyn", None), ("williamsburg", 3)]
    assert list(search_mod.neighborhood_candidates("Astoria NYC")) == [("astoria", None)]
    assert list(search_mod.neighborhood_candidates("the Bronx")) == [("the bronx", None), ("the", 2)]


# ------------------------------------------------------------------ GeoSearch


def test_an_address_goes_to_geosearch_once_and_is_cached(world):
    first = world.post(ADDRESS)
    assert first.status == 200 and first.json["location"] == "350 5TH AVE, New York, NY, USA"
    assert world.geosearch.count() == 1
    again = world.post({"what": "pizza", "where": "  350 5TH ave "})
    assert again.status == 200
    assert world.geosearch.count() == 1


def test_the_request_shape(world, search_mod):
    world.post(ADDRESS)
    call, = world.geosearch.calls
    assert call.url.startswith("https://geosearch.planninglabs.nyc/v2/search?")
    assert call.params == {"text": "350 5th ave", "size": "1"}
    assert call.timeout == search_mod.GEOSEARCH_TIMEOUT_S


def test_the_geocoder_url_can_be_switched_without_a_code_change(world):
    world.env(PLACES_GEOSEARCH_URL="https://geocoder.example.test/v2/search")
    world.post(ADDRESS)
    assert world.geosearch.calls[0].url.startswith("https://geocoder.example.test/v2/search?")


def test_cached_geocodes_live_30_days(world):
    world.post(ADDRESS)
    world.clock.advance(timedelta(days=29).total_seconds())
    world.post(ADDRESS)
    assert world.geosearch.count() == 1
    world.clock.advance(timedelta(days=2).total_seconds())
    world.post(ADDRESS)
    assert world.geosearch.count() == 2


def test_no_match_is_not_found_and_cached(world):
    world.geosearch.script((200, b'{"type": "FeatureCollection", "features": []}'))
    first = world.post({"what": "coffee", "where": "Hoboken NJ"})
    assert first.status == 404 and first.code == "not_found"
    assert "in New York City" in first.json["error"]["message"]
    assert world.post({"what": "coffee", "where": "hoboken nj"}).code == "not_found"
    assert world.geosearch.count() == 1


def test_a_fuzzy_street_match_for_a_name_is_not_found(world):
    # What GeoSearch really answered for "Greenwich Village" before neighborhoods were local.
    world.store.neighborhoods.clear()
    world.geosearch.script((200, geosearch_body("37 GREENWICH STREET, New York, NY, USA", 40.705786, -74.013961)))
    resp = world.post({"what": "coffee", "where": "Greenwich Village"})
    assert resp.code == "not_found"


@pytest.mark.parametrize("query,label,expected", [
    ("Greenwich Village", "37 GREENWICH STREET, New York, NY, USA", False),
    ("350 5th Ave", "350 5 AVENUE, New York, NY, USA", True),
    ("Washington Square Park", "WASHINGTON SQUARE PARK, New York, NY, USA", True),
    ("Times Sq", "TIMES SQUARE, New York, NY, USA", True),
    ("NYC", "NYC", False),
    ("Manhattan", "MANHATTAN AVENUE, Brooklyn, NY, USA", False),
])
def test_geocode_word_check(search_mod, query, label, expected):
    assert search_mod.geocode_matches_text(query, label) is expected


def test_low_confidence_is_not_found(world, search_mod):
    world.geosearch.script((200, geosearch_body(confidence=search_mod.MIN_GEOCODE_CONFIDENCE - 0.01)))
    assert world.post(ADDRESS).code == "not_found"


@pytest.mark.parametrize("reply", [(500, b""), (429, b""), (None, b""), (200, b"<html>"), (200, b'{"features": "x"}'),
                                   (200, b'{"features": [{"geometry": {"coordinates": ["a"]}}]}')])
def test_geocoder_trouble_is_upstream_busy_and_not_cached(world, reply):
    world.geosearch.script(reply)
    resp = world.post(ADDRESS)
    assert resp.status == 503 and resp.code == "upstream_busy"
    assert "Search this area" in resp.json["error"]["message"]
    assert world.store.geocodes == {}


def test_the_user_agent_names_the_project(world, search_mod):
    assert search_mod.USER_AGENT == "place-finder-demo/1.0 (portfolio project)"


# ------------------------------------------------------------------ the five boroughs


def test_a_map_position_outside_nyc_is_out_of_area(world):
    resp = world.post({"what": "coffee", "lat": 40.7440, "lng": -74.0324})  # Hoboken
    assert resp.status == 422 and resp.code == "out_of_area"
    assert resp.json["error"]["message"].startswith("The map center is outside New York City.")
    assert not any(name == "search_places" for name, _ in world.store.calls)


def test_a_geocode_outside_nyc_is_out_of_area(world):
    world.geosearch.script((200, geosearch_body("1 HUDSON STREET, Hoboken, NJ, USA", 40.7440, -74.0324)))
    resp = world.post({"what": "coffee", "where": "1 Hudson St Hoboken"})
    assert resp.code == "out_of_area"
    assert "“1 Hudson St Hoboken” is outside New York City" in resp.json["error"]["message"]


def test_inside_nyc_reports_the_borough_and_release(world):
    resp = world.post({"what": "coffee", "lat": 40.7307, "lng": -73.9957})
    assert resp.json["borough"] == "Manhattan"
    assert resp.json["data_release"] == "2026-10-21.0"
