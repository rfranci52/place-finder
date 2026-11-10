# Place Finder

Find coffee, pharmacies, parks and other everyday places near any address, on a map, from
[OpenStreetMap](https://www.openstreetmap.org) open data. It is a 2026 rebuild of a 2019 bootcamp
project, and most of the work is in the part you cannot see: keeping a public demo inside the usage
policies of the free community services it depends on.

## What it does

- Type **what** (a category such as "pharmacy", or any name such as "Joe's") and **where** (a
  neighborhood, city or postal code), or tap a quick pick: Coffee, Pizza, Pharmacy, Clinic, Grocery,
  Park.
- Move the map and choose **Search this area** to search around the map center instead.
- Up to 60 places come back, nearest first, each with its category, address, phone, opening hours,
  website and a link to its OpenStreetMap entry. Hovering or selecting a place in the list highlights
  it on the map, and clicking a marker selects it in the list.
- Loading, empty, rate-limited, not-found and service-busy states each have their own message.
- It works on a phone, with a keyboard, and with a screen reader (labeled form, live status messages,
  a labeled map region, real buttons and links).

## 2019 and 2026

| | 2019 bootcamp version | 2026 rebuild |
|---|---|---|
| Frontend | Create React App (CRA) | Vite, React 19, TypeScript |
| Map | Google Maps through `react-google-maps` | Leaflet with OpenStreetMap tiles, no key |
| Place data | Yelp, called from the browser through a public CORS (Cross-Origin Resource Sharing) proxy | OpenStreetMap: Nominatim for geocoding, the Overpass API for places, called from a server function |
| Secrets | An Application Programming Interface (API) key in client-side code | No third-party keys at all; the only secrets are a database address and a hash salt, both server-side |
| When a service is unavailable | The page stopped working | Stable error codes, each with its own message on the page |
| Fair use of the services | Not addressed | Caching, pacing and daily limits enforced in Postgres |

The original is at [rfranci52/react-google-maps](https://github.com/rfranci52/react-google-maps).

## Architecture

```
Browser: React + Leaflet (frontend/src)
  |  map tiles, straight from tile.openstreetmap.org
  |
  |  POST /api/search  {"what": "coffee", "where": "Washington Square Park, New York"}
  v
Vercel Python function: frontend/api/search.py  (standard library + psycopg, one file)
  |-- Postgres (Neon, pooled), schema "places", role places_app:
  |     geocode cache, place cache, daily counters, upstream pacing
  |-- Nominatim         location text -> coordinates   (at most one call per second, site-wide)
  '-- Overpass API      coordinates + term -> places    (main instance, then one mirror retry)
```

One search, step by step:

1. **Check the request.** Only the site's own origin, JavaScript Object Notation (JSON) only, at most 2 kilobytes, POST only. `what` is
   2 to 40 characters; then either `where` (2 to 80 characters) or a `lat`/`lng` pair, plus an
   optional `radius` of 500, 1000, 1500 or 2500 meters.
2. **Resolve the term.** About 40 curated categories (food and drink, everyday services, health),
   with synonyms and plurals, map to OpenStreetMap tag filters: "cafe" and "coffee shop" both mean
   `amenity=cafe` or `shop=coffee`; "pizza" means a restaurant or fast-food place whose `cuisine`
   includes pizza or whose name contains "pizz". Anything else becomes a case-insensitive match on
   place names (and cuisines), restricted to things that are actually places, so a search for
   "Broadway" does not return street segments.
3. **Geocode** the location, from the cache when possible.
4. **Find places** around the point, from the cache when possible; otherwise one Overpass query.
5. **Normalize.** Drop unnamed and malformed elements, merge a shop mapped twice (as a point and as
   its building), keep websites only when they are `https://`, sort by distance, keep 60.

The response is `{"ok": true, "query", "center", "location", "places", "truncated", "cached",
"fetched_at"}`, and every failure is `{"ok": false, "error": {"code", "message", "retry_after"?}}` with a
matching HTTP (Hypertext Transfer Protocol) status:

| code | HTTP status | when |
|---|---|---|
| `invalid` | 400, 413, 415 | the request is malformed, too large or not JSON |
| `forbidden` | 403 | the Origin header is not the site |
| `not_found` | 404 | the location could not be geocoded |
| `method_not_allowed` | 405 | anything but POST (with an `Allow: POST` header) |
| `rate_limited` | 429 | a daily limit is used up (with `Retry-After`) |
| `upstream_busy` | 503 | the OpenStreetMap services refused or did not answer in time |
| `unavailable` | 503 | the database cannot be reached, so the search is refused |

No response ever contains a stack trace.

## Fair use and safety

The services are free, run by volunteers and donations, and each has a usage policy. The design
follows them, and the numbers below come from those policies.

**Caching (Postgres).** Geocodes are kept 30 days, keyed on the normalized location text (lower
case, single spaces, one comma style), and the text sent to Nominatim is that same normalized text,
so a cached answer always belongs to its key. "Not found" answers are cached too. Place lists are
kept 24 hours, keyed on the term (synonyms share a key), the point rounded to 0.001 degrees (about
110 meters) and the radius; the query runs around the rounded point for the same reason. A cached
answer makes no upstream call and does not count against any limit. Both caches are bounded by
construction: a key hashes to one of a fixed number of slots (16,384 and 4,096) and a write replaces
its slot, so the tables cannot grow without limit and need no cleanup job.

**Pacing.** Nominatim allows at most one request per second for the whole application. Serverless
instances do not share memory, so the spacing lives in the database: `places.reserve_call` takes a
row lock on the service's row, reserves the next start time at least 1.1 seconds after the previous
one, and tells the caller how long to wait (or refuses when the queue is longer than 3 seconds,
which shows as "busy"). The lock is transaction-scoped on purpose: Neon's pooler runs PgBouncer in
transaction mode, where a session-level advisory lock could outlive the transaction on a shared
connection. After any refusal from a service, that service is left alone for 30 seconds, as the
Overpass wiki asks. A check constraint keeps the Nominatim interval from ever being set under one
second.

**Limits.** A visitor is an HMAC (Hash-based Message Authentication Code) of their Internet
Protocol (IP) address with a secret salt, with each IPv6 /64 counted as one visitor; the address
itself is never stored or logged. Each visitor gets 10 searches a day that need live data, and the
whole site gets 100 upstream calls a day. The site figure follows the Overpass wiki, which puts a
regularly used website at under about 100 queries and 10 megabytes a day on the main instance,
summed across all of its users. The visitor is checked first, so a visitor over their own limit never
spends the site's allowance, and a search the site limit refuses at its start does not count against
the visitor. If the
database cannot be reached the search is refused: there is no path that calls a service without
the cache, the pacing and the counters.

**Query safety.** Search text reaches an Overpass query through exactly two functions.
`regex_literal()` puts a backslash before every POSIX (Portable Operating System Interface) extended
regular expression special character, and `ql_string()` escapes backslashes and quotes for an
Overpass QL (Query Language) string and refuses control characters, which request cleaning has
already removed. The tests read every generated query back with the escape rules from the Overpass
documentation and check, for thousands of generated and hand-picked inputs (quotes, backslashes,
brackets, line breaks, Unicode tricks, lone surrogates), that the query's structure never changes
and that the regular expression matches the typed text literally.

**Least privilege.** The app logs in as `places_app`, which has no privilege on any table. It can
run eight `SECURITY DEFINER` functions, each with a fixed `search_path`, and the rules (lifetimes,
slot counts, the one-second floor, row size limits) are enforced in the database by those functions
and by check constraints. The role also gets short statement, lock and idle timeouts.

**The rest.** Websites from OpenStreetMap are passed on only when they are plain `https://`
addresses. The page sends the security headers in `frontend/vercel.json`, including a
Content-Security-Policy (CSP) in report-only mode that allows images only from the site, inline
data images and the tile server. Nothing autocompletes as you type, which Nominatim's policy forbids; a request is sent
only when you search.

The usage policies, for reference: [Nominatim](https://operations.osmfoundation.org/policies/nominatim/)
(at most one request per second, an identifying User-Agent, visible attribution, cache results, no
autocomplete, be able to switch services without a code change),
[Overpass public instances and guidance](https://wiki.openstreetmap.org/wiki/Overpass_API#Public_Overpass_API_instances),
and [OpenStreetMap tiles](https://operations.osmfoundation.org/policies/tiles/). The service URLs (Uniform
Resource Locators) and the User-Agent are environment variables for that reason.

## Run it locally

You need [uv](https://docs.astral.sh/uv/), Node.js 20.19 or 22.12 or later (Vite 8's requirement), and a
local Postgres.

```sh
uv sync                          # Python environment in .venv
(cd frontend && npm install)

# A local database for development
createdb places_dev
psql -d places_dev -f db/schema.sql
psql -d places_dev -c "alter role places_app login password 'local-dev-only'"
```

Put the settings in an env file outside the repository, for example `../site_private/.env.places.dev`:

```sh
PLACES_DB_URL=postgresql://places_app:local-dev-only@127.0.0.1:5432/places_dev
PLACES_HASH_SALT=any-long-random-string
```

Then start the two halves:

```sh
uv run python scripts/dev_api.py --env-file ../site_private/.env.places.dev   # 127.0.0.1:8790
(cd frontend && npm run dev)                                                  # http://localhost:5174
```

Vite proxies `/api` to the local server, which serves the same `frontend/api/search.py` that Vercel
runs. If Python cannot verify HTTPS (secure HTTP) certificates on your machine, set `SSL_CERT_FILE` to the path
printed by `uv run python -c "import certifi; print(certifi.where())"`.

| Variable | Meaning | Default |
|---|---|---|
| `PLACES_DB_URL` | pooled connection URL for `places_app` | required |
| `PLACES_HASH_SALT` | secret salt for visitor hashes | required |
| `PLACES_ALLOWED_ORIGINS` | comma-separated page origins allowed to call the function | none (the dev server adds the Vite origins) |
| `PLACES_USER_AGENT` | User-Agent sent to the services | `place-finder-demo/1.0 (portfolio project)` |
| `PLACES_VISITOR_DAILY` | live searches per visitor per day | 10 |
| `PLACES_UPSTREAM_DAILY` | upstream calls per day, site-wide | 100 |
| `PLACES_NOMINATIM_URL`, `PLACES_OVERPASS_URL`, `PLACES_OVERPASS_MIRROR_URL` | service endpoints | the public OpenStreetMap services |
| `VITE_TILE_URL` (build time) | map tile URL template | OpenStreetMap standard tiles |

## Tests

```sh
uv run pytest                                  # every upstream service and the database faked
(cd frontend && npm run build && npm run lint) # type check, production build, lint
```

The suite covers the vocabulary, escaping (property-based fuzzing with Hypothesis plus a seeded
random run), both caches and their lifetimes, Nominatim pacing and the pause after a refusal, the
daily limits and failing closed, the mirror fallback, request handling (origin, content type, size,
methods over a real socket, no leaked errors), the response shapes, place normalization, the setup
script's helpers and the dev server.

`tests/test_schema_pg.py` runs `db/schema.sql` and the function's real database code against a
throwaway local Postgres: role privileges, cache lifetimes and bounds, concurrent limit checks,
concurrent pacing reservations and a full search. It is skipped unless you point it at a local
cluster as a superuser:

```sh
PLACES_TEST_ADMIN_DSN=postgresql://postgres:PASSWORD@127.0.0.1:54329/postgres uv run pytest tests/test_schema_pg.py
```

## Deploying

The Vercel project's root directory is `frontend/`: Vite builds the page and `frontend/api/search.py`
becomes the function, with `psycopg` from `frontend/requirements.txt`. `scripts/setup_db.py` creates
the schema and the role on Neon from the owner's connection URL (`PLACES_OWNER_URL`), generates the
role password and the hash salt, writes the pooled `PLACES_DB_URL` and `PLACES_HASH_SALT` to a
private env file outside the repository, and verifies every grant. Those two values, plus
`PLACES_ALLOWED_ORIGINS` and `PLACES_USER_AGENT`, go into the Vercel project's environment.

## Known gaps

- Overpass has no "nearest first" ordering. The function asks for up to 200 matches and keeps the
  60 closest of those, so in very dense areas a closer place can be missed; the page says when there
  were more matches and suggests zooming in.
- Name matching is case-insensitive but not accent-insensitive ("cafe" will not find "Café" by
  name, although "cafe" as a category works).
- Opening hours are shown as OpenStreetMap writes them (for example `Mo-Fr 08:00-18:00`), not parsed.
- Two identical searches that miss the cache at the same moment both call the service.
- The daily counter table gains one small row per visitor per day and has no pruning job yet.
- The page has no social preview image.

## Credits

Place data &copy; [OpenStreetMap contributors](https://www.openstreetmap.org/copyright), available
under the Open Database License (ODbL). Geocoding by [Nominatim](https://nominatim.org). Place
queries by the [Overpass API](https://overpass-api.de), using the main instance run by FOSSGIS and
the [private.coffee](https://overpass.private.coffee) instance as the mirror. Map display by
[Leaflet](https://leafletjs.com) with OpenStreetMap's standard tiles.

Built by Rakim Francis.
