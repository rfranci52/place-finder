"""Turning Overpass elements into the place list: up to 60, nearest first, safe links only."""
from conftest import COFFEE_NEAR_PARK, node, overpass_body, way

CENTER = (40.731, -73.997)


def run(world, elements, what="coffee"):
    world.upstream.script("overpass", (200, overpass_body(elements)))
    resp = world.post({"what": what, "lat": CENTER[0], "lng": CENTER[1]})
    assert resp.status == 200, resp.json
    return resp.json


def test_at_most_60_places_nearest_first(world):
    elements = [node(i, 40.731 + i * 0.0001, -73.997, name=f"Cafe {i:03d}", amenity="cafe") for i in range(1, 120)]
    body = run(world, list(reversed(elements)))
    names = [p["name"] for p in body["places"]]
    assert len(names) == 60
    assert names == [f"Cafe {i:03d}" for i in range(1, 61)]
    distances = [p["distance_m"] for p in body["places"]]
    assert distances == sorted(distances)
    assert body["truncated"] is True


def test_a_full_overpass_page_is_reported_as_truncated(world, search_mod):
    elements = [node(i, 40.731, -73.997, amenity="cafe") for i in range(1, search_mod.OVERPASS_ELEMENT_LIMIT + 1)]
    elements[0]["tags"]["name"] = "Only Named"
    body = run(world, elements)
    assert [p["name"] for p in body["places"]] == ["Only Named"]
    assert body["truncated"] is True


def test_buildings_use_their_center(world):
    # The July prototype read center["lng"], which Overpass never sends, and lost every way.
    body = run(world, [way(5, 40.7315, -73.9965, name="Building Cafe", amenity="cafe")])
    place, = body["places"]
    assert (place["lat"], place["lng"]) == (40.7315, -73.9965)
    assert place["id"] == "way/5"
    assert place["osm_url"] == "https://www.openstreetmap.org/way/5"


def test_websites_are_https_only(world):
    sites = {
        1: "https://good.example/menu?x=1",
        2: "http://plain.example",
        3: "javascript:alert(1)",
        4: "www.no-scheme.example",
        5: "https://user:pw@evil.example",
        6: "https://ok.example/<script>",
        7: "data:text/html,hi",
        8: "HTTPS://Upper.Example",
        9: "https:///no-host",
    }
    elements = [node(i, 40.731, -73.997 + i * 0.001, name=f"Place {i}", amenity="cafe", website=url) for i, url in sites.items()]
    body = run(world, elements)
    got = {p["name"]: p["website"] for p in body["places"]}
    assert got == {"Place 1": sites[1], "Place 8": sites[8], **{f"Place {i}": "" for i in (2, 3, 4, 5, 6, 7, 9)}}


def test_contact_website_and_phone_fallbacks(world):
    body = run(world, [node(1, 40.731, -73.997, name="A", amenity="cafe", **{
        "contact:website": "https://a.example", "contact:phone": "+1 (212) 555-0100;+1 212 555 0101"})])
    place, = body["places"]
    assert place["website"] == "https://a.example"
    assert place["phone"] == "+1 (212) 555-0100"


def test_odd_phone_values_are_dropped(world):
    body = run(world, [node(1, 40.731, -73.997, name="A", amenity="cafe", phone="call us!"),
                       node(2, 40.732, -73.997, name="B", amenity="cafe", phone="12")])
    assert {p["phone"] for p in body["places"]} == {""}


def test_unnamed_places_use_brand_or_operator_or_are_dropped(world):
    body = run(world, [
        node(1, 40.731, -73.997, amenity="atm", brand="Chase"),
        node(2, 40.732, -73.997, amenity="atm", operator="Citibank"),
        node(3, 40.733, -73.997, amenity="atm"),
    ], what="atm")
    assert [(p["name"], p["category"]) for p in body["places"]] == [
        ("Chase", "ATM (cash machine)"), ("Citibank", "ATM (cash machine)")]


def test_the_same_place_mapped_twice_appears_once(world):
    body = run(world, [node(1, 40.73101, -73.99701, name="Twin", amenity="cafe"),
                       way(2, 40.73102, -73.99702, name="twin", amenity="cafe")])
    assert len(body["places"]) == 1


def test_name_search_drops_streets_and_other_non_places(world):
    body = run(world, [
        way(1, 40.731, -73.997, name="Joe's Street", highway="residential"),
        node(2, 40.731, -73.997, name="Joe's Pizza", amenity="fast_food", cuisine="pizza"),
    ], what="Joe's")
    assert [p["name"] for p in body["places"]] == ["Joe's Pizza"]
    assert body["places"][0]["category"] == "Fast food · pizza"


def test_address_hours_and_label(world):
    body = run(world, [node(1, 40.731, -73.997, name="Corner Bakery", shop="bakery",
                            opening_hours="Mo-Su 06:00-20:00", **{"addr:housenumber": "10", "addr:street": "Bleecker St",
                                                                  "addr:city": "New York", "addr:state": "NY",
                                                                  "addr:postcode": "10012"})], what="bakery")
    place, = body["places"]
    assert place["address"] == "10 Bleecker St, New York, NY 10012"
    assert place["hours"] == "Mo-Su 06:00-20:00"
    assert place["category"] == "Bakery"


def test_control_characters_and_long_values_from_osm_are_cleaned(world):
    body = run(world, [node(1, 40.731, -73.997, name="Bad\u0000Name‮" + "x" * 300, amenity="cafe",
                            opening_hours="y" * 500)])
    place, = body["places"]
    assert "\u0000" not in place["name"] and "‮" not in place["name"]
    assert len(place["name"]) <= 100 and len(place["hours"]) <= 160


def test_malformed_elements_are_skipped(world):
    body = run(world, [
        {"type": "node", "id": 1, "lat": "x", "lon": 1, "tags": {"name": "A", "amenity": "cafe"}},
        {"type": "way", "id": 2, "tags": {"name": "B", "amenity": "cafe"}},
        {"type": "area", "id": 3, "lat": 1, "lon": 1, "tags": {"name": "C", "amenity": "cafe"}},
        {"type": "node", "id": True, "lat": 1, "lon": 1, "tags": {"name": "D", "amenity": "cafe"}},
        {"type": "node", "id": 5, "lat": 95, "lon": 1, "tags": {"name": "E", "amenity": "cafe"}},
        "not a dict",
        node(6, 40.731, -73.997, name="Good", amenity="cafe"),
    ])
    assert [p["name"] for p in body["places"]] == ["Good"]


def test_an_empty_area_is_a_normal_empty_answer(world):
    body = run(world, [])
    assert body["places"] == [] and body["truncated"] is False


def test_where_search_returns_the_geocoded_label(world):
    resp = world.post(COFFEE_NEAR_PARK)
    assert resp.json["location"].startswith("Washington Square Park")
