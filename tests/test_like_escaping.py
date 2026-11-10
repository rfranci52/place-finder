"""Search text in the name match. The query is parameterized, so SQL injection is not possible; what
these tests guard is the LIKE pattern built from the text: a typed % or _ must match itself, not
act as a wildcard. A reference LIKE matcher (SQL semantics, ESCAPE '\\') checks it here, and
test_schema_pg.py checks the same property against real Postgres."""
import re

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

NASTY = list("%_\\'\";()[]{}*+?.^$|") + ["é", "İ", "\U0001f355", " ", "%%", "\\%", "\\_", "_%"]
nasty_text = st.lists(st.sampled_from(NASTY + list("abcXYZ019")), min_size=1, max_size=20).map("".join)
any_text = st.text(st.characters(blacklist_categories=("Cs",)), min_size=1, max_size=40)


def like_regex(pattern, escape="\\"):
    """SQL LIKE semantics as a Python regex: % any run, _ any one character, escape makes literal."""
    out, i = [], 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == escape:
            assert i + 1 < len(pattern), "a pattern may not end with the escape character"
            out.append(re.escape(pattern[i + 1]))
            i += 2
            continue
        out.append(".*" if ch == "%" else "." if ch == "_" else re.escape(ch))
        i += 1
    return re.compile("".join(out), re.S)


def like(value, pattern):
    return like_regex(pattern).fullmatch(value) is not None


@settings(max_examples=500, deadline=None)
@given(st.one_of(nasty_text, any_text))
def test_an_escaped_pattern_matches_exactly_its_text(search_mod, text):
    pattern = search_mod.like_escape(text)
    assert like(text, pattern)
    # Contains: the text inside anything matches, and the wildcards never leak.
    assert like("before " + text + " after", f"%{pattern}%")


@settings(max_examples=500, deadline=None)
@given(st.one_of(nasty_text, any_text), st.one_of(nasty_text, any_text))
def test_an_escaped_pattern_does_not_match_other_text(search_mod, text, other):
    if other != text:
        assert not like(other, search_mod.like_escape(text))


@pytest.mark.parametrize("text,victim", [
    ("%", "anything at all"), ("_", "x"), ("a_c", "abc"), ("100%", "1000"), ("\\", "\\\\"), ("%_%", "abc"),
])
def test_wildcards_typed_by_a_visitor_are_literal(search_mod, text, victim):
    assert not like(victim, search_mod.like_escape(text))
    assert like(text, search_mod.like_escape(text))


def test_name_search_parameters(search_mod):
    term = search_mod.resolve_term("50% Off_Deals")
    sql, params = search_mod.search_params(term, 40.73, -74.0, 1000, 61)
    assert sql == search_mod.NAME_SQL
    assert params["contains"] == "%50\\% off\\_deals%"
    assert params["text"] == "50% off_deals"
    assert params["fuzzy"] is True
    # The category slug is built from folded words only, with its underscores escaped for LIKE.
    assert params["slug"] == "50_off_deals"
    assert params["slug_start"] == "50\\_off\\_deals\\_%"
    assert params["slug_middle"] == "%\\_50\\_off\\_deals\\_%"


def test_short_terms_skip_the_fuzzy_match(search_mod):
    assert search_mod.search_params(search_mod.resolve_term("ab"), 0, 0, 500, 61)[1]["fuzzy"] is False


def test_the_sql_has_no_room_for_request_text(search_mod):
    """Both queries are built once, from constants, before any request arrives."""
    for sql in (search_mod.CATEGORY_SQL, search_mod.NAME_SQL):
        assert "{" not in sql and "}" not in sql
        placeholders = set(re.findall(r"%\((\w+)\)s", sql))
        assert placeholders <= {"lat", "lng", "radius", "limit", "categories", "contains", "fuzzy", "text",
                                "slug", "slug_start", "slug_end", "slug_middle"}


@settings(max_examples=300, deadline=None)
@given(st.one_of(nasty_text, any_text))
def test_any_typed_text_yields_well_formed_parameters(search_mod, raw):
    try:
        request = search_mod.parse_request({"what": raw, "where": "Greenwich Village"})
    except search_mod.SearchFailure as failure:
        assert failure.code == "invalid"
        return
    sql, params = search_mod.search_params(request.term, 40.73, -74.0, 1500, 61)
    for key in ("contains", "slug_start", "slug_end", "slug_middle"):
        if key in params:
            like_regex(params[key])  # raises if an escape is left dangling
    assert all(isinstance(v, (str, int, float, bool, list)) for v in params.values())
