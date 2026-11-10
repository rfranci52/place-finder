"""The curated vocabulary: what maps to a tag filter, and what falls back to a name match."""
import re

import pytest

from conftest import COFFEE_NEAR_PARK

REQUIRED = {
    "Food and drink": ["pizza", "coffee", "bakery", "bar", "restaurant"],
    "Everyday services": ["grocery", "laundry", "bank", "atm", "library", "park", "gym"],
    "Health": ["pharmacy", "clinic", "hospital", "dentist", "doctor"],
}
CHIPS = ["Coffee", "Pizza", "Pharmacy", "Clinic", "Grocery", "Park"]


def test_about_forty_categories_in_three_groups(search_mod):
    assert 38 <= len(search_mod.VOCABULARY) <= 45
    groups = {c.group for c in search_mod.VOCABULARY}
    assert groups == set(REQUIRED)


@pytest.mark.parametrize("group,key", [(g, k) for g, keys in REQUIRED.items() for k in keys])
def test_required_terms_are_categories_in_their_group(search_mod, group, key):
    term = search_mod.resolve_term(key)
    assert term.kind == "category"
    assert search_mod.CATEGORIES[term.key].group == group


@pytest.mark.parametrize("chip", CHIPS)
def test_every_quick_chip_is_a_category(search_mod, chip):
    assert search_mod.resolve_term(chip).kind == "category"


@pytest.mark.parametrize("typed,key", [
    ("cafe", "coffee"), ("Café", "coffee"), ("  COFFEE   shop ", "coffee"), ("cafés", "coffee"),
    ("pharmacies", "pharmacy"), ("Drug-store", "pharmacy"), ("ATMs", "atm"), ("cash machine", "atm"),
    ("bars", "bar"), ("pubs", "bar"), ("pizzeria", "pizza"), ("laundromats", "laundry"),
    ("libraries", "library"), ("groceries", "grocery"), ("Urgent care", "clinic"), ("walk-in clinic", "clinic"),
    ("emergency room", "hospital"), ("dentists", "dentist"), ("doctors", "doctor"), ("parks", "park"),
    ("gyms", "gym"), ("bodega", "convenience store"), ("tacos", "mexican"), ("vets", "veterinarian"),
])
def test_synonyms_plurals_case_and_accents_resolve(search_mod, typed, key):
    term = search_mod.resolve_term(search_mod.clean_text(typed))
    assert (term.kind, term.key) == ("category", key)


def test_synonyms_share_one_cache_key(search_mod):
    keys = {search_mod.resolve_term(t).cache_key for t in ("coffee", "cafe", "Café", "espresso")}
    assert keys == {"c:coffee"}


@pytest.mark.parametrize("typed", ["Joe's", "thai", "Shake Shack", "ramen bar xyz", "7-Eleven"])
def test_unknown_terms_fall_back_to_a_name_match(search_mod, typed):
    term = search_mod.resolve_term(typed)
    assert term.kind == "name"
    assert term.key == typed.lower()
    assert term.cache_key == "n:" + typed.lower()


def test_a_term_claimed_twice_is_rejected(search_mod):
    a = search_mod.Category("a", "A", "Health", ("shared",), ())
    b = search_mod.Category("b", "B", "Health", ("Shared",), ())
    with pytest.raises(ValueError, match="belongs to both"):
        search_mod.build_index((a, b))


def test_every_category_builds_a_query_with_a_name_filter_and_timeout(search_mod):
    for category in search_mod.VOCABULARY:
        term = search_mod.Term("category", category.key, category.label)
        query = search_mod.build_query(term, 40.731, -73.997, 1500)
        assert query.startswith("[out:json][timeout:10];(")
        assert query.endswith(");out center 200;")
        statements = re.findall(r"nwr\[.*?\(around:1500,40\.731,-73\.997\);", query)
        assert len(statements) == len(category.clauses) >= 1
        # Only named places come back for a category search.
        assert all(search_mod.NAMED in s for s in statements)


def test_name_search_requires_a_place_tag(search_mod):
    term = search_mod.resolve_term("Joe's")
    query = search_mod.build_query(term, 40.731, -73.997, 1000)
    assert query.count(search_mod.IS_POI) == 2
    assert '["name"~"joe\'s",i]' in query
    assert '["cuisine"~"joe\'s",i]' in query


def test_pizza_matches_cuisine_or_name(search_mod):
    query = search_mod.build_query(search_mod.resolve_term("pizza"), 1, 2, 500)
    assert '["cuisine"~"(^|;) *(pizza) *(;|$)",i]' in query
    assert '["name"~"pizz",i]' in query


def test_search_reports_which_kind_of_match_it_used(world):
    resp = world.post(COFFEE_NEAR_PARK)
    assert resp.json["query"]["term"] == {"kind": "category", "key": "coffee", "label": "Coffee"}
    resp = world.post({"what": "Joe's", "where": "Washington Square Park, New York"})
    assert resp.json["query"]["term"] == {"kind": "name", "key": "joe's", "label": "Joe's"}
