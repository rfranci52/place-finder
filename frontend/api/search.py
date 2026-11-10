"""Place search: POST /api/search.

A Vercel Python function in one self-contained file (standard library plus
psycopg), because Vercel does not reliably bundle sibling modules. It turns a
search term plus a location into nearby places from OpenStreetMap:

1. Nominatim turns the location text into coordinates.
2. The Overpass API returns matching places around that point.

Both are free community services with usage policies, and most of this file is
about staying inside them:

* Postgres caches geocodes for 30 days and place lists for 24 hours, so a
  repeated search makes no upstream call at all.
* Nominatim calls start at least one second apart across every running
  instance, through a reservation kept in the database.
* Each visitor (a salted hash of the IP address) gets a daily allowance of
  searches that need live data, and the whole site gets a daily allowance of
  upstream calls. When the database cannot be reached the search is refused,
  never run without those checks.
* After a 429 or 504 from the main Overpass instance the query is retried once
  on a documented public mirror, and the instance that refused is left alone
  for 30 seconds, as the Overpass wiki asks.
* Search text reaches an Overpass query only through regex_literal() and
  ql_string(), so it can never leave its string or its regular expression.

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

DEFAULT_USER_AGENT = "place-finder-demo/1.0 (portfolio project)"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
# Public instances are listed at
# https://wiki.openstreetmap.org/wiki/Overpass_API#Public_Overpass_API_instances (read 2026-11-09).
# The main instance is run by FOSSGIS. The mirror is the private.coffee instance ("previously known
# as overpass.kumi.systems"), which that table says may be used in any project, with no rate limit.
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OVERPASS_MIRROR_URL = "https://overpass.private.coffee/api/interpreter"

MAX_BODY_BYTES = 2048
MAX_WHAT = 40
MAX_WHERE = 80
MIN_TEXT = 2
RADII = (500, 1000, 1500, 2500)
DEFAULT_RADIUS = 1500
MAX_PLACES = 60
OVERPASS_ELEMENT_LIMIT = 200
OVERPASS_TIMEOUT_S = 10  # sent to Overpass as [timeout:10]
OVERPASS_HTTP_TIMEOUT_S = 12
NOMINATIM_HTTP_TIMEOUT_S = 6
NOMINATIM_MAX_WAIT_MS = 3000
OVERPASS_MAX_WAIT_MS = 2000
# The Overpass wiki asks clients to wait 30 seconds after a 429 before the next request.
PAUSE_AFTER_REFUSAL_S = 30
REQUEST_BUDGET_S = 25  # under the 30 second maxDuration set in vercel.json
MIN_ATTEMPT_S = 4
MAX_UPSTREAM_BYTES = 2_000_000
CONNECT_TIMEOUT_S = 8
# Statuses that mean "busy, ask someone else": these send the query to the mirror.
RETRYABLE = frozenset({429, 502, 503, 504})
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

# Defaults for the daily limits, overridable with environment variables. The Overpass wiki puts
# a regularly used website at under about 100 queries and 10 MB a day on the main instance, summed
# over all of its users, so the site-wide cap on upstream calls defaults to 100.
VISITOR_DAILY_DEFAULT = 10
UPSTREAM_DAILY_DEFAULT = 100

# Indirection points for the tests, which swap in a fake clock.
sleep = time.sleep
monotonic = time.monotonic


@dataclass(frozen=True)
class Settings:
    db_url: str
    hash_salt: str
    allowed_origins: frozenset
    user_agent: str
    visitor_daily: int
    upstream_daily: int
    nominatim_url: str
    overpass_url: str
    overpass_mirror_url: str


def _env_int(env, name, default, low, high):
    try:
        value = int(env.get(name, default))
    except (TypeError, ValueError):
        return default
    return min(max(value, low), high)


def _user_agent(value):
    """The configured User-Agent if it is plain printable ASCII, otherwise the default."""
    value = (value or "").strip()
    if not value or len(value) > 200 or not all(" " <= ch <= "~" for ch in value):
        return DEFAULT_USER_AGENT
    return value


def load_settings(env=None):
    env = os.environ if env is None else env
    return Settings(
        db_url=env.get("PLACES_DB_URL", ""),
        hash_salt=env.get("PLACES_HASH_SALT", ""),
        allowed_origins=parse_origins(env.get("PLACES_ALLOWED_ORIGINS", "")),
        user_agent=_user_agent(env.get("PLACES_USER_AGENT")),
        visitor_daily=_env_int(env, "PLACES_VISITOR_DAILY", VISITOR_DAILY_DEFAULT, 0, 1000),
        upstream_daily=_env_int(env, "PLACES_UPSTREAM_DAILY", UPSTREAM_DAILY_DEFAULT, 0, 10000),
        # Nominatim's policy asks that an app can switch services without a code change.
        nominatim_url=env.get("PLACES_NOMINATIM_URL") or NOMINATIM_URL,
        overpass_url=env.get("PLACES_OVERPASS_URL") or OVERPASS_URL,
        overpass_mirror_url=env.get("PLACES_OVERPASS_MIRROR_URL") or OVERPASS_MIRROR_URL,
    )


# ------------------------------------------------------------------ errors

# code: HTTP status. The page renders each code its own way.
ERRORS = {
    "invalid": 400,
    "forbidden": 403,
    "not_found": 404,
    "method_not_allowed": 405,
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
        "The free OpenStreetMap service is busy right now. Please try again in a few seconds.",
        retry_after=10,
    )


def unavailable():
    return SearchFailure(
        "unavailable",
        "Search is paused because the demo's database can't be reached. Please try again in a minute.",
        retry_after=30,
    )


def seconds_until_utc_midnight(now=None):
    now = now or datetime.now(timezone.utc)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(int((tomorrow - now).total_seconds()), 1)


def rate_limited(reason, per_visitor):
    wait = seconds_until_utc_midnight()
    hours = max(1, round(wait / 3600))
    when = "about an hour" if hours == 1 else f"about {hours} hours"
    if reason == "visitor":
        message = (
            f"You've run {per_visitor} new searches today, the limit for one visitor on this demo. "
            f"Searches that are already cached still work. New ones open up again in {when}."
        )
    else:
        message = (
            "This demo has used today's share of the free OpenStreetMap services. "
            f"Searches that are already cached still work. New ones open up again in {when}."
        )
    return SearchFailure("rate_limited", message, retry_after=wait)


# ------------------------------------------------------------------ text

_QUOTES = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"'})


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


# ------------------------------------------------------------------ Overpass QL escaping

# The characters POSIX extended regular expressions treat as special outside a bracket expression.
# A backslash before each one makes it literal, in POSIX and in Python's re alike. "]" and "}" are
# ordinary outside brackets and braces, and a backslash before them is undefined in POSIX, so they
# are left alone.
_ERE_SPECIAL = frozenset("\\.[()*+?{|^$")


def regex_literal(text):
    """A POSIX extended regular expression that matches exactly this text."""
    return "".join("\\" + ch if ch in _ERE_SPECIAL else ch for ch in text)


def ql_string(value):
    """A double-quoted Overpass QL string that decodes to exactly this value.

    Overpass decodes C-style escapes in strings (backslash and quote among them), so both are
    escaped. Control characters are refused rather than escaped; request text has them removed
    before it gets here, so meeting one means a bug, and a bug should not reach Overpass.
    """
    if any(unicodedata.category(ch).startswith("C") for ch in value):
        raise ValueError("control character in an Overpass string")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def tag_is(key, value):
    return f"[{ql_string(key)}={ql_string(value)}]"


def tag_in(key, *values):
    pattern = "^(" + "|".join(regex_literal(v) for v in values) + ")$"
    return f"[{ql_string(key)}~{ql_string(pattern)}]"


def tag_lists(key, *words):
    """The tag's semicolon-separated list holds one of these words, ignoring case."""
    pattern = "(^|;) *(" + "|".join(regex_literal(w) for w in words) + ") *(;|$)"
    return f"[{ql_string(key)}~{ql_string(pattern)},i]"


def tag_contains(key, text):
    """The tag's value contains this text, ignoring case."""
    return f"[{ql_string(key)}~{ql_string(regex_literal(text))},i]"


def keys_match(*keys):
    """The element has a tag with one of these keys."""
    return f"[~{ql_string('^(' + '|'.join(regex_literal(k) for k in keys) + ')$')}~{ql_string('.')}]"


# ------------------------------------------------------------------ vocabulary


@dataclass(frozen=True)
class Category:
    key: str
    label: str
    group: str
    synonyms: tuple
    clauses: tuple  # Overpass tag filters; a place matching any one clause is a match


FOOD_PLACE = tag_in("amenity", "restaurant", "fast_food")
FOOD, EVERYDAY, HEALTH = "Food and drink", "Everyday services", "Health"


def _cuisine(*words):
    return FOOD_PLACE + tag_lists("cuisine", *words)


VOCABULARY = (
    Category("coffee", "Coffee", FOOD, ("cafe", "coffee shop", "espresso", "latte"),
             (tag_is("amenity", "cafe"), tag_is("shop", "coffee"))),
    Category("pizza", "Pizza", FOOD, ("pizzeria", "pizza place", "slice"),
             (_cuisine("pizza"), FOOD_PLACE + tag_contains("name", "pizz"))),
    Category("bakery", "Bakery", FOOD, ("bread", "pastry", "pastries", "bake shop"),
             (tag_is("shop", "bakery"), tag_is("shop", "pastry"))),
    Category("bar", "Bar", FOOD, ("pub", "drinks", "cocktails", "beer", "brewery", "tavern"),
             (tag_in("amenity", "bar", "pub", "biergarten"),)),
    Category("restaurant", "Restaurant", FOOD, ("food", "dinner", "lunch", "eat", "dining"),
             (tag_is("amenity", "restaurant"),)),
    Category("fast food", "Fast food", FOOD, ("fastfood", "takeout", "take out", "quick bite"),
             (tag_is("amenity", "fast_food"),)),
    Category("ice cream", "Ice cream", FOOD, ("gelato", "frozen yogurt", "dessert"),
             (tag_is("amenity", "ice_cream"), tag_is("shop", "ice_cream"))),
    Category("burger", "Burgers", FOOD, ("hamburger", "cheeseburger"),
             (_cuisine("burger"),)),
    Category("sushi", "Sushi", FOOD, ("japanese", "ramen"),
             (_cuisine("sushi", "japanese", "ramen"),)),
    Category("mexican", "Mexican", FOOD, ("taco", "tacos", "burrito", "taqueria"),
             (_cuisine("mexican", "tex-mex", "taco", "tacos", "burrito"),)),
    Category("chinese", "Chinese", FOOD, ("dim sum", "dumplings"),
             (_cuisine("chinese", "dim_sum", "dumpling", "cantonese", "sichuan"),)),
    Category("deli", "Deli", FOOD, ("sandwich", "sandwiches", "bagel", "bagels"),
             (tag_is("shop", "deli"), _cuisine("sandwich", "deli", "bagel"))),
    Category("liquor store", "Liquor store", FOOD, ("liquor", "wine", "wine shop", "beer store"),
             (tag_in("shop", "alcohol", "wine", "beverages"),)),

    Category("grocery", "Grocery", EVERYDAY, ("groceries", "grocery store", "grocer", "food market"),
             (tag_in("shop", "supermarket", "grocery", "greengrocer"),)),
    Category("supermarket", "Supermarket", EVERYDAY, ("super market",),
             (tag_is("shop", "supermarket"),)),
    Category("convenience store", "Convenience store", EVERYDAY, ("convenience", "bodega", "corner store"),
             (tag_is("shop", "convenience"),)),
    Category("laundry", "Laundry", EVERYDAY, ("laundromat", "dry cleaner", "dry cleaning", "laundry service"),
             (tag_in("shop", "laundry", "dry_cleaning"),)),
    Category("bank", "Bank", EVERYDAY, ("credit union",),
             (tag_is("amenity", "bank"),)),
    Category("atm", "ATM (cash machine)", EVERYDAY, ("cash machine", "cash"),
             (tag_is("amenity", "atm"), tag_is("amenity", "bank") + tag_is("atm", "yes"))),
    Category("library", "Library", EVERYDAY, ("public library",),
             (tag_is("amenity", "library"),)),
    Category("park", "Park", EVERYDAY, ("green space", "garden"),
             (tag_is("leisure", "park"),)),
    Category("playground", "Playground", EVERYDAY, ("play area",),
             (tag_is("leisure", "playground"),)),
    Category("gym", "Gym", EVERYDAY, ("fitness", "fitness center", "workout"),
             (tag_is("leisure", "fitness_centre"),)),
    Category("post office", "Post office", EVERYDAY, ("post", "mail"),
             (tag_is("amenity", "post_office"),)),
    Category("gas station", "Gas station", EVERYDAY, ("gas", "petrol", "fuel"),
             (tag_is("amenity", "fuel"),)),
    Category("hardware store", "Hardware store", EVERYDAY, ("hardware", "home improvement"),
             (tag_in("shop", "hardware", "doityourself"),)),
    Category("bookstore", "Bookstore", EVERYDAY, ("books", "bookshop", "book store"),
             (tag_is("shop", "books"),)),
    Category("hair salon", "Hair salon", EVERYDAY, ("barber", "barbershop", "salon", "haircut"),
             (tag_is("shop", "hairdresser"),)),
    Category("bike shop", "Bike shop", EVERYDAY, ("bike", "bicycle", "bicycle shop", "bike repair"),
             (tag_is("shop", "bicycle"),)),
    Category("hotel", "Hotel", EVERYDAY, ("motel", "hostel", "lodging", "guest house"),
             (tag_in("tourism", "hotel", "motel", "hostel", "guest_house"),)),
    Category("museum", "Museum", EVERYDAY, ("gallery", "art museum"),
             (tag_is("tourism", "museum"),)),
    Category("pet store", "Pet store", EVERYDAY, ("pet shop", "pet supplies", "pets"),
             (tag_is("shop", "pet"),)),
    Category("movie theater", "Movie theater", EVERYDAY, ("cinema", "movies", "movie theatre"),
             (tag_is("amenity", "cinema"),)),

    Category("pharmacy", "Pharmacy", HEALTH, ("drugstore", "drug store", "chemist"),
             (tag_is("amenity", "pharmacy"), tag_is("healthcare", "pharmacy"))),
    Category("clinic", "Clinic", HEALTH, ("urgent care", "walk in clinic", "health center", "medical center"),
             (tag_is("amenity", "clinic"), tag_is("healthcare", "clinic"))),
    Category("hospital", "Hospital", HEALTH, ("emergency room", "emergency"),
             (tag_is("amenity", "hospital"), tag_is("healthcare", "hospital"))),
    Category("dentist", "Dentist", HEALTH, ("dental", "dental office", "orthodontist"),
             (tag_is("amenity", "dentist"), tag_is("healthcare", "dentist"))),
    Category("doctor", "Doctor", HEALTH, ("physician", "primary care", "medical office"),
             (tag_is("amenity", "doctors"), tag_is("healthcare", "doctor"))),
    Category("eye care", "Eye care", HEALTH, ("optician", "optometrist", "eye doctor", "glasses", "eyeglasses"),
             (tag_is("shop", "optician"), tag_is("healthcare", "optometrist"))),
    Category("veterinarian", "Veterinarian", HEALTH, ("vet", "animal hospital", "veterinary"),
             (tag_is("amenity", "veterinary"),)),
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

    @property
    def cache_key(self):
        return ("c:" if self.kind == "category" else "n:") + self.key


def _singulars(folded):
    if folded.endswith("ies") and len(folded) > 4:
        yield folded[:-3] + "y"
    if folded.endswith("es") and len(folded) > 3:
        yield folded[:-2]
    if folded.endswith("s") and len(folded) > 2:
        yield folded[:-1]


def resolve_term(text):
    """A vocabulary category when the text names one, otherwise a case-insensitive name match."""
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
        "Add a location, like a neighborhood, city or postal code.",
        f"Keep the location to {MAX_WHERE} characters.",
    )
    return SearchRequest(what=what, term=term, radius=radius, where=location_key(where), where_text=where)


# ------------------------------------------------------------------ the Overpass query

NAMED = keys_match("name", "brand", "operator")
POI_KEYS = ("amenity", "shop", "healthcare", "leisure", "tourism", "craft", "office", "club")
IS_POI = keys_match(*POI_KEYS)


def build_query(term, lat, lng, radius):
    """The Overpass QL query for a term around a point. Every string in it comes from ql_string()."""
    around = f"(around:{int(radius)},{lat:.3f},{lng:.3f})"
    if term.kind == "category":
        clauses = [clause + NAMED for clause in CATEGORIES[term.key].clauses]
    else:
        clauses = [IS_POI + tag_contains("name", term.key), IS_POI + tag_contains("cuisine", term.key)]
    body = "".join(f"nwr{clause}{around};" for clause in clauses)
    return f"[out:json][timeout:{OVERPASS_TIMEOUT_S}];({body});out center {OVERPASS_ELEMENT_LIMIT};"


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


def http_request(url, *, user_agent, timeout, form=None):
    """One request to an upstream service. Network trouble comes back as Reply(None), never raises."""
    data = urlencode(form).encode("utf-8") if form is not None else None
    headers = {"User-Agent": user_agent, "Accept": "application/json", "Accept-Encoding": "gzip"}
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
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


def call_upstream(name, url, settings, timeout, form=None):
    started = monotonic()
    reply = http_request(url, user_agent=settings.user_agent, timeout=timeout, form=form)
    # One line per upstream call, with no search text and no visitor details.
    print(f"upstream {name}: {reply.status or 'no reply'} in {round((monotonic() - started) * 1000)} ms, "
          f"{len(reply.body)} bytes")
    return reply


class Deadline:
    def __init__(self, seconds):
        self.end = monotonic() + seconds

    def left(self):
        return self.end - monotonic()


# ------------------------------------------------------------------ limits


class LiveBudget:
    """Counts one search's upstream calls against the daily limits, visitor first."""

    def __init__(self, store, visitor, settings):
        self.store = store
        self.visitor = visitor
        self.settings = settings
        self.started = False

    def take(self):
        if not self.started:
            verdict = self.store.begin_live_search(
                self.visitor, self.settings.visitor_daily, self.settings.upstream_daily
            )
            if verdict == "visitor_limit":
                raise rate_limited("visitor", self.settings.visitor_daily)
            if verdict != "ok":
                raise rate_limited("global", self.settings.visitor_daily)
            self.started = True
        elif not self.store.take_upstream_slot(self.settings.upstream_daily):
            raise rate_limited("global", self.settings.visitor_daily)


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


# ------------------------------------------------------------------ geocoding


@dataclass(frozen=True)
class Geocode:
    found: bool
    lat: float | None = None
    lng: float | None = None
    label: str | None = None


def _short_label(display_name):
    parts = [part.strip() for part in clean_text(display_name).split(",") if part.strip()]
    return ", ".join(parts[:3])[:200]


def parse_geocode(body):
    """Nominatim's jsonv2 answer as a Geocode, or None when it is not the expected shape."""
    try:
        data = json.loads(body)
    except ValueError:
        return None
    if not isinstance(data, list):
        return None
    if not data:
        return Geocode(found=False)
    first = data[0]
    if not isinstance(first, dict):
        return None
    try:
        lat, lng = float(first["lat"]), float(first["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (math.isfinite(lat) and math.isfinite(lng) and -90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    label = first.get("display_name")
    return Geocode(found=True, lat=lat, lng=lng, label=_short_label(label) if isinstance(label, str) else None)


def geocode_live(store, budget, query, settings, deadline):
    if deadline.left() < MIN_ATTEMPT_S:
        raise upstream_busy()
    # The reservation keeps Nominatim calls at least a second apart across every instance.
    wait_ms = store.reserve_call("nominatim", NOMINATIM_MAX_WAIT_MS)
    if wait_ms is None:
        raise upstream_busy()
    budget.take()
    if wait_ms > 0:
        sleep(wait_ms / 1000)
    params = {"q": query, "format": "jsonv2", "limit": 1, "accept-language": "en"}
    reply = call_upstream("nominatim", f"{settings.nominatim_url}?{urlencode(params)}", settings,
                          timeout=min(NOMINATIM_HTTP_TIMEOUT_S, max(deadline.left() - 1, 1)))
    if reply.status != 200:
        store.pause_upstream("nominatim", PAUSE_AFTER_REFUSAL_S)
        raise upstream_busy()
    geo = parse_geocode(reply.body)
    if geo is None:
        raise upstream_busy()
    return geo


# ------------------------------------------------------------------ places

_RETRY, _FAIL = "retry", "fail"


def read_overpass(reply):
    """The element list, or _RETRY when the instance was busy, or _FAIL when retrying cannot help."""
    if reply.status is None or reply.status in RETRYABLE:
        return _RETRY
    if reply.status != 200:
        return _FAIL
    try:
        data = json.loads(reply.body)
    except ValueError:
        return _RETRY  # an overloaded server can answer 200 with an error page
    if not isinstance(data, dict) or not isinstance(data.get("elements"), list):
        return _RETRY
    remark = data.get("remark")
    # A query that ran out of time or memory still answers 200, with a remark and partial results.
    if isinstance(remark, str) and "runtime error" in remark.lower():
        return _RETRY
    return data["elements"]


def fetch_elements(store, budget, query, settings, deadline):
    """Main instance first, then the mirror once. An instance that refused is skipped for 30 seconds."""
    for name, url in (("overpass", settings.overpass_url), ("overpass-mirror", settings.overpass_mirror_url)):
        if deadline.left() < MIN_ATTEMPT_S:
            break
        wait_ms = store.reserve_call(name, OVERPASS_MAX_WAIT_MS)
        if wait_ms is None:
            continue  # paused after a refusal, or already busy with other searches
        budget.take()
        if wait_ms > 0:
            sleep(wait_ms / 1000)
        reply = call_upstream(name, url, settings, form={"data": query},
                              timeout=min(OVERPASS_HTTP_TIMEOUT_S, max(deadline.left() - 1, 1)))
        outcome = read_overpass(reply)
        if isinstance(outcome, list):
            return outcome
        store.pause_upstream(name, PAUSE_AFTER_REFUSAL_S)
        if outcome == _FAIL:
            break
    raise upstream_busy()


TAG_LABELS = {
    ("amenity", "cafe"): "Cafe", ("shop", "coffee"): "Coffee shop", ("amenity", "fast_food"): "Fast food",
    ("amenity", "ice_cream"): "Ice cream", ("shop", "ice_cream"): "Ice cream",
    ("amenity", "biergarten"): "Beer garden", ("shop", "pastry"): "Pastry shop",
    ("shop", "alcohol"): "Liquor store", ("shop", "wine"): "Wine shop", ("shop", "beverages"): "Drinks shop",
    ("shop", "convenience"): "Convenience store", ("shop", "greengrocer"): "Greengrocer",
    ("shop", "dry_cleaning"): "Dry cleaner", ("amenity", "atm"): "ATM (cash machine)",
    ("leisure", "fitness_centre"): "Gym", ("amenity", "post_office"): "Post office",
    ("amenity", "fuel"): "Gas station", ("shop", "doityourself"): "Home improvement",
    ("shop", "books"): "Bookstore", ("shop", "hairdresser"): "Hair salon", ("shop", "bicycle"): "Bike shop",
    ("tourism", "guest_house"): "Guest house", ("shop", "pet"): "Pet store",
    ("amenity", "cinema"): "Movie theater", ("amenity", "doctors"): "Doctor",
    ("healthcare", "optometrist"): "Optometrist", ("amenity", "veterinary"): "Veterinarian",
}
CUISINE_PLACES = frozenset({"restaurant", "fast_food", "cafe"})
PHONE_SHAPE = re.compile(r"\+?[0-9][0-9 ().\-/]{5,30}")
_URL_BREAKERS = frozenset('"<>\\^`{|}')


def _tag(tags, key, limit=200):
    value = tags.get(key)
    return clean_text(value)[:limit] if isinstance(value, str) else ""


def _pretty(value):
    return value.replace("_", " ").replace(";", " / ").strip().capitalize()


def category_label(tags):
    for key in POI_KEYS:
        value = _tag(tags, key, 60)
        if value:
            label = TAG_LABELS.get((key, value)) or _pretty(value)
            cuisine = _tag(tags, "cuisine", 60).split(";")[0].strip()
            if key == "amenity" and value in CUISINE_PLACES and cuisine:
                label = f"{label} · {_pretty(cuisine).lower()}"
            return label[:60]
    return "Place"


def address_of(tags):
    full = _tag(tags, "addr:full", 160)
    if full:
        return full
    street = " ".join(p for p in (_tag(tags, "addr:housenumber", 20), _tag(tags, "addr:street", 80)) if p)
    region = " ".join(p for p in (_tag(tags, "addr:state", 30), _tag(tags, "addr:postcode", 20)) if p)
    return ", ".join(p for p in (street, _tag(tags, "addr:city", 60), region) if p)[:160]


def phone_of(tags):
    first = (_tag(tags, "phone") or _tag(tags, "contact:phone")).split(";")[0].strip()
    return first if PHONE_SHAPE.fullmatch(first) else ""


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
    return value


def distance_m(lat1, lng1, lat2, lng2):
    """Great-circle distance in meters."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6_371_000 * math.asin(min(1.0, math.sqrt(a)))


def place_from(element, lat, lng, need_poi_tag):
    if not isinstance(element, dict) or not isinstance(element.get("tags"), dict):
        return None
    kind, osm_id, tags = element.get("type"), element.get("id"), element["tags"]
    if kind not in ("node", "way", "relation") or type(osm_id) is not int or osm_id <= 0:
        return None
    point = element if kind == "node" else element.get("center")
    if not isinstance(point, dict):
        return None
    try:
        plat, plng = float(point["lat"]), float(point["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (math.isfinite(plat) and math.isfinite(plng) and -90 <= plat <= 90 and -180 <= plng <= 180):
        return None
    name = _tag(tags, "name", 100) or _tag(tags, "brand", 100) or _tag(tags, "operator", 100)
    if not name or (need_poi_tag and not any(_tag(tags, key) for key in POI_KEYS)):
        return None
    return {
        "id": f"{kind}/{osm_id}",
        "name": name,
        "category": category_label(tags),
        "address": address_of(tags),
        "phone": phone_of(tags),
        "hours": _tag(tags, "opening_hours", 160),
        "website": https_url(tags.get("website") or tags.get("contact:website") or tags.get("url")),
        "lat": round(plat, 6),
        "lng": round(plng, 6),
        "osm_url": f"https://www.openstreetmap.org/{kind}/{osm_id}",
        "distance_m": round(distance_m(lat, lng, plat, plng)),
    }


def normalize_places(elements, term, lat, lng):
    """(the closest places, up to MAX_PLACES; whether there were more matches than shown)."""
    places, seen = [], set()
    for element in elements:
        place = place_from(element, lat, lng, need_poi_tag=term.kind == "name")
        if place is None:
            continue
        # The same shop is often mapped twice, as a point and as its building.
        key = (place["name"].casefold(), round(place["lat"], 4), round(place["lng"], 4))
        if key in seen:
            continue
        seen.add(key)
        places.append(place)
    places.sort(key=lambda p: (p["distance_m"], p["name"]))
    truncated = len(elements) >= OVERPASS_ELEMENT_LIMIT or len(places) > MAX_PLACES
    return places[:MAX_PLACES], truncated


# ------------------------------------------------------------------ the database


class Store:
    """The app role's whole view of Postgres: eight security definer functions, no tables."""

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

    def results_get(self, term_key, lat_e3, lng_e3, radius):
        row = self._row("select places, truncated, fetched_at from places.results_get(%s, %s, %s, %s)",
                        (term_key, lat_e3, lng_e3, radius))
        return None if row is None else (row[0], row[1], row[2])

    def results_put(self, term_key, lat_e3, lng_e3, radius, places, truncated):
        row = self._row("select places.results_put(%s, %s, %s, %s, %s::jsonb, %s)",
                        (term_key, lat_e3, lng_e3, radius, json.dumps(places), truncated))
        return row[0] if row else None

    def begin_live_search(self, visitor, per_visitor, upstream_limit):
        return self._row("select places.begin_live_search(%s, %s, %s)", (visitor, per_visitor, upstream_limit))[0]

    def take_upstream_slot(self, upstream_limit):
        return bool(self._row("select places.take_upstream_slot(%s)", (upstream_limit,))[0])

    def reserve_call(self, upstream, max_wait_ms):
        return self._row("select places.reserve_call(%s, %s)", (upstream, max_wait_ms))[0]

    def pause_upstream(self, upstream, seconds):
        self._conn.execute("select places.pause_upstream(%s, %s)", (upstream, seconds))


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
    """A cache write. The upstream call has already happened, so a failed write only costs a cache hit."""
    try:
        return action(*args)
    except Exception as exc:
        print(f"search: cache write skipped ({type(exc).__name__})")
        return None


def _iso(moment):
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ------------------------------------------------------------------ the search


def search(data, ip, settings):
    request = parse_request(data)
    if not settings.db_url or not settings.hash_salt:
        print("search: PLACES_DB_URL or PLACES_HASH_SALT is not set, refusing")
        raise unavailable()
    visitor = visitor_key(ip, settings.hash_salt)
    deadline = Deadline(REQUEST_BUDGET_S)
    try:
        with open_store(settings.db_url) as store:
            return _search(store, request, visitor, settings, deadline)
    except SearchFailure:
        raise
    except Exception as exc:
        # Fail closed: an error here usually means the database, and without it there are no
        # limits, no pacing and no cache, so nothing runs.
        print(f"search: refused after {type(exc).__name__}")
        raise unavailable() from None


def _search(store, request, visitor, settings, deadline):
    budget = LiveBudget(store, visitor, settings)
    location = None
    if request.where is not None:
        geo = store.geocode_get(request.where)
        if geo is None:
            geo = geocode_live(store, budget, request.where, settings, deadline)
            _best_effort(store.geocode_put, request.where, geo)
        if not geo.found:
            raise SearchFailure(
                "not_found",
                f"Couldn't find \u201c{request.where_text}\u201d. Try a neighborhood, city or postal code.",
            )
        lat, lng, location = geo.lat, geo.lng, geo.label
    else:
        lat, lng = request.lat, request.lng

    # Rounded to 0.001 degrees, about 110 meters of latitude: nearby searches share one cache entry,
    # and the query runs around the rounded point so the entry matches its key exactly.
    lat_e3, lng_e3 = round(lat * 1000), round(lng * 1000)
    clat, clng = lat_e3 / 1000, lng_e3 / 1000
    term = request.term
    hit = store.results_get(term.cache_key, lat_e3, lng_e3, request.radius)
    if hit is not None:
        places, truncated, fetched_at = hit
        cached = True
    else:
        elements = fetch_elements(store, budget, build_query(term, clat, clng, request.radius), settings, deadline)
        places, truncated = normalize_places(elements, term, clat, clng)
        fetched_at = _best_effort(store.results_put, term.cache_key, lat_e3, lng_e3, request.radius,
                                  places, truncated) or datetime.now(timezone.utc)
        cached = False
    return {
        "ok": True,
        "query": {
            "what": request.what,
            "term": {"kind": term.kind, "key": term.key, "label": term.label},
            "radius_m": request.radius,
        },
        "center": {"lat": clat, "lng": clng},
        "location": location,
        "places": places,
        "truncated": bool(truncated),
        "cached": cached,
        "fetched_at": _iso(fetched_at),
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
