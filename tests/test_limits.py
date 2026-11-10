"""Daily limits: per visitor on searches that need live data, and site-wide on upstream calls.
The database is required; without it nothing runs."""
from conftest import COFFEE_NEAR_PARK, SALT, nominatim_body


def live(n):
    return {"what": "coffee", "where": f"Somewhere {n}"}


def test_visitor_cap_counts_only_live_searches(world):
    world.env(PLACES_VISITOR_DAILY=2)
    assert world.post(live(1)).status == 200
    assert world.post(live(1)).json["cached"] is True  # free, does not count
    assert world.post(live(2)).status == 200
    calls = world.upstream.count()
    third = world.post(live(3))
    assert third.status == 429 and third.code == "rate_limited"
    assert "the limit for one visitor" in third.json["error"]["message"]
    assert world.upstream.count() == calls  # refused before any upstream call
    assert int(third.headers["Retry-After"]) == third.json["error"]["retry_after"] > 0
    # Cached searches still work after the cap.
    assert world.post(live(1)).status == 200


def test_visitor_is_checked_before_the_site_allowance(world):
    world.env(PLACES_VISITOR_DAILY=1)
    world.post(live(1))
    site_before = world.store.count("upstream")
    world.post(live(2))
    assert world.store.count("upstream") == site_before


def test_site_cap_counts_every_upstream_call(world):
    world.env(PLACES_UPSTREAM_DAILY=3, PLACES_VISITOR_DAILY=50)
    assert world.post(live(1)).status == 200  # geocode + overpass = 2
    world.upstream.script("nominatim", (200, nominatim_body(lat=41.0, lng=-74.0)))
    resp = world.post(live(2))  # geocode = 3, then overpass is refused
    assert resp.code == "rate_limited"
    assert "today's share" in resp.json["error"]["message"]
    assert world.store.count("upstream") == 3
    assert world.upstream.count() == 3
    # Another visitor is refused too, and it does not use up that visitor's allowance.
    other = world.post(live(3), ip="198.51.100.20")
    assert other.code == "rate_limited"
    other_key = world.mod.visitor_key("198.51.100.20", SALT)
    assert world.store.count("v:" + other_key) == 0


def test_the_mirror_retry_takes_a_site_slot(world):
    world.upstream.script("overpass", (429, b""))
    world.post(COFFEE_NEAR_PARK)
    assert world.store.count("upstream") == 3  # geocode, main, mirror
    assert [n for n, _ in world.store.calls].count("take_upstream_slot") == 2


def test_visitors_are_separate_and_an_ipv6_64_is_one_visitor(world):
    world.env(PLACES_VISITOR_DAILY=1)
    assert world.post(live(1), ip="203.0.113.1").status == 200
    assert world.post(live(2), ip="203.0.113.2").status == 200
    assert world.post(live(3), ip="2001:db8:1:2::1").status == 200
    assert world.post(live(4), ip="2001:db8:1:2:ffff::9").code == "rate_limited"
    assert world.post(live(5), ip="2001:db8:1:3::1").status == 200


def test_only_a_salted_hash_of_the_address_reaches_the_database(world, search_mod):
    world.post(live(1), ip="203.0.113.77")
    (visitor, _, _), = [args for name, args in world.store.calls if name == "begin_live_search"]
    assert "203.0.113.77" not in repr(world.store.calls)
    assert visitor == search_mod.visitor_key("203.0.113.77", SALT)
    assert visitor != search_mod.visitor_key("203.0.113.77", "another-salt")
    assert len(visitor) == 32 and all(c in "0123456789abcdef" for c in visitor)


def test_first_forwarded_address_is_the_visitor(world, search_mod):
    world.post(live(1), ip="203.0.113.5, 10.0.0.1")
    (visitor, _, _), = [args for name, args in world.store.calls if name == "begin_live_search"]
    assert visitor == search_mod.visitor_key("203.0.113.5", SALT)


def test_unreachable_database_fails_closed(world):
    world.env(PLACES_DB_URL="unreachable")
    resp = world.post(COFFEE_NEAR_PARK)
    assert resp.status == 503 and resp.code == "unavailable"
    assert "database" in resp.json["error"]["message"]
    assert world.upstream.count() == 0


def test_missing_settings_fail_closed(world):
    for missing in ("PLACES_DB_URL", "PLACES_HASH_SALT"):
        world.env(PLACES_DB_URL="postgresql://fake", PLACES_HASH_SALT=SALT)
        world.env(**{missing: None})
        resp = world.post(COFFEE_NEAR_PARK)
        assert resp.code == "unavailable"
    assert world.upstream.count() == 0


def test_a_limit_check_that_errors_is_a_refusal(world):
    world.store.fail_on = {"begin_live_search"}
    resp = world.post(COFFEE_NEAR_PARK)
    assert resp.code == "unavailable"
    assert world.upstream.count() == 0


def test_a_pacing_check_that_errors_is_a_refusal(world):
    world.store.fail_on = {"reserve_call"}
    assert world.post(COFFEE_NEAR_PARK).code == "unavailable"
    assert world.upstream.count() == 0


def test_a_cache_read_that_errors_is_a_refusal(world):
    world.store.fail_on = {"geocode_get"}
    assert world.post(COFFEE_NEAR_PARK).code == "unavailable"
    assert world.upstream.count() == 0


def test_zero_caps_refuse_everything_live(world):
    world.env(PLACES_VISITOR_DAILY=0)
    assert world.post(live(1)).code == "rate_limited"
    world.env(PLACES_VISITOR_DAILY=5, PLACES_UPSTREAM_DAILY=0)
    assert world.post(live(1)).code == "rate_limited"
    assert world.upstream.count() == 0


def test_limit_settings_are_clamped(search_mod):
    settings = search_mod.load_settings({"PLACES_VISITOR_DAILY": "-5", "PLACES_UPSTREAM_DAILY": "nope"})
    assert settings.visitor_daily == 0
    assert settings.upstream_daily == search_mod.UPSTREAM_DAILY_DEFAULT
    assert search_mod.load_settings({}).visitor_daily == 10
    assert search_mod.load_settings({}).upstream_daily == 100
