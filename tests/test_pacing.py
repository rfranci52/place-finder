"""Nominatim: at most one call per second across instances, an identifying User-Agent, and a pause
after a refusal."""
from conftest import nominatim_body


def where(n):
    return {"what": "coffee", "where": f"Test Place {n}"}


def test_back_to_back_geocodes_start_at_least_a_second_apart(world):
    for n in range(4):
        assert world.post(where(n)).status == 200
    starts = [c.at for c in world.upstream.calls if c.service == "nominatim"]
    assert len(starts) == 4
    assert all(b - a >= 1.0 for a, b in zip(starts, starts[1:]))


def test_the_wait_comes_from_the_database_reservation(world):
    world.post(where(1))
    world.post(where(2))
    reservations = [args for name, args in world.store.calls if name == "reserve_call" and args[0] == "nominatim"]
    assert reservations == [("nominatim", 3000), ("nominatim", 3000)]
    assert world.clock.sleeps and abs(world.clock.sleeps[0] - 1.1) < 1e-9


def test_a_long_queue_is_busy_not_a_long_wait(world):
    world.store.next_at["nominatim"] = world.clock.monotonic() + 10  # other instances queued ahead
    resp = world.post(where(1))
    assert resp.status == 503 and resp.code == "upstream_busy"
    assert world.upstream.count("nominatim") == 0
    assert resp.headers["Retry-After"] == "10"


def test_a_refusal_pauses_nominatim_for_30_seconds(world):
    world.upstream.script("nominatim", (429, b""))
    assert world.post(where(1)).code == "upstream_busy"
    assert ("pause_upstream", ("nominatim", 30)) in world.store.calls
    world.clock.advance(20)
    assert world.post(where(2)).code == "upstream_busy"
    assert world.upstream.count("nominatim") == 1  # no call during the pause
    world.clock.advance(15)
    assert world.post(where(3)).status == 200
    assert world.upstream.count("nominatim") == 2


def test_user_agent_defaults_to_the_project_and_names_no_person(world, search_mod):
    world.post(where(1))
    agents = {c.user_agent for c in world.upstream.calls}
    assert agents == {"place-finder-demo/1.0 (portfolio project)"}
    assert search_mod.DEFAULT_USER_AGENT == "place-finder-demo/1.0 (portfolio project)"


def test_user_agent_comes_from_the_environment(world):
    world.env(PLACES_USER_AGENT="place-finder-demo/1.0 (+https://example.org/contact)")
    world.post(where(1))
    assert {c.user_agent for c in world.upstream.calls} == {"place-finder-demo/1.0 (+https://example.org/contact)"}


def test_a_malformed_user_agent_falls_back_to_the_default(search_mod):
    for bad in ("", "x" * 300, "bad\nheader", "café/1.0"):
        assert search_mod.load_settings({"PLACES_USER_AGENT": bad}).user_agent == search_mod.DEFAULT_USER_AGENT


def test_nominatim_url_can_be_switched_without_a_code_change(world):
    world.env(PLACES_NOMINATIM_URL="https://nominatim.example.test/search")
    world.post(where(1))
    assert world.upstream.calls[0].url.startswith("https://nominatim.example.test/search?q=test+place+1")


def test_nominatim_request_shape(world):
    world.post(where(1))
    call = world.upstream.calls[0]
    assert call.params == {"q": "test place 1", "format": "jsonv2", "limit": "1", "accept-language": "en"}
    assert call.form is None


def test_a_garbled_geocode_is_busy_and_not_cached(world):
    world.upstream.script("nominatim", (200, b"<html>oops</html>"), (200, nominatim_body()))
    assert world.post(where(1)).code == "upstream_busy"
    assert world.store.geocodes == {}
    assert world.post(where(1)).status == 200
