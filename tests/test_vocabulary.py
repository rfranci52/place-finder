"""The curated vocabulary: everyday words to Overture category keys, and the fallback name match."""
import json

import pytest

from conftest import NEAR_THE_PARK, ROOT

REQUIRED = {
    "Food and drink": ["pizza", "coffee", "bakery", "bar", "restaurant"],
    "Everyday services": ["grocery", "laundry", "bank", "atm", "library", "park", "gym"],
    "Health": ["pharmacy", "clinic", "hospital", "dentist", "doctor"],
}
CHIPS = ["Coffee", "Pizza", "Pharmacy", "Clinic", "Grocery", "Park"]
# Row counts per category key in release 2026-10-21.0 at confidence 0.7, written by the real
# load (`load_places.py check-vocabulary`). The test fails if the vocabulary uses a key the
# release does not have.
SNAPSHOT = json.loads((ROOT / "tests" / "data" / "category_counts_2026-10-21.0.json").read_text())


def test_about_forty_categories_in_three_groups(search_mod):
    assert len(search_mod.VOCABULARY) == 40
    assert {c.group for c in search_mod.VOCABULARY} == set(REQUIRED)


@pytest.mark.parametrize("group,key", [(g, k) for g, keys in REQUIRED.items() for k in keys])
def test_required_terms_are_categories_in_their_group(search_mod, group, key):
    term = search_mod.resolve_term(key)
    assert term.kind == "category"
    assert search_mod.CATEGORIES[term.key].group == group


@pytest.mark.parametrize("chip", CHIPS)
def test_every_quick_chip_is_a_category(search_mod, chip):
    assert search_mod.resolve_term(chip).kind == "category"


def test_every_key_exists_in_the_loaded_release(search_mod):
    missing = [(c.key, k) for c in search_mod.VOCABULARY for k in c.keys if SNAPSHOT.get(k, 0) == 0]
    assert missing == []
    assert sum(len(c.keys) for c in search_mod.VOCABULARY) == 202


def test_keys_are_unique_within_each_category(search_mod):
    for category in search_mod.VOCABULARY:
        assert len(set(category.keys)) == len(category.keys), category.key


@pytest.mark.parametrize("typed,key", [
    ("cafe", "coffee"), ("Café", "coffee"), ("  COFFEE   shop ", "coffee"), ("cafés", "coffee"),
    ("pharmacies", "pharmacy"), ("Drug-store", "pharmacy"), ("ATMs", "atm"), ("cash machine", "atm"),
    ("bars", "bar"), ("pubs", "bar"), ("pizzeria", "pizza"), ("laundromats", "laundry"),
    ("libraries", "library"), ("groceries", "grocery"), ("supermarket", "grocery"), ("Urgent care", "clinic"),
    ("walk-in clinic", "clinic"), ("emergency room", "hospital"), ("dentists", "dentist"), ("doctors", "doctor"),
    ("parks", "park"), ("gyms", "gym"), ("bodega", "convenience store"), ("tacos", "mexican"),
    ("vets", "veterinarian"), ("galleries", "art gallery"), ("barber", "hair salon"),
])
def test_synonyms_plurals_case_and_accents_resolve(search_mod, typed, key):
    term = search_mod.resolve_term(search_mod.clean_text(typed))
    assert (term.kind, term.key) == ("category", key)


@pytest.mark.parametrize("typed", ["Joe's", "thai", "Shake Shack", "ramen bar xyz", "7-Eleven"])
def test_unknown_terms_fall_back_to_a_name_match(search_mod, typed):
    term = search_mod.resolve_term(typed)
    assert (term.kind, term.key) == ("name", typed.lower())


def test_a_term_claimed_twice_is_rejected(search_mod):
    a = search_mod.Category("a", "A", "Health", ("shared",), ("x",))
    b = search_mod.Category("b", "B", "Health", ("Shared",), ("y",))
    with pytest.raises(ValueError, match="belongs to both"):
        search_mod.build_index((a, b))


def test_restaurant_covers_every_cuisine_key_with_twenty_places(search_mod):
    keys = set(search_mod.CATEGORIES["restaurant"].keys)
    expected = {k for k, n in SNAPSHOT.items() if (k == "restaurant" or k.endswith("_restaurant")) and n >= 20}
    assert keys == expected


def test_category_search_sends_the_keys_as_one_array(search_mod):
    sql, params = search_mod.search_params(search_mod.resolve_term("coffee"), 40.73, -74.0, 1500, 61)
    assert sql == search_mod.CATEGORY_SQL
    assert params["categories"] == ["coffee_shop", "cafe", "coffee_roastery"]
    assert (params["lat"], params["lng"], params["radius"], params["limit"]) == (40.73, -74.0, 1500, 61)


def test_search_reports_which_kind_of_match_it_used(world):
    resp = world.post(NEAR_THE_PARK)
    assert resp.json["query"]["term"] == {"kind": "category", "key": "coffee", "label": "Coffee"}
    resp = world.post({"what": "Joe's", "where": "Greenwich Village"})
    assert resp.json["query"]["term"] == {"kind": "name", "key": "joe's", "label": "Joe's"}
    assert [p["name"] for p in resp.json["places"]] == ["Joe's Pizza"]


def test_an_unknown_cuisine_word_matches_its_category_key(world):
    resp = world.post({"what": "thai", "where": "Greenwich Village"})
    assert [p["name"] for p in resp.json["places"]] == ["Thai Garden"]
