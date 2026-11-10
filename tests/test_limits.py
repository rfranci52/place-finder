"""Daily limits: searches (generous) and geocoder calls (smaller), per visitor and site-wide. The
database is required; without it nothing runs."""
from conftest import ADDRESS, NEAR_THE_PARK, SALT


def address(n):
    return {"what": "coffee", "where": f"{n} Broadway"}


def test_defaults(search_mod):
    settings = search_mod.load_settings({})
    assert (settings.searches_per_visitor, settings.searches_per_day) == (300, 20_000)
    assert (settings.geocodes_per_visitor, settings.geocodes_per_day) == (30, 1_000)


def test_every_search_counts_once(world):
    for _ in range(3):
        assert world.post(NEAR_THE_PARK).status == 200
    assert world.store.count("search:site") == 3


def test_the_search_limit_per_visitor(world):
    world.env(PLACES_SEARCHES_PER_VISITOR=2)
    world.post(NEAR_THE_PARK)
    world.post(NEAR_THE_PARK)
    third = world.post(NEAR_THE_PARK)
    assert third.status == 429 and third.code == "rate_limited"
    assert "2 searches today" in third.json["error"]["message"]
    assert int(third.headers["Retry-After"]) == third.json["error"]["retry_after"] > 0
    assert world.post(NEAR_THE_PARK, ip="198.51.100.9").status == 200  # someone else


def test_the_site_search_limit(world):
    world.env(PLACES_SEARCHES_PER_DAY=1)
    world.post(NEAR_THE_PARK)
    resp = world.post(NEAR_THE_PARK, ip="198.51.100.9")
    assert resp.code == "rate_limited" and "daily search limit" in resp.json["error"]["message"]
    # Refused by the site limit, so it does not count against that visitor.
    assert world.store.count("search:v:" + world.mod.visitor_key("198.51.100.9", SALT)) == 0


def test_geocoder_calls_have_their_own_limits(world):
    world.env(PLACES_GEOCODES_PER_VISITOR=2)
    assert world.post(address(1)).status == 200
    assert world.post(address(1)).status == 200  # cached: no geocoder call, no geocode slot
    assert world.post(address(2)).status == 200
    resp = world.post(address(3))
    assert resp.code == "rate_limited" and "2 new locations" in resp.json["error"]["message"]
    assert world.geosearch.count() == 2
    # Neighborhood names and map positions still work.
    assert world.post(NEAR_THE_PARK).status == 200
    assert world.post({"what": "coffee", "lat": 40.7307, "lng": -73.9957}).status == 200


def test_the_site_geocoder_limit(world):
    world.env(PLACES_GEOCODES_PER_DAY=1)
    world.post(address(1))
    resp = world.post(address(2), ip="198.51.100.9")
    assert resp.code == "rate_limited" and "address lookup service" in resp.json["error"]["message"]
    assert world.geosearch.count() == 1


def test_visitors_are_separate_and_an_ipv6_64_is_one_visitor(world):
    world.env(PLACES_SEARCHES_PER_VISITOR=1)
    assert world.post(NEAR_THE_PARK, ip="2001:db8:1:2::1").status == 200
    assert world.post(NEAR_THE_PARK, ip="2001:db8:1:2:ffff::9").code == "rate_limited"
    assert world.post(NEAR_THE_PARK, ip="2001:db8:1:3::1").status == 200


def test_only_a_salted_hash_of_the_address_reaches_the_database(world, search_mod):
    world.post(NEAR_THE_PARK, ip="203.0.113.77")
    assert "203.0.113.77" not in repr(world.store.calls)
    (kind, visitor, _, _), = [args for name, args in world.store.calls if name == "take_slot"]
    assert visitor == search_mod.visitor_key("203.0.113.77", SALT)
    assert visitor != search_mod.visitor_key("203.0.113.77", "another-salt")


def test_first_forwarded_address_is_the_visitor(world, search_mod):
    world.post(NEAR_THE_PARK, ip="203.0.113.5, 10.0.0.1")
    (_, visitor, _, _), = [args for name, args in world.store.calls if name == "take_slot"]
    assert visitor == search_mod.visitor_key("203.0.113.5", SALT)


def test_unreachable_database_fails_closed(world):
    world.env(PLACES_DB_URL="unreachable")
    resp = world.post(ADDRESS)
    assert resp.status == 503 and resp.code == "unavailable"
    assert world.geosearch.count() == 0


def test_missing_settings_fail_closed(world):
    for missing in ("PLACES_DB_URL", "PLACES_HASH_SALT"):
        world.env(PLACES_DB_URL="postgresql://fake", PLACES_HASH_SALT=SALT)
        world.env(**{missing: None})
        assert world.post(ADDRESS).code == "unavailable"
    assert world.geosearch.count() == 0


def test_a_limit_check_that_errors_is_a_refusal(world):
    world.store.fail_on = {"take_slot"}
    assert world.post(ADDRESS).code == "unavailable"
    assert world.geosearch.count() == 0


def test_a_failed_cache_write_still_answers(world):
    world.store.fail_on = {"geocode_put"}
    assert world.post(ADDRESS).status == 200


def test_zero_limits_refuse(world):
    world.env(PLACES_SEARCHES_PER_VISITOR=0)
    assert world.post(NEAR_THE_PARK).code == "rate_limited"


def test_limit_settings_are_clamped(search_mod):
    settings = search_mod.load_settings({"PLACES_SEARCHES_PER_VISITOR": "-5", "PLACES_GEOCODES_PER_DAY": "nope"})
    assert settings.searches_per_visitor == 0
    assert settings.geocodes_per_day == search_mod.GEOCODES_PER_DAY
