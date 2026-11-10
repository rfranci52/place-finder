"""The place list: nearest first, at most 60, safe links, readable labels."""
from conftest import NEAR_THE_PARK, place


def test_nearest_first_within_the_radius(world):
    resp = world.post(NEAR_THE_PARK)
    names = [p["name"] for p in resp.json["places"]]
    assert names == ["Cafe Uno", "Bean Bar"]  # Far Away Cafe is outside 1.5 km
    distances = [p["distance_m"] for p in resp.json["places"]]
    assert distances == sorted(distances)
    wide = world.post({**NEAR_THE_PARK, "radius": 2500})
    assert "Far Away Cafe" not in [p["name"] for p in wide.json["places"]]


def test_at_most_60_and_truncated_when_there_are_more(world):
    world.store.places = [place(100 + i, f"Cafe {i:03d}", "cafe", 40.7307 + i * 0.00001, -73.9957) for i in range(75)]
    resp = world.post(NEAR_THE_PARK)
    assert len(resp.json["places"]) == 60 and resp.json["truncated"] is True
    assert [p["name"] for p in resp.json["places"]][:3] == ["Cafe 000", "Cafe 001", "Cafe 002"]
    search_call = [args for name, args in world.store.calls if name == "search_places"][-1]
    assert search_call[-1] == 61  # one more than shown, to know whether there are more


def test_exactly_60_is_not_truncated(world):
    world.store.places = [place(100 + i, f"Cafe {i:03d}", "cafe", 40.7307, -73.9957) for i in range(60)]
    assert world.post(NEAR_THE_PARK).json["truncated"] is False


def test_place_shape(world):
    first = world.post(NEAR_THE_PARK).json["places"][0]
    assert first == {
        "id": "00000000-0000-4000-8000-000000000001", "name": "Cafe Uno", "category": "Coffee shop",
        "address": "1 Test St, New York 10012", "phone": "+12125550100", "website": "https://cafe-uno.example",
        "borough": "Manhattan", "lat": 40.731, "lng": -73.997, "distance_m": first["distance_m"],
    }


def test_websites_are_https_only(search_mod):
    assert search_mod.https_url("https://good.example/menu?x=1") == "https://good.example/menu?x=1"
    assert search_mod.https_url("HTTPS://Upper.Example/A") == "https://Upper.Example/A"
    for bad in ("http://plain.example", "javascript:alert(1)", "www.no-scheme.example", "https://user:pw@evil.example",
                "https://ok.example/<script>", "data:text/html,hi", "https:///no-host", "", None, 7):
        assert search_mod.https_url(bad) == ""


def test_category_labels(search_mod):
    assert search_mod.category_label("coffee_shop") == "Coffee shop"
    assert search_mod.category_label("atm") == "ATM (cash machine)"
    assert search_mod.category_label("doctors_office") == "Doctor's office"
    assert search_mod.category_label(None) == "Place"


def test_missing_parts_leave_no_stray_commas(search_mod):
    row = ("id", "Name", "cafe", None, None, None, None, None, None, 40.7, -74.0, 12.4)
    p = search_mod.place_from_row(row)
    assert (p["address"], p["phone"], p["website"], p["borough"], p["distance_m"]) == ("", "", "", "", 12)
