"""A location typed as a ZIP code: answered from the table in search.py, never from GeoSearch,
which has no ZIP code index and once sent "10456" (the Bronx) to a Queens house number."""
import re

import pytest

from conftest import ROOT, geosearch_body, load_module


@pytest.fixture(scope="module")
def builder():
    return load_module(ROOT / "scripts" / "build_zip_centers.py", "place_zip_builder_for_tests")


@pytest.mark.parametrize("typed,code", [
    ("10456", "10456"),
    (" 10456 ", "10456"),
    ("10456, Bronx", "10456"),
    ("Bronx, NY 10456", "10456"),
    ("the Bronx 10456", "10456"),
    ("10456 New York NY", "10456"),
    ("zip 10456", "10456"),
    ("ZIP code: 10456", "10456"),
    ("10456-1234", "10456"),
    ("10502", "10502"),
])
def test_a_bare_zip_code_is_recognized(search_mod, typed, code):
    assert search_mod.zip_code_in(typed) == code


@pytest.mark.parametrize("typed", [
    "1000 Grand Concourse, Bronx, NY 10456",
    "350 5th Ave",
    "104-56 Queens Blvd",
    "1045",
    "104567",
    "10456 10457",
    "Williamsburg",
    "11374 Queens Blvd",
])
def test_anything_more_than_a_zip_code_is_not(search_mod, typed):
    assert search_mod.zip_code_in(typed) is None


@pytest.mark.parametrize("typed", ["10456", "10456, Bronx", "Bronx, NY 10456", "zip 10456-1234"])
def test_a_zip_code_needs_no_geocoder_call(world, typed):
    resp = world.post({"what": "pizza", "where": typed})
    assert resp.status == 200
    assert resp.json["location"] == "ZIP 10456, Bronx"
    assert resp.json["center"] == {"lat": 40.83107, "lng": -73.90779}
    assert world.geosearch.count() == 0
    assert world.store.count("geocode:site") == 0
    assert not any(name in ("geocode_get", "geocode_put", "neighborhood") for name, _ in world.store.calls)


def test_a_wrong_answer_already_cached_for_a_zip_code_is_not_used(world, search_mod):
    queens = search_mod.Geocode(found=True, lat=40.7293, lng=-73.862, label="104-56 ...")
    world.store.geocodes["10456"] = (queens, world.store.clock.wall)
    resp = world.post({"what": "pizza", "where": "10456"})
    assert resp.json["location"] == "ZIP 10456, Bronx"


def test_a_zip_code_outside_the_table_is_not_found_without_the_geocoder(world):
    resp = world.post({"what": "pizza", "where": "10502"})
    assert resp.status == 404 and resp.code == "not_found"
    assert resp.json["error"]["message"].startswith("Couldn't find ZIP code 10502 in New York City.")
    assert world.geosearch.count() == 0
    assert not any(name == "search_places" for name, _ in world.store.calls)


def test_an_address_with_a_zip_code_still_goes_to_the_geocoder(world):
    world.geosearch.script((200, geosearch_body("1000 GRAND CONCOURSE, Bronx, NY, USA", 40.8297, -73.9208)))
    resp = world.post({"what": "pizza", "where": "1000 Grand Concourse, Bronx, NY 10456"})
    assert resp.status == 200
    assert resp.json["location"] == "1000 GRAND CONCOURSE, Bronx, NY, USA"
    assert world.geosearch.count() == 1


def test_the_not_found_message_mentions_zip_codes(world):
    world.geosearch.script((200, '{"features": []}'))
    resp = world.post({"what": "pizza", "where": "123 Nowhere Lane"})
    assert resp.code == "not_found"
    assert "a ZIP code" in resp.json["error"]["message"]


# ------------------------------------------------------------------ the table


@pytest.mark.parametrize("code,borough,lat,lng", [
    ("10456", "Bronx", 40.831, -73.908),     # Morrisania and Claremont
    ("11232", "Brooklyn", 40.656, -74.005),  # Sunset Park
    ("10001", "Manhattan", 40.748, -73.991),  # Chelsea
    ("11374", "Queens", 40.729, -73.862),    # Rego Park
    ("10314", "Staten Island", 40.607, -74.162),
])
def test_the_table_puts_known_zip_codes_in_the_right_place(search_mod, code, borough, lat, lng):
    got_lat, got_lng, got_borough = search_mod.ZIP_CENTERS[code]
    assert got_borough == borough
    assert abs(got_lat - lat) < 0.01 and abs(got_lng - lng) < 0.01


def test_every_table_row_is_in_nyc_and_in_its_prefix_borough(search_mod, builder):
    assert len(search_mod.ZIP_CENTERS) > 200
    for code, (lat, lng, borough) in search_mod.ZIP_CENTERS.items():
        assert re.fullmatch(r"[0-9]{5}", code)
        assert 40.49 <= lat <= 40.92 and -74.26 <= lng <= -73.70
        assert borough == builder.expected_borough(code)
    assert "10502" not in search_mod.ZIP_CENTERS  # Ardsley, outside the city


def test_the_builder_keeps_only_well_supported_zip_codes_in_their_borough(builder):
    many = [(40.83, -73.91)] * builder.MIN_PLACES
    points = {
        "10456": many,                                # enough places, Bronx prefix, Bronx center: kept
        "10457": many[:-1],                           # one place short: dropped
        "10163": [(40.69, -73.94)] * 30,              # Manhattan prefix but a Brooklyn center: dropped
        "07030": many,                                # not an NYC prefix: dropped
    }
    borough_of = lambda lat, lng: "Brooklyn" if lng > -73.95 and lat < 40.7 else "Bronx"  # noqa: E731
    assert builder.centers(points, borough_of) == {"10456": (40.83, -73.91, "Bronx")}


def test_the_builder_block_round_trips_and_replaces_itself(builder):
    block = builder.render({"10456": (40.831071, -73.907789, "Bronx"), "10314": (40.6, -74.16, "Staten Island")}, "R1")
    namespace = {}
    exec(block, namespace)  # the generated block is the same code search.py runs
    assert namespace["ZIP_CENTERS"] == {"10456": (40.83107, -73.90779, "Bronx"), "10314": (40.6, -74.16, "Staten Island")}
    source = "x = 1\n"
    once = builder.replace_block(source, block)
    assert builder.replace_block(once, block) == once
    assert once.startswith("x = 1\n") and once.count(builder.BEGIN) == 1


def test_damaged_markers_stop_the_builder(builder):
    with pytest.raises(SystemExit):
        builder.replace_block(f"{builder.END}\n{builder.BEGIN}\n", "block")
