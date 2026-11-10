"""Place search across New York City's five boroughs: POST /api/search.

A Vercel Python function in one self-contained file (standard library plus psycopg), because
Vercel does not reliably bundle sibling modules. It turns a search term plus a location into
the nearest matching places:

1. A typed location is matched against NYC's own neighborhood and park names first (loaded from
   NYC Open Data). Anything else, typically a street address, goes to NYC GeoSearch, the City's
   free geocoder (NYC Department of City Planning, https://geosearch.planninglabs.nyc), and the
   answer is cached in Postgres for 30 days.
2. A point outside the five boroughs is refused with out_of_area.
3. One parameterized PostGIS query finds the 60 nearest matches within the radius, from the
   site's own copy of Overture Maps places (loaded monthly by scripts/load_places.py).

Searches are cheap database reads, so they get a generous daily allowance. Calls to GeoSearch
get a smaller one. Both are counted per visitor (a salted hash of the IP address) and for the
whole site, in Postgres; when the database cannot be reached the search is refused.

Responses are JSON. Success is {"ok": true, ...}; every failure is
{"ok": false, "error": {"code": ..., "message": ...}} with a code from ERRORS.
"""
import hashlib
import hmac
import http.client
import ipaddress
import json
import math
import os
import re
import time
import unicodedata
import urllib.error
import urllib.request
import zlib
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlencode, urlsplit

# ------------------------------------------------------------------ settings

USER_AGENT = "place-finder-demo/1.0 (portfolio project)"
# NYC GeoSearch: Pelias over the City's Property Address Directory. No key; the documentation
# (https://geosearch.planninglabs.nyc/docs, source at github.com/NYCPlanning/labs-geosearch-docs,
# read 2026-11-09) publishes no rate limit, and asks only that autocomplete be throttled. This
# site never autocompletes: it geocodes once per submitted search, and caches the answer.
GEOSEARCH_URL = "https://geosearch.planninglabs.nyc/v2/search"
# GeoSearch scores how well a result matches the parsed input text, from 0 to 1. Below this the
# match is treated as not found rather than shown as a guess.
MIN_GEOCODE_CONFIDENCE = 0.8

MAX_BODY_BYTES = 2048
MAX_WHAT = 40
MAX_WHERE = 80
MIN_TEXT = 2
RADII = (500, 1000, 1500, 2500)
DEFAULT_RADIUS = 1500
MAX_PLACES = 60
GEOSEARCH_TIMEOUT_S = 6
MAX_UPSTREAM_BYTES = 1_000_000
CONNECT_TIMEOUT_S = 8
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

# Daily allowances, overridable with environment variables. A search is one indexed query, so a
# person browsing never meets the search limit; the geocoder limits keep this site a light user
# of a free City service.
SEARCHES_PER_VISITOR = 300
SEARCHES_PER_DAY = 20_000
GEOCODES_PER_VISITOR = 30
GEOCODES_PER_DAY = 1_000

# An indirection point for the tests, which swap in a fake clock.
monotonic = time.monotonic


@dataclass(frozen=True)
class Settings:
    db_url: str
    hash_salt: str
    allowed_origins: frozenset
    searches_per_visitor: int
    searches_per_day: int
    geocodes_per_visitor: int
    geocodes_per_day: int
    geosearch_url: str


def _env_int(env, name, default, low, high):
    try:
        value = int(env.get(name, default))
    except (TypeError, ValueError):
        return default
    return min(max(value, low), high)


def load_settings(env=None):
    env = os.environ if env is None else env
    return Settings(
        db_url=env.get("PLACES_DB_URL", ""),
        hash_salt=env.get("PLACES_HASH_SALT", ""),
        allowed_origins=parse_origins(env.get("PLACES_ALLOWED_ORIGINS", "")),
        searches_per_visitor=_env_int(env, "PLACES_SEARCHES_PER_VISITOR", SEARCHES_PER_VISITOR, 0, 100_000),
        searches_per_day=_env_int(env, "PLACES_SEARCHES_PER_DAY", SEARCHES_PER_DAY, 0, 10_000_000),
        geocodes_per_visitor=_env_int(env, "PLACES_GEOCODES_PER_VISITOR", GEOCODES_PER_VISITOR, 0, 10_000),
        geocodes_per_day=_env_int(env, "PLACES_GEOCODES_PER_DAY", GEOCODES_PER_DAY, 0, 1_000_000),
        geosearch_url=env.get("PLACES_GEOSEARCH_URL") or GEOSEARCH_URL,
    )


# ------------------------------------------------------------------ errors

# code: HTTP status. The page renders each code its own way.
ERRORS = {
    "invalid": 400,
    "forbidden": 403,
    "not_found": 404,
    "method_not_allowed": 405,
    "out_of_area": 422,
    "rate_limited": 429,
    "upstream_busy": 503,
    "unavailable": 503,
}


class SearchFailure(Exception):
    def __init__(self, code, message, retry_after=None):
        super().__init__(code)
        self.code = code
        self.message = message
        self.status = ERRORS[code]
        self.retry_after = retry_after

    def payload(self):
        error = {"code": self.code, "message": self.message}
        if self.retry_after is not None:
            error["retry_after"] = self.retry_after
        return {"ok": False, "error": error}


def invalid(message):
    return SearchFailure("invalid", message)


def upstream_busy():
    return SearchFailure(
        "upstream_busy",
        "NYC's address lookup service didn't answer. Please try again in a few seconds, or move the map "
        "and choose Search this area, which doesn't need it.",
        retry_after=10,
    )


def unavailable():
    return SearchFailure(
        "unavailable",
        "Search is paused because the demo's database can't be reached. Please try again in a minute.",
        retry_after=30,
    )


def out_of_area(typed=None):
    where = f"“{typed}” is" if typed else "The map center is"
    return SearchFailure(
        "out_of_area",
        f"{where} outside New York City. Place Finder covers the five boroughs only: Manhattan, Brooklyn, "
        "Queens, the Bronx and Staten Island.",
    )


def seconds_until_utc_midnight(now=None):
    now = now or datetime.now(timezone.utc)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(int((tomorrow - now).total_seconds()), 1)


def rate_limited(kind, verdict, per_visitor):
    wait = seconds_until_utc_midnight()
    hours = max(1, round(wait / 3600))
    when = "about an hour" if hours == 1 else f"about {hours} hours"
    if kind == "search" and verdict == "visitor_limit":
        message = f"You've run {per_visitor} searches today, the limit for one visitor. It resets in {when}."
    elif kind == "search":
        message = f"This demo has reached its daily search limit. It resets in {when}."
    elif verdict == "visitor_limit":
        message = (
            f"You've looked up {per_visitor} new locations today, the limit for one visitor. Locations "
            "already looked up still work, and so does moving the map and choosing Search this area."
        )
    else:
        message = (
            "This demo has used today's share of NYC's address lookup service. Locations already looked "
            "up still work, and so does moving the map and choosing Search this area."
        )
    return SearchFailure("rate_limited", message, retry_after=wait)


# ------------------------------------------------------------------ text

_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})


def _drop_controls(text):
    """Whitespace becomes a space; other control, format, surrogate and unassigned characters go."""
    return "".join(
        " " if ch.isspace() else ch
        for ch in text
        if ch.isspace() or not unicodedata.category(ch).startswith("C")
    )


def clean_text(value):
    """NFKC text with no control characters, curly quotes made straight, and single spaces."""
    text = unicodedata.normalize("NFKC", _drop_controls(value))
    return " ".join(_drop_controls(text).translate(_QUOTES).split())


def fold(text):
    """The form vocabulary lookups compare: lower case, no accents, punctuation as spaces."""
    text = unicodedata.normalize("NFKD", text.lower().replace("'", ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[\W_]+", " ", text).split())


def location_key(text):
    """The cache key for a location: lower case, with commas spaced one way."""
    parts = (part.strip() for part in text.lower().split(","))
    return ", ".join(part for part in parts if part)


# Trailing words that only say "in New York", and the borough names a location may end with.
_CITY_WORDS = ("new york city", "new york", "nyc", "ny", "usa", "us")
_BOROUGH_SUFFIXES = (("staten island", 5), ("the bronx", 2), ("manhattan", 1), ("brooklyn", 3),
                     ("queens", 4), ("bronx", 2))
# Words that tell nothing about where in the city a place is, for the geocoder check below.
_NOT_SPECIFIC = frozenset(w for phrase in _CITY_WORDS + tuple(n for n, _ in _BOROUGH_SUFFIXES)
                          for w in phrase.split()) | {"the", "of", "and", "in"}


def neighborhood_candidates(text):
    """(alias, borough code or None) pairs to look up, most literal first.

    "Williamsburg, Brooklyn, NY" tries "williamsburg brooklyn", then "williamsburg" in Brooklyn.
    """
    words = fold(text)
    stripped = True
    while stripped:
        stripped = False
        for noise in _CITY_WORDS:
            if words.endswith(" " + noise):
                words, stripped = words[: -len(noise) - 1].strip(), True
    yield words, None
    for name, code in _BOROUGH_SUFFIXES:
        if words.endswith(" " + name):
            yield words[: -len(name) - 1].strip(), code
            break


def geocode_matches_text(query, label):
    """Whether a GeoSearch answer plausibly names what was typed.

    GeoSearch is an address geocoder. Given a house number it is reliable; given only a name it
    falls back to a fuzzy street match ("Greenwich Village" came back as 37 Greenwich Street).
    So a query without digits is accepted only when every specific word in it starts a word of
    the answer's label.
    """
    if any(ch.isdigit() for ch in query):
        return True
    wanted = [w for w in fold(query).split() if w not in _NOT_SPECIFIC]
    have = fold(label or "").split()
    return bool(wanted) and all(any(h.startswith(w) for h in have) for w in wanted)


def like_escape(text):
    """Text for a LIKE or ILIKE pattern with ESCAPE '\\': every character matches itself.

    The query itself is parameterized, so this is not about SQL injection. It stops a typed
    % or _ from acting as a wildcard inside the pattern.
    """
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ------------------------------------------------------------------ vocabulary
#
# Each entry maps everyday words to Overture category keys (taxonomy.primary). Every key was
# checked against the loaded release 2026-10-21.0 with `load_places.py check-vocabulary`.


@dataclass(frozen=True)
class Category:
    key: str
    label: str
    group: str
    synonyms: tuple
    keys: tuple  # Overture taxonomy.primary values that count as a match


FOOD, EVERYDAY, HEALTH = "Food and drink", "Everyday services", "Health"

# Every key ending in _restaurant with at least 20 places in the release, plus "restaurant".
RESTAURANT_KEYS = (
    "afghani_restaurant", "african_restaurant", "american_restaurant", "argentine_restaurant",
    "asian_fusion_restaurant", "asian_restaurant", "bangladeshi_restaurant", "bar_and_grill_restaurant",
    "barbecue_restaurant", "brazilian_restaurant", "breakfast_and_brunch_restaurant", "buffet_restaurant",
    "burger_restaurant", "cajun_and_creole_restaurant", "caribbean_restaurant", "chicken_restaurant",
    "chicken_wings_restaurant", "chinese_restaurant", "colombian_restaurant", "comfort_food_restaurant",
    "cuban_restaurant", "dim_sum_restaurant", "dominican_restaurant", "doner_kebab_restaurant",
    "dumpling_restaurant", "eastern_european_restaurant", "ecuadorian_restaurant", "ethiopian_restaurant",
    "european_restaurant", "falafel_restaurant", "fast_food_restaurant", "filipino_restaurant",
    "fish_and_chips_restaurant", "french_restaurant", "georgian_restaurant", "german_restaurant",
    "greek_restaurant", "guatemalan_restaurant", "haitian_restaurant", "halal_restaurant",
    "hawaiian_restaurant", "health_food_restaurant", "hot_dog_restaurant", "indian_restaurant",
    "italian_restaurant", "jamaican_restaurant", "japanese_restaurant", "korean_restaurant",
    "kosher_restaurant", "latin_american_restaurant", "lebanese_restaurant", "malaysian_restaurant",
    "mediterranean_restaurant", "mexican_restaurant", "middle_eastern_restaurant", "nepalese_restaurant",
    "pakistani_restaurant", "pan_asian_restaurant", "peruvian_restaurant", "pizza_restaurant",
    "poke_restaurant", "puerto_rican_restaurant", "ramen_restaurant", "restaurant", "russian_restaurant",
    "salvadoran_restaurant", "seafood_restaurant", "soup_restaurant", "southern_american_restaurant",
    "spanish_restaurant", "sushi_restaurant", "taco_restaurant", "taiwanese_restaurant", "texmex_restaurant",
    "thai_restaurant", "turkish_restaurant", "uzbek_restaurant", "vegan_restaurant", "vegetarian_restaurant",
    "venezuelan_restaurant", "vietnamese_restaurant",
)

VOCABULARY = (
    Category("coffee", "Coffee", FOOD, ("cafe", "coffee shop", "espresso", "latte"),
             ("coffee_shop", "cafe", "coffee_roastery")),
    Category("pizza", "Pizza", FOOD, ("pizzeria", "pizza place", "slice"),
             ("pizza_restaurant", "pizza_delivery_service")),
    Category("bakery", "Bakery", FOOD, ("bread", "pastry", "pastries", "bake shop"), ("bakery",)),
    Category("bar", "Bar", FOOD, ("pub", "drinks", "cocktails", "beer", "brewery", "tavern"),
             ("bar", "cocktail_bar", "pub", "sports_bar", "wine_bar", "dive_bar", "gastropub", "irish_pub",
              "beer_bar", "beer_garden", "gay_bar", "brewery", "whiskey_bar")),
    Category("restaurant", "Restaurant", FOOD, ("food", "dinner", "lunch", "eat", "dining"), RESTAURANT_KEYS),
    Category("fast food", "Fast food", FOOD, ("fastfood", "takeout", "take out", "quick bite"),
             ("fast_food_restaurant", "hot_dog_restaurant")),
    Category("ice cream", "Ice cream", FOOD, ("gelato", "frozen yogurt", "dessert"),
             ("ice_cream_shop", "gelato_shop", "frozen_yogurt_shop", "dessert_shop")),
    Category("burger", "Burgers", FOOD, ("hamburger", "cheeseburger"), ("burger_restaurant",)),
    Category("sushi", "Sushi and Japanese", FOOD, ("japanese", "ramen"),
             ("sushi_restaurant", "japanese_restaurant", "ramen_restaurant")),
    Category("mexican", "Mexican", FOOD, ("taco", "tacos", "burrito", "taqueria"),
             ("mexican_restaurant", "taco_restaurant", "texmex_restaurant")),
    Category("chinese", "Chinese", FOOD, ("dim sum", "dumplings"),
             ("chinese_restaurant", "dim_sum_restaurant", "dumpling_restaurant", "cantonese_restaurant",
              "sichuan_restaurant", "shanghainese_restaurant")),
    Category("deli", "Deli", FOOD, ("sandwich", "sandwiches", "bagel", "bagels"),
             ("delicatessen", "sandwich_shop", "bagel_shop")),
    Category("liquor store", "Liquor store", FOOD, ("liquor", "wine", "wine shop", "beer store"),
             ("liquor_store", "beer_wine_spirits_store")),

    Category("grocery", "Grocery", EVERYDAY,
             ("groceries", "grocery store", "grocer", "food market", "supermarket", "super market"),
             ("grocery_store", "asian_grocery_store", "organic_grocery_store", "international_grocery_store",
              "mexican_grocery_store", "kosher_grocery_store", "health_market")),
    Category("convenience store", "Convenience store", EVERYDAY, ("convenience", "bodega", "corner store"),
             ("convenience_store",)),
    Category("laundry", "Laundry", EVERYDAY, ("laundromat", "dry cleaner", "dry cleaning", "laundry service"),
             ("laundromat", "dry_cleaning", "laundry_service")),
    Category("bank", "Bank", EVERYDAY, ("credit union",), ("bank", "bank_or_credit_union", "credit_union")),
    Category("atm", "ATM (cash machine)", EVERYDAY, ("cash machine", "cash"), ("atm",)),
    Category("library", "Library", EVERYDAY, ("public library",), ("library",)),
    Category("park", "Park", EVERYDAY, ("green space", "dog park"), ("park", "dog_park", "state_park", "national_park")),
    Category("playground", "Playground", EVERYDAY, ("play area",), ("playground",)),
    Category("gym", "Gym", EVERYDAY, ("fitness", "fitness center", "workout"),
             ("gym", "sport_or_fitness_facility", "boxing_gym")),
    Category("post office", "Post office", EVERYDAY, ("post", "mail"), ("post_office",)),
    Category("gas station", "Gas station", EVERYDAY, ("gas", "petrol", "fuel"), ("gas_station", "fueling_station")),
    Category("hardware store", "Hardware store", EVERYDAY, ("hardware", "home improvement"),
             ("hardware_store", "home_improvement_store", "hardware_home_and_garden_store")),
    Category("bookstore", "Bookstore", EVERYDAY, ("books", "bookshop", "book store"),
             ("bookstore", "used_bookstore", "academic_bookstore")),
    Category("hair salon", "Hair salon or barber", EVERYDAY, ("barber", "barbershop", "salon", "haircut"),
             ("hair_salon", "barber", "hair_stylist", "kids_hair_salon")),
    Category("bike shop", "Bike shop", EVERYDAY, ("bike", "bicycle", "bicycle shop", "bike repair"),
             ("bike_store", "bike_repair_maintenance")),
    Category("hotel", "Hotel", EVERYDAY, ("motel", "hostel", "lodging"), ("hotel", "motel", "hostel")),
    Category("museum", "Museum", EVERYDAY, ("art museum", "history museum"),
             ("museum", "art_museum", "history_museum", "childrens_museum", "science_museum",
              "contemporary_art_museum", "modern_art_museum", "design_museum", "community_museum")),
    Category("art gallery", "Art gallery", EVERYDAY, ("gallery", "galleries"), ("art_gallery",)),
    Category("pet store", "Pet store", EVERYDAY, ("pet shop", "pet supplies", "pets", "pet"),
             ("pet_store", "aquatic_pet_store")),
    Category("movie theater", "Movie theater", EVERYDAY, ("cinema", "movies", "movie theatre"),
             ("movie_theater", "drive_in_theater")),

    Category("pharmacy", "Pharmacy", HEALTH, ("drugstore", "drug store", "chemist"), ("pharmacy", "drugstore")),
    Category("clinic", "Clinic", HEALTH, ("urgent care", "walk in clinic", "health center", "medical center"),
             ("urgent_care_clinic", "walk_in_clinic", "public_health_clinic", "primary_care_or_general_clinic",
              "pediatric_clinic")),
    Category("hospital", "Hospital", HEALTH, ("emergency room", "emergency"),
             ("hospital", "emergency_department", "childrens_hospital")),
    Category("dentist", "Dentist", HEALTH, ("dental", "dental office", "orthodontist"),
             ("dental_clinic", "general_dentistry", "cosmetic_dentistry", "pediatric_dentistry")),
    Category("doctor", "Doctor", HEALTH, ("physician", "primary care", "medical office"), ("doctors_office",)),
    Category("eye care", "Eye care", HEALTH, ("optician", "optometrist", "eye doctor", "glasses", "eyeglasses"),
             ("optometry", "vision_or_eye_care_clinic", "eyewear_store")),
    Category("veterinarian", "Veterinarian", HEALTH, ("vet", "animal hospital", "veterinary"),
             ("veterinarian", "animal_hospital", "emergency_pet_hospital", "veterinary_care")),
)


def build_index(vocabulary):
    """Folded term -> Category. Two categories claiming the same term is a mistake, so it raises."""
    index = {}
    for category in vocabulary:
        for term in {category.key, category.label, *category.synonyms}:
            folded = fold(term)
            owner = index.setdefault(folded, category)
            if owner is not category:
                raise ValueError(f"{term!r} belongs to both {owner.key!r} and {category.key!r}")
    return index


CATEGORIES = {category.key: category for category in VOCABULARY}
TERM_INDEX = build_index(VOCABULARY)


@dataclass(frozen=True)
class Term:
    kind: str  # "category" for a vocabulary entry, "name" for a match on place names
    key: str  # the vocabulary key, or the typed text in lower case
    label: str


def _singulars(folded):
    if folded.endswith("ies") and len(folded) > 4:
        yield folded[:-3] + "y"
    if folded.endswith("es") and len(folded) > 3:
        yield folded[:-2]
    if folded.endswith("s") and len(folded) > 2:
        yield folded[:-1]


def resolve_term(text):
    """A vocabulary category when the text names one, otherwise a name match."""
    folded = fold(text)
    for candidate in (folded, *_singulars(folded)):
        category = TERM_INDEX.get(candidate)
        if category is not None:
            return Term("category", category.key, category.label)
    return Term("name", text.lower(), text)


# ------------------------------------------------------------------ requests

FIELDS = frozenset({"what", "where", "lat", "lng", "radius"})


@dataclass(frozen=True)
class SearchRequest:
    what: str
    term: Term
    radius: int
    where: str | None = None  # the cache key form of the location
    where_text: str | None = None  # the location as typed, cleaned, for messages
    lat: float | None = None
    lng: float | None = None


def _text_field(data, name, limit, missing, too_long):
    value = data.get(name)
    if value is None or value == "":
        raise invalid(missing)
    if not isinstance(value, str):
        raise invalid(f"'{name}' must be text.")
    # NFKC can turn one character into many, so the raw length is bounded before normalizing.
    if len(value) > limit * 4:
        raise invalid(too_long)
    text = clean_text(value)
    if len(text) < MIN_TEXT:
        raise invalid(missing)
    if len(text) > limit or len(text.lower()) > limit:
        raise invalid(too_long)
    return text


def _coordinate(value, low, high, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise invalid(f"'{name}' must be a number.")
    if not math.isfinite(value) or not low <= value <= high:
        raise invalid(f"'{name}' must be between {low} and {high}.")
    return float(value)


def parse_request(data):
    if not isinstance(data, dict):
        raise invalid("Send the search as a JSON object.")
    extra = set(data) - FIELDS
    if extra:
        raise invalid(f"Unexpected field: {sorted(extra)[0][:40]!r}.")
    what = _text_field(
        data, "what", MAX_WHAT,
        "Type what you're looking for, like coffee or pharmacy.",
        f"Keep the search term to {MAX_WHAT} characters.",
    )
    radius = data.get("radius", DEFAULT_RADIUS)
    if type(radius) is not int or radius not in RADII:
        raise invalid("That search radius isn't one this demo offers.")
    has_where = data.get("where") not in (None, "")
    has_point = "lat" in data or "lng" in data
    if has_where and has_point:
        raise invalid("Search near a typed location or near a map position, not both.")
    term = resolve_term(what)
    if has_point:
        return SearchRequest(
            what=what, term=term, radius=radius,
            lat=_coordinate(data.get("lat"), -90, 90, "lat"),
            lng=_coordinate(data.get("lng"), -180, 180, "lng"),
        )
    where = _text_field(
        data, "where", MAX_WHERE,
        "Add a neighborhood or address in New York City.",
        f"Keep the location to {MAX_WHERE} characters.",
    )
    return SearchRequest(what=what, term=term, radius=radius, where=location_key(where), where_text=where)


# ------------------------------------------------------------------ the place query
#
# One parameterized query per search. The two variants differ only in their match clause, and
# both clauses are constants: no request text is ever formatted into SQL.

_SEARCH_SQL = """
with here as (select st_setsrid(st_makepoint(%(lng)s, %(lat)s), 4326)::geography as pt)
select p.id::text, p.name, p.category, p.street, p.locality, p.postcode, p.phone, p.website, p.borough,
       st_y(p.geog::geometry), st_x(p.geog::geometry), st_distance(p.geog, here.pt)
from places.places p, here
where st_dwithin(p.geog, here.pt, %(radius)s)
  and ({match})
order by p.geog <-> here.pt
limit %(limit)s
"""
CATEGORY_SQL = _SEARCH_SQL.format(match="p.category = any(%(categories)s)")
# A name containing the typed text (trigram-indexed ILIKE), a close spelling for longer terms
# (pg_trgm's <% word similarity, default threshold 0.6), or a category key with the typed word
# in it, so "thai" finds thai_restaurant even when the name does not say so.
NAME_SQL = _SEARCH_SQL.format(match=(
    "p.name ilike %(contains)s escape '\\'"
    " or (%(fuzzy)s and %(text)s <%% p.name)"
    " or p.category = %(slug)s"
    " or p.category like %(slug_start)s escape '\\'"
    " or p.category like %(slug_end)s escape '\\'"
    " or p.category like %(slug_middle)s escape '\\'"
))


def search_params(term, lat, lng, radius, limit):
    params = {"lat": lat, "lng": lng, "radius": radius, "limit": limit}
    if term.kind == "category":
        return CATEGORY_SQL, {**params, "categories": list(CATEGORIES[term.key].keys)}
    slug = like_escape(fold(term.key).replace(" ", "_"))
    return NAME_SQL, {
        **params,
        "contains": f"%{like_escape(term.key)}%",
        "fuzzy": len(term.key) >= 4,
        "text": term.key,
        "slug": fold(term.key).replace(" ", "_"),  # empty never matches: categories are non-empty
        "slug_start": f"{slug}\\_%",
        "slug_end": f"%\\_{slug}",
        "slug_middle": f"%\\_{slug}\\_%",
    }


# ------------------------------------------------------------------ upstream HTTP


@dataclass(frozen=True)
class Reply:
    status: int | None  # None when no usable answer came back: timeout, connection error, oversized
    body: bytes = b""


def _gunzip(raw, limit=MAX_UPSTREAM_BYTES):
    try:
        inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
        out = inflater.decompress(raw, limit + 1)
    except zlib.error:
        return None
    if len(out) > limit or inflater.unconsumed_tail:
        return None
    return out


def http_request(url, *, timeout):
    """One GET to the geocoder. Network trouble comes back as Reply(None), never raises."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json", "Accept-Encoding": "gzip"}
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_UPSTREAM_BYTES + 1)
            encoding = (response.headers.get("Content-Encoding") or "").lower()
            status = response.status
    except urllib.error.HTTPError as exc:
        exc.close()
        return Reply(exc.code)
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
        return Reply(None)
    if len(raw) > MAX_UPSTREAM_BYTES:
        return Reply(None)
    if encoding == "gzip":
        raw = _gunzip(raw)
        if raw is None:
            return Reply(None)
    return Reply(status, raw)


# ------------------------------------------------------------------ limits


def quota_key(ip):
    """What a daily limit counts against. An IPv6 host usually holds a whole /64, so that is one visitor."""
    try:
        parsed = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if parsed.version == 6:
        if parsed.ipv4_mapped:
            return str(parsed.ipv4_mapped)
        return str(ipaddress.ip_network(f"{parsed}/64", strict=False))
    return ip


def visitor_key(ip, salt):
    """A keyed hash of the visitor's address. The address itself is never stored or logged."""
    return hmac.new(salt.encode("utf-8"), quota_key(ip).encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def take(store, kind, visitor, settings):
    if kind == "search":
        per_visitor, per_day = settings.searches_per_visitor, settings.searches_per_day
    else:
        per_visitor, per_day = settings.geocodes_per_visitor, settings.geocodes_per_day
    verdict = store.take_slot(kind, visitor, per_visitor, per_day)
    if verdict != "ok":
        raise rate_limited(kind, verdict, per_visitor)


# ------------------------------------------------------------------ geocoding


@dataclass(frozen=True)
class Geocode:
    found: bool
    lat: float | None = None
    lng: float | None = None
    label: str | None = None


def parse_geosearch(body):
    """The best GeoSearch match as a Geocode, or None when the answer is not the expected shape.

    GeoSearch answers in GeoJSON (it is Pelias): features[].geometry.coordinates is [lng, lat],
    and properties carry a display label and a match confidence.
    """
    try:
        data = json.loads(body)
    except ValueError:
        return None
    features = data.get("features") if isinstance(data, dict) else None
    if not isinstance(features, list):
        return None
    if not features:
        return Geocode(found=False)
    first = features[0] if isinstance(features[0], dict) else {}
    properties = first.get("properties") if isinstance(first.get("properties"), dict) else {}
    coordinates = (first.get("geometry") or {}).get("coordinates") if isinstance(first.get("geometry"), dict) else None
    try:
        lng, lat = float(coordinates[0]), float(coordinates[1])
        confidence = float(properties.get("confidence", 0))
    except (TypeError, ValueError, IndexError):
        return None
    if not (math.isfinite(lat) and math.isfinite(lng) and -90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    if not math.isfinite(confidence) or confidence < MIN_GEOCODE_CONFIDENCE:
        return Geocode(found=False)
    label = properties.get("label")
    label = clean_text(label)[:200] if isinstance(label, str) else None
    return Geocode(found=True, lat=lat, lng=lng, label=label or None)


def geocode_live(query, settings):
    """GeoSearch's best match, or Geocode(found=False) when it does not plausibly match the text."""
    params = {"text": query, "size": 1}
    started = monotonic()
    reply = http_request(f"{settings.geosearch_url}?{urlencode(params)}", timeout=GEOSEARCH_TIMEOUT_S)
    # One line per geocoder call, with no search text and no visitor details.
    print(f"upstream geosearch: {reply.status or 'no reply'} in {round((monotonic() - started) * 1000)} ms, "
          f"{len(reply.body)} bytes")
    if reply.status != 200:
        raise upstream_busy()
    geo = parse_geosearch(reply.body)
    if geo is None:
        raise upstream_busy()
    if geo.found and not geocode_matches_text(query, geo.label):
        return Geocode(found=False)
    return geo


# ------------------------------------------------------------------ places

CATEGORY_LABELS = {
    "atm": "ATM (cash machine)", "bank_or_credit_union": "Bank", "doctors_office": "Doctor's office",
    "optometry": "Optometrist", "delicatessen": "Deli", "drugstore": "Drugstore", "gym": "Gym",
    "bike_store": "Bike shop", "dental_clinic": "Dentist", "general_dentistry": "Dentist",
    "sport_or_fitness_facility": "Fitness center", "texmex_restaurant": "Tex-Mex restaurant",
}
PHONE_SHAPE = re.compile(r"\+?[0-9][0-9 ().\-/]{5,30}")
_URL_BREAKERS = frozenset('"<>\\^`{|}')


def category_label(key):
    if not isinstance(key, str) or not key:
        return "Place"
    return CATEGORY_LABELS.get(key) or key.replace("_", " ").strip().capitalize()


def https_url(value):
    """The value when it is a plain https address, otherwise ""."""
    value = (value or "").strip() if isinstance(value, str) else ""
    if not value or len(value) > 300 or not value.isprintable():
        return ""
    if any(ch.isspace() or ch in _URL_BREAKERS for ch in value):
        return ""
    try:
        parts = urlsplit(value)
        parts.port  # noqa: B018, raises on a malformed port
    except ValueError:
        return ""
    if parts.scheme.lower() != "https" or not parts.hostname or "@" in parts.netloc:
        return ""
    return "https" + value[len(parts.scheme):]  # schemes are case-insensitive; store one form


def place_from_row(row):
    gers, name, category, street, locality, postcode, phone, website, borough, lat, lng, distance = row
    area = " ".join(p for p in (locality, postcode) if p)
    return {
        "id": gers,
        "name": name,
        "category": category_label(category),
        "address": ", ".join(p for p in (street, area) if p),
        "phone": phone or "",
        "website": https_url(website),
        "borough": borough or "",
        "lat": round(float(lat), 6),
        "lng": round(float(lng), 6),
        "distance_m": round(float(distance)),
    }


# ------------------------------------------------------------------ the database


class Store:
    """The app role's view of Postgres: read access to the place data and three functions."""

    def __init__(self, conn):
        self._conn = conn

    def _row(self, sql, params):
        return self._conn.execute(sql, params).fetchone()

    def geocode_get(self, query):
        row = self._row("select found, lat, lng, label from places.geocode_get(%s)", (query,))
        return None if row is None else Geocode(found=row[0], lat=row[1], lng=row[2], label=row[3])

    def geocode_put(self, query, geo):
        self._conn.execute("select places.geocode_put(%s, %s, %s, %s, %s)",
                           (query, geo.found, geo.lat, geo.lng, geo.label))

    def neighborhood(self, alias, borocode):
        """(label, borough, lat, lng) for a neighborhood or park name, or None."""
        return self._row(
            "select n.label, n.boroname, n.lat, n.lng from places.neighborhoods n"
            " where n.alias = %s and (%s::smallint is null or n.borocode = %s)"
            " order by n.borocode limit 1",
            (alias, borocode, borocode),
        )

    def take_slot(self, kind, visitor, per_visitor, per_day):
        return self._row("select places.take_slot(%s, %s, %s, %s)", (kind, visitor, per_visitor, per_day))[0]

    def area_of(self, lat, lng):
        """(borough name or None, the loaded data release)."""
        return self._row(
            "select (select b.boroname from places.boroughs b"
            "         where st_intersects(b.geom, st_setsrid(st_makepoint(%s, %s), 4326))"
            "         order by b.borocode limit 1),"
            "       (select m.value from places.meta m where m.key = 'release')",
            (lng, lat),
        )

    def search_places(self, term, lat, lng, radius, limit):
        sql, params = search_params(term, lat, lng, radius, limit)
        return self._conn.execute(sql, params).fetchall()


@contextmanager
def open_store(url):
    import psycopg

    # Neon's pooled endpoint runs PgBouncer in transaction mode: psycopg's automatic prepared
    # statements must be off, and the URL must carry no "options" startup parameter (the pooler
    # rejects it), which is why every name below is schema-qualified instead of set by search_path.
    conn = psycopg.connect(url, autocommit=True, connect_timeout=CONNECT_TIMEOUT_S, prepare_threshold=None)
    try:
        yield Store(conn)
    finally:
        conn.close()


def _best_effort(action, *args):
    """A cache write. The geocoder call has already happened, so a failed write only costs a cache hit."""
    try:
        return action(*args)
    except Exception as exc:
        print(f"search: cache write skipped ({type(exc).__name__})")
        return None


# ------------------------------------------------------------------ the search


def search(data, ip, settings):
    request = parse_request(data)
    if not settings.db_url or not settings.hash_salt:
        print("search: PLACES_DB_URL or PLACES_HASH_SALT is not set, refusing")
        raise unavailable()
    visitor = visitor_key(ip, settings.hash_salt)
    try:
        with open_store(settings.db_url) as store:
            return _search(store, request, visitor, settings)
    except SearchFailure:
        raise
    except Exception as exc:
        # Fail closed: an error here usually means the database, and without it there are no
        # limits and no data, so nothing runs.
        print(f"search: refused after {type(exc).__name__}")
        raise unavailable() from None


def resolve_neighborhood(store, text):
    """A neighborhood, park or borough name from NYC's own list, without calling the geocoder."""
    for alias, borocode in neighborhood_candidates(text):
        if len(alias) < 3:
            continue
        row = store.neighborhood(alias, borocode)
        if row is not None:
            label, borough, lat, lng = row
            shown = label if label in (borough, "The Bronx") else f"{label}, {borough}"
            return Geocode(found=True, lat=float(lat), lng=float(lng), label=shown)
    return None


def _search(store, request, visitor, settings):
    take(store, "search", visitor, settings)
    location = None
    if request.where is not None:
        geo = resolve_neighborhood(store, request.where_text) or store.geocode_get(request.where)
        if geo is None:
            take(store, "geocode", visitor, settings)
            geo = geocode_live(request.where, settings)
            _best_effort(store.geocode_put, request.where, geo)
        if not geo.found:
            raise SearchFailure(
                "not_found",
                f"Couldn't find “{request.where_text}” in New York City. Try a neighborhood or a "
                "street address in one of the five boroughs.",
            )
        lat, lng, location = geo.lat, geo.lng, geo.label
    else:
        lat, lng = request.lat, request.lng

    borough, release = store.area_of(lat, lng)
    if borough is None:
        raise out_of_area(request.where_text)
    rows = store.search_places(request.term, lat, lng, request.radius, MAX_PLACES + 1)
    places = [place_from_row(row) for row in rows[:MAX_PLACES]]
    term = request.term
    return {
        "ok": True,
        "query": {
            "what": request.what,
            "term": {"kind": term.kind, "key": term.key, "label": term.label},
            "radius_m": request.radius,
        },
        "center": {"lat": round(lat, 6), "lng": round(lng, 6)},
        "location": location,
        "borough": borough,
        "places": places,
        "truncated": len(rows) > MAX_PLACES,
        "data_release": release,
    }


# ------------------------------------------------------------------ HTTP handling


def normalize_origin(value):
    """scheme://host[:port] in lower case, or "" when the value is not a plain web origin.

    Plain http is accepted only for local development hosts.
    """
    value = (value or "").strip()
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return ""
    scheme, host = parts.scheme.lower(), (parts.hostname or "").lower()
    if scheme not in ("http", "https") or not host or parts.username is not None:
        return ""
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        return ""
    if scheme == "http" and host not in LOCAL_HOSTS:
        return ""
    shown = f"[{host}]" if ":" in host else host
    return f"{scheme}://{shown}" + (f":{port}" if port else "")


def parse_origins(value):
    return frozenset(o for o in (normalize_origin(item) for item in (value or "").split(",")) if o)


def origin_allowed(origin, allowed):
    """Only the configured page origins may call this. A missing Origin header is refused.

    Browsers attach Origin to every POST. A script can forge it, which is why the daily limits
    exist as well.
    """
    normalized = normalize_origin(origin)
    return bool(normalized) and normalized in allowed


def is_json(content_type):
    return (content_type or "").split(";", 1)[0].strip().lower() == "application/json"


def client_ip(headers, fallback):
    # Vercel sets x-forwarded-for at its edge and overwrites any value the client sent.
    forwarded = (headers.get("x-forwarded-for") or "").split(",")[0].strip()
    return clean_text(forwarded)[:64] or fallback or "unknown"


def _reject_constant(name):
    raise ValueError(f"{name} is not allowed")


JSON_TYPE = "application/json; charset=utf-8"


class handler(BaseHTTPRequestHandler):
    def send_error(self, code, message=None, explain=None):
        # A short JSON reply in place of the standard library's HTML error page. A method with
        # no handler arrives here as 501; for a POST-only endpoint the right answer is 405.
        if code == 501:
            code = 405
        if code == 405:
            payload = SearchFailure("method_not_allowed", "Use POST for this endpoint.").payload()
        else:
            payload = {"ok": False, "error": {"code": "invalid", "message": "That request could not be read."}}
        body = json.dumps(payload).encode("utf-8")
        self.close_connection = True
        self.send_response(code)
        if code == 405:
            self.send_header("Allow", "POST")
        self.send_header("Content-Type", JSON_TYPE)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if getattr(self, "command", None) != "HEAD":
            self.wfile.write(body)

    def log_message(self, format, *args):
        # The standard line starts with the client address, which is left out on purpose.
        print("search: " + (format % args))

    def _reply(self, status, payload, retry_after=None):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", JSON_TYPE)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if retry_after is not None:
            self.send_header("Retry-After", str(int(retry_after)))
        self.end_headers()
        self.wfile.write(body)

    def _fail(self, failure):
        return self._reply(failure.status, failure.payload(), failure.retry_after)

    def do_POST(self):
        settings = load_settings()
        if not origin_allowed(self.headers.get("origin"), settings.allowed_origins):
            return self._fail(SearchFailure("forbidden", "This endpoint only answers the Place Finder page."))
        if not is_json(self.headers.get("content-type")):
            return self._reply(415, invalid("Send the search as JSON.").payload())
        try:
            length = int(self.headers.get("content-length") or 0)
        except ValueError:
            return self._fail(invalid("That request could not be read."))
        if length < 0:
            return self._fail(invalid("That request could not be read."))
        if length > MAX_BODY_BYTES:
            return self._reply(413, invalid("That request is too large.").payload())
        try:
            data = json.loads(self.rfile.read(length) or b"null", parse_constant=_reject_constant)
        except (ValueError, RecursionError):
            return self._fail(invalid("That request is not valid JSON."))
        fallback = self.client_address[0] if isinstance(self.client_address, tuple) and self.client_address else ""
        try:
            payload = search(data, client_ip(self.headers, fallback), settings)
        except SearchFailure as failure:
            return self._fail(failure)
        except Exception as exc:
            # Never a stack trace in the response; the type name in the log is enough to start from.
            print(f"search: unexpected {type(exc).__name__}")
            return self._fail(unavailable())
        return self._reply(200, payload)
