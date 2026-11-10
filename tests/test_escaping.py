"""User text must never leave the Overpass QL string or the regular expression it is placed in.

The checks read each generated query back the way Overpass does, using the escape rules in the
Overpass QL documentation (https://wiki.openstreetmap.org/wiki/Overpass_API/Overpass_QL#Escaping):
a string is double- or single-quoted, and inside it backslash starts one of \\n \\t \\" \\' \\\\ \\uXXXX.
Then three properties must hold for any input:

1. Structure: the query outside its strings is identical to the query for a harmless term, so
   no statement, filter or setting was added, removed or changed.
2. String content: the decoded regular expression is exactly regex_literal(text).
3. Regex content: every POSIX extended regular expression special character in it is escaped,
   so it matches the text literally (checked directly, and with Python's re as a second opinion).
"""
import json
import random
import re

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from conftest import ORIGIN

ERE_SPECIAL = set("\\.[()*+?{|^$")
NASTY = list('"\'\\[](){}.*+?|^$;~,:=!<>/#&%@`-_ ') + [
    "\n", "\r", "\t", "\x00", "\x1b", "\u2028", "\u2029", "\u200b", "\u202e", "\ufeff",
    "\u201c", "\u201d", "\u2018", "\u2019", "\uff02", "\uff3c", "\u00e9", "\u00df", "\u0130", "\U0001f355",
    "out", "body", "json", "timeout", "around", "nwr", "node", "way", "relation", ");", "\";", "\\\"", "\\\\",
]
nasty_text = st.lists(st.sampled_from(NASTY), max_size=20).map("".join)
any_text = st.text(st.characters(), max_size=60)
surrogates = st.text(st.characters(min_codepoint=0xD800, max_codepoint=0xDFFF), min_size=1, max_size=4)
fuzz_text = st.one_of(nasty_text, any_text, st.tuples(any_text, surrogates, nasty_text).map("".join))


def read_query(query):
    """(skeleton, strings): the query with each string replaced by a marker, and the decoded strings."""
    skeleton, strings, i = [], [], 0
    while i < len(query):
        ch = query[i]
        if ch not in "\"'":
            skeleton.append(ch)
            i += 1
            continue
        quote, i, out = ch, i + 1, []
        while True:
            assert i < len(query), "unterminated string"
            c = query[i]
            if c == "\\":
                assert i + 1 < len(query), "dangling backslash"
                nxt = query[i + 1]
                if nxt in "\"'\\":
                    out.append(nxt)
                    i += 2
                elif nxt in "nt":
                    out.append("\n" if nxt == "n" else "\t")
                    i += 2
                elif nxt == "u":
                    out.append(chr(int(query[i + 2:i + 6], 16)))
                    i += 6
                else:
                    raise AssertionError(f"unknown escape \\{nxt}")
            elif c == quote:
                i += 1
                break
            else:
                assert c not in "\n\r", "raw line break inside a string"
                out.append(c)
                i += 1
        skeleton.append("\x00STR\x00")
        strings.append("".join(out))
    return "".join(skeleton), strings


def is_literal_regex(pattern, text):
    """Every special character escaped, and unescaping gives back the text."""
    out, i = [], 0
    while i < len(pattern):
        c = pattern[i]
        if c == "\\":
            assert i + 1 < len(pattern) and pattern[i + 1] in ERE_SPECIAL, "backslash before an ordinary character"
            out.append(pattern[i + 1])
            i += 2
        else:
            assert c not in ERE_SPECIAL, f"unescaped special character {c!r}"
            out.append(c)
            i += 1
    return "".join(out) == text


def check_name_query(mod, text):
    term = mod.Term("name", text, text)
    query = mod.build_query(term, 40.731, -73.997, 1500)
    reference = mod.build_query(mod.Term("name", "x", "x"), 40.731, -73.997, 1500)
    skeleton, strings = read_query(query)
    ref_skeleton, ref_strings = read_query(reference)
    assert skeleton == ref_skeleton
    assert len(strings) == len(ref_strings)
    user = [s for s, r in zip(strings, ref_strings) if r == "x"]
    assert len(user) == 2  # the name and cuisine filters
    for pattern in user:
        assert pattern == mod.regex_literal(text)
        assert is_literal_regex(pattern, text)
        assert re.fullmatch(pattern, text) is not None
    # The strings that came from constants are untouched.
    assert [s for s, r in zip(strings, ref_strings) if r != "x"] == [r for r in ref_strings if r != "x"]


@settings(max_examples=400, deadline=None)
@given(fuzz_text)
def test_fuzz_from_request_to_query(search_mod, raw):
    """Whatever arrives in 'what' is refused, becomes a category, or becomes a contained name match."""
    try:
        request = search_mod.parse_request({"what": raw, "where": "new york"})
    except search_mod.SearchFailure as failure:
        assert failure.code == "invalid"
        return
    term = request.term
    if term.kind == "category":
        assert term.key in search_mod.CATEGORIES
    else:
        assert len(term.key) <= search_mod.MAX_WHAT
        check_name_query(search_mod, term.key)


@settings(max_examples=400, deadline=None)
@given(st.text(st.characters(blacklist_categories=("Cc", "Cf", "Cs", "Co", "Cn")), max_size=80))
def test_fuzz_escaping_functions_directly(search_mod, text):
    _, strings = read_query(search_mod.ql_string(text))
    assert strings == [text]
    assert is_literal_regex(search_mod.regex_literal(text), text)
    _, strings = read_query(search_mod.ql_string(search_mod.regex_literal(text)))
    assert strings == [search_mod.regex_literal(text)]


def test_seeded_random_fuzz(search_mod):
    rng = random.Random(20261109)
    pool = NASTY + [chr(c) for c in range(32, 127)]
    for _ in range(3000):
        raw = "".join(rng.choice(pool) for _ in range(rng.randint(1, 50)))
        try:
            request = search_mod.parse_request({"what": raw, "where": "new york"})
        except search_mod.SearchFailure:
            continue
        if request.term.kind == "name":
            check_name_query(search_mod, request.term.key)


@pytest.mark.parametrize("text", [
    '"', "\\", '\\"', '"];out;', '");out body;(', "pizza\");node(1);out;(\"", ".*", "(a|b)+", "[", "]",
    "^$", "{1,5}", "a\\", "\\\\\"", "'", "''", "$(rm)", "x\\nout", "joe's", "st. mark's", "a.b", "1+1",
])
def test_known_break_out_attempts_stay_inside(search_mod, text):
    check_name_query(search_mod, text)


@pytest.mark.parametrize("raw", ["pizza\n);out;", "a\x00b", "\u202eazzip", "ok\u200b\u200bok", "x\ud800y", "tab\there"])
def test_control_and_format_characters_never_reach_the_query(search_mod, raw):
    request = search_mod.parse_request({"what": raw, "where": "new york"})
    assert all(not 0xD800 <= ord(c) <= 0xDFFF and c.isprintable() for c in request.term.key)
    if request.term.kind == "name":
        check_name_query(search_mod, request.term.key)


def test_ql_string_refuses_control_characters(search_mod):
    for bad in ("a\nb", "a\x00", "\u200b", "\ud800"):
        with pytest.raises(ValueError):
            search_mod.ql_string(bad)


def test_quote_and_backslash_are_escaped_backslash_first(search_mod):
    assert search_mod.ql_string('a"b') == '"a\\"b"'
    assert search_mod.ql_string("a\\b") == '"a\\\\b"'
    assert search_mod.ql_string('\\"') == '"\\\\\\""'
    assert search_mod.regex_literal("St. Mark's (East)") == "St\\. Mark's \\(East\\)"


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(fuzz_text)
def test_fuzz_through_the_http_handler(world, raw):
    """End to end: the query that would go to Overpass keeps its shape for any 'what'."""
    world.env(PLACES_VISITOR_DAILY=1000, PLACES_UPSTREAM_DAILY=10000)
    world.store.results.clear()
    before = len(world.upstream.calls)
    resp = world.post({"what": raw, "where": "new york"}, origin=ORIGIN)
    assert resp.status in (200, 400), resp.json
    assert set(resp.json) <= {"ok", "error", "query", "center", "location", "places", "truncated", "cached", "fetched_at"}
    if resp.status == 400:
        assert resp.json["error"]["code"] == "invalid"
        assert len(world.upstream.calls) == before
        return
    sent = [c for c in world.upstream.calls[before:] if c.service == "overpass"]
    assert len(sent) == 1
    term = world.mod.Term(**resp.json["query"]["term"])
    lat, lng = resp.json["center"]["lat"], resp.json["center"]["lng"]
    if term.kind == "name":
        reference = world.mod.build_query(world.mod.Term("name", "x", "x"), lat, lng, 1500)
        assert read_query(sent[0].query)[0] == read_query(reference)[0]
    else:
        assert sent[0].query == world.mod.build_query(term, lat, lng, 1500)
    json.dumps(resp.json)  # the response itself always serializes
