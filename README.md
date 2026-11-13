# Place Finder

Search places across New York City's five boroughs (Manhattan, Brooklyn, Queens, the Bronx and
Staten Island): coffee, pharmacies, parks and about forty other everyday categories, or any place
by name, on a map. The places come from the site's own copy of [Overture Maps](https://overturemaps.org)
open data, rebuilt monthly by a small pipeline into Postgres with PostGIS. It is a 2026 rebuild of a
2019 bootcamp project, and it covers New York City (NYC) only, on purpose: one city done properly,
from data the site controls.

## What it does

- Type **what** (a category such as "pharmacy", or a name such as "Joe's") and **where** (a
  neighborhood, a park, a ZIP code or a street address in NYC), or tap a quick pick: Coffee,
  Pizza, Pharmacy, Clinic, Grocery, Park.
- Move the map and choose **Search this area** to search around the map center. The map stays
  over the city, and the button turns itself off when the center leaves the five boroughs.
- Up to 60 places come back, nearest first, with category, address, borough, phone and website.
  Hovering or selecting a place in the list highlights it on the map, and clicking a marker
  selects it in the list.
- Loading, empty, outside-NYC, not-found, rate-limited and service-trouble states each have their
  own message.
- It works on a phone, with a keyboard, and with a screen reader (labeled form, live status
  messages, a labeled map region, real buttons and links).

## 2019 and 2026

| | 2019 bootcamp version | 2026 rebuild |
|---|---|---|
| Place data | Yelp, called from the browser through a public CORS (Cross-Origin Resource Sharing) proxy, with the Application Programming Interface (API) key in client-side code | Its own monthly data pipeline: Overture Maps places for the five boroughs, loaded into Postgres with PostGIS |
| Search | Whatever the Yelp API returned | One parameterized Structured Query Language (SQL) query: category or name match, within a radius, nearest first |
| Frontend | Create React App (CRA) | Vite, React 19, TypeScript |
| Map | Google Maps through `react-google-maps` | Leaflet with OpenStreetMap tiles, no key |
| Secrets | A third-party key in the browser | No third-party keys at all; the only secrets are a database address and a hash salt, both server-side |
| When something fails | The page stopped working | Stable error codes, each with its own message on the page |

The original is at [rfranci52/react-google-maps](https://github.com/rfranci52/react-google-maps).

## Architecture

```
Monthly, on a laptop                          Every search
--------------------                          ------------
scripts/load_places.py                        Browser: React + Leaflet (frontend/src)
  DuckDB reads Overture's public bucket         | map tiles straight from tile.openstreetmap.org
  (NYC bounding box only)                       |
  + NYC Open Data borough and                   | POST /api/search {"what": "coffee", "where": "Astoria"}
    neighborhood boundaries                     v
  -> staging table, indexes, checks       Vercel Python function: frontend/api/search.py
  -> one-transaction swap                   |-- Postgres + PostGIS (Neon), schema "places":
          |                                 |     places, boroughs, neighborhoods, geocode cache,
          '------------------------------>  |     daily counters
                                            '-- NYC GeoSearch, only for street addresses
```

One search, step by step:

1. **Check the request.** Only the site's own origin, JavaScript Object Notation (JSON) only, at
   most 2 kilobytes, POST only. `what` is 2 to 40 characters; then either `where` (2 to 80
   characters) or a `lat`/`lng` pair, plus an optional `radius` of 500, 1000, 1500 or 2500 meters.
2. **Count it** against the visitor's and the site's daily search allowance.
3. **Find the point.** A location that is only a ZIP code ("10456", "Bronx, NY 10456") comes from
   a table of ZIP code centers built from the loaded places (see "Refreshing the data"), because
   GeoSearch has no ZIP code index and matched bare ZIP codes to house numbers elsewhere in the
   city. Any other typed location is first looked up in NYC's own neighborhood and park
   names (from the 2020 Neighborhood Tabulation Areas, or NTAs), with an optional borough
   ("Chelsea, Staten Island"). Anything else, typically a street address, goes to
   [NYC GeoSearch](https://geosearch.planninglabs.nyc), the City's free geocoder, and its answer
   is cached for 30 days.
4. **Check the area.** A point outside the five boroughs is refused with `out_of_area`, using the
   official borough boundaries.
5. **Search.** About 40 curated categories with synonyms and plurals map to Overture category
   keys ("cafe" and "coffee shop" both mean `coffee_shop`, `cafe` or `coffee_roastery`). Anything
   else is a name match: a case-insensitive substring through a trigram index, a close spelling
   for longer terms ("starbuks" finds Starbucks), or a category key containing the word ("thai"
   finds `thai_restaurant`). `ST_DWithin` keeps it inside the radius, the GiST (Generalized Search
   Tree) index serves it, and the 60 nearest come back.

The response is `{"ok": true, "query", "center", "location", "borough", "places", "truncated",
"data_release"}`, and every failure is `{"ok": false, "error": {"code", "message",
"retry_after"?}}` with a matching HTTP (Hypertext Transfer Protocol) status:

| code | HTTP status | when |
|---|---|---|
| `invalid` | 400, 413, 415 | the request is malformed, too large or not JSON |
| `forbidden` | 403 | the Origin header is not the site |
| `not_found` | 404 | the location is not a known NYC neighborhood and GeoSearch has no confident match |
| `method_not_allowed` | 405 | anything but POST (with an `Allow: POST` header) |
| `out_of_area` | 422 | the point is outside the five boroughs |
| `rate_limited` | 429 | a daily allowance is used up (with `Retry-After`) |
| `upstream_busy` | 503 | GeoSearch did not answer usefully |
| `unavailable` | 503 | the database cannot be reached, so the search is refused |

No response ever contains a stack trace.

## The data

Everything below was measured on 2026-11-09 against Overture release `2026-10-21.0` (schema
v2.0.0, where `taxonomy` and `basic_category` replaced the old `categories` field).

- **Read.** DuckDB, with its `httpfs` and `spatial` extensions, reads the release straight from
  Overture's public bucket without a key. Parquet row groups carry bounding-box statistics, so
  filtering on the NYC box first skipped everything else: the extract read **138.7 MiB (145.4 MB)**
  and found 627,224 places in the box.
- **Clip.** Points are kept only inside the five boroughs, using NYC Open Data's "Borough
  Boundaries (water areas included)" from the Department of City Planning. Water areas are
  included so places on piers stay in. That leaves **494,235** places.
- **Floor.** Overture's `confidence` measures whether a place exists. In NYC it is bimodal:

  | confidence | places | what is there |
  |---|---|---|
  | 0.9 to 1.0 | 250,279 | most places, from many sources |
  | 0.8 to 0.9 | 25,120 | mixed sources |
  | 0.7 to 0.8 | 41,807 | 52% backed only by a business registry, 27% by Foursquare |
  | 0.6 to 0.7 | 41,233 | 74% backed only by a business registry, none by Foursquare |
  | 0.5 to 0.6 | 33,535 | 72% backed only by a business registry, none by Foursquare |
  | 0.4 to 0.5 | 9,864 | the trough between the two groups |
  | under 0.4 | 92,397 | mostly single-source records |

  The floor is **0.7**. The 0.5 to 0.7 band is its own population: almost three quarters of it is
  backed only by business-registry filings, with no consumer source, and sampling it turned up
  misfiled entries (an auto body shop listed as a restaurant, a person's name as a pharmacy). At
  0.7, consumer sources such as Foursquare start to corroborate. Places marked permanently or
  temporarily closed are left out too.
- **Size.** At that floor the table holds **296,035 places**. Measured in Postgres: table 77.2 MB,
  indexes 57.2 MB (GiST 22.2, trigram 20.8, primary key 11.9, category 2.2), **134.4 MB in all**,
  under the 200 MB target. The plan step estimated 121.7 MB before the first load; its per-row
  allowances have since been calibrated to these measurements, and it now estimates 140.0 MB.
- **Columns.** Overture's id (a UUID), name, primary category, street, locality, postcode, one
  phone, one website (kept only when it is `https://`), confidence, the source datasets (for
  attribution), the borough and a geography point.
- **Vocabulary.** The 40 categories use 202 Overture category keys, and every one has rows in the
  loaded release (`load_places.py check-vocabulary`, and a snapshot test). "Restaurant" covers
  `restaurant` and the 80 cuisine keys with at least 20 places.
- **Neighborhoods.** 484 names from the 2020 NTAs: each NTA's name, each part of a hyphenated
  name ("SoHo-Little Italy-Hudson Square" also answers to "SoHo"), and each borough's own name,
  each pointing inside its areas. GeoSearch is an address geocoder and handles names poorly (it
  answered "37 Greenwich Street" for "Greenwich Village"), which is why names are resolved here,
  and why a GeoSearch answer to a query with no house number is accepted only when every word typed
  appears in its label.

## Fair use and safety

**Limits.** A visitor is an HMAC (Hash-based Message Authentication Code) of their Internet
Protocol (IP) address with a secret salt, with each IPv6 /64 counted as one visitor; the address
itself is never stored or logged. Searches are single indexed queries, so the allowance is
generous: 300 per visitor and 20,000 for the site per day. GeoSearch calls get a smaller one, 30
per visitor and 1,000 for the site per day, because they use a free City service. The visitor is
checked first, so a visitor over their own limit never spends the site's allowance, and a request
the site limit refuses is given back to the visitor. If the database cannot be reached the search
is refused: there is no path around the counters.

**GeoSearch.** It needs no key, and its documentation publishes no rate limit; it asks only that
autocomplete be throttled. This site never autocompletes: neighborhood names never reach it, a
street address is sent once per submitted search, and the answer, including "not found", is cached
for 30 days in a table that cannot grow past 16,384 rows (a key hashes to a slot, and a write
replaces its slot).

**Query safety.** The place search is one parameterized SQL query; its two variants differ only
in a constant match clause, so no request text is ever formatted into SQL. What remains is the
name pattern: `like_escape()` makes a typed `%`, `_` or `\` match itself rather than act as a
wildcard. The tests check that property with thousands of generated strings, against a reference
LIKE matcher and against real Postgres.

**Least privilege.** The app logs in as `places_app`. It can read the public data (places, borough
outlines, neighborhood names, release facts), it can run three security definer functions (the
geocode cache and the daily counters), each with a fixed `search_path`, and it can do nothing
else. It also gets short statement, lock and idle timeouts.

**The rest.** The page sends the security headers in `frontend/vercel.json`, including a
Content-Security-Policy (CSP) in report-only mode that allows images only from the site, inline
data images and the tile server.

## Run it locally

You need [uv](https://docs.astral.sh/uv/), Node.js 20.19 or 22.12 or later (Vite 8's
requirement), and Postgres with the PostGIS and pg_trgm extensions.

```sh
uv sync                          # Python environment in .venv, with the test tools and the pipeline
(cd frontend && npm install)

createdb places_dev
psql -d places_dev -f db/schema.sql
psql -d places_dev -c "alter role places_app login password 'local-dev-only'"
uv run python scripts/load_places.py extract     # about 140 MiB from Overture, once
PLACES_OWNER_URL=postgresql://localhost/places_dev uv run python scripts/load_places.py load
```

The extract keeps its downloads in `data/` (ignored by git), so later runs read nothing remote.
Put the app's settings in an env file outside the repository, for example
`../site_private/.env.places.dev`:

```sh
PLACES_DB_URL=postgresql://places_app:local-dev-only@127.0.0.1:5432/places_dev
PLACES_HASH_SALT=any-long-random-string
```

Then start the two halves:

```sh
uv run python scripts/dev_api.py --env-file ../site_private/.env.places.dev   # 127.0.0.1:8790
(cd frontend && npm run dev)                                                  # http://localhost:5174
```

Vite proxies `/api` to the local server, which serves the same `frontend/api/search.py` that
Vercel runs. If Python cannot verify HTTPS (secure HTTP) certificates on your machine, set
`SSL_CERT_FILE` to the path printed by `uv run python -c "import certifi; print(certifi.where())"`.

| Variable | Meaning | Default |
|---|---|---|
| `PLACES_DB_URL` | pooled connection URL for `places_app` | required |
| `PLACES_HASH_SALT` | secret salt for visitor hashes | required |
| `PLACES_ALLOWED_ORIGINS` | comma-separated page origins allowed to call the function | none (the dev server adds the Vite origins) |
| `PLACES_SEARCHES_PER_VISITOR`, `PLACES_SEARCHES_PER_DAY` | daily search allowances | 300, 20,000 |
| `PLACES_GEOCODES_PER_VISITOR`, `PLACES_GEOCODES_PER_DAY` | daily GeoSearch allowances | 30, 1,000 |
| `PLACES_GEOSEARCH_URL` | geocoder endpoint | `https://geosearch.planninglabs.nyc/v2/search` |
| `VITE_TILE_URL` (build time) | map tile URL template | OpenStreetMap standard tiles |

## Refreshing the data

Overture publishes a release every month; check `https://stac.overturemaps.org/catalog.json` for
the latest one. A refresh runs from a laptop with the database owner's connection URL. Use the
direct endpoint rather than the pooled one, since the load is one long transaction.

```sh
export PLACES_OWNER_URL=...        # the owner's direct (not -pooler) URL
R=2026-11-xx.0                     # the new release

uv run python scripts/load_places.py extract --release $R    # the one download, NYC box only
uv run python scripts/load_places.py plan --release $R       # rows, confidence, estimated size
```

`plan` prints the confidence distribution and the estimated size with indexes, and stops if it
is over 200 MB. If the distribution has shifted, choose the floor again with `--min-confidence`.
Then:

```sh
psql "$PLACES_OWNER_URL" -c "drop table if exists places.places_previous"   # last month's backup
uv run python scripts/load_places.py load --release $R
uv run python scripts/load_places.py check-vocabulary
```

`load` builds `places.places_staging` and its indexes, checks it (row count, unique ids, every
point inside the NYC area, at least 1,000 rows), and swaps it in, all in one transaction, so a
failure at any step leaves the live table exactly as it was. The table it replaces is kept as
`places.places_previous`, and the next load refuses to start while that exists: removing the
backup is a deliberate step. To roll back, rename `places` aside, rename `places_previous` to
`places` and grant `select` on it to `places_app`, in one transaction. If `check-vocabulary`
reports a key with no rows, the release renamed a category: update `VOCABULARY` in
`frontend/api/search.py` and the snapshot in `tests/data/`. Update `DEFAULT_RELEASE` in the
script once the new release is live.

Then rebuild the ZIP code table and deploy it with the next push:

```sh
uv run python scripts/build_zip_centers.py --dry-run   # counts per borough, writes nothing
uv run python scripts/build_zip_centers.py             # rewrites the table at the end of search.py
```

Each ZIP code's center is the median position of the loaded places that carry it. A ZIP code is
kept when at least 20 places carry it and its center falls in the borough its first three digits
imply, which drops codes known only from a few misfiled addresses (233 of 715 seen in release
2026-10-21.0).

Space: during a refresh two copies exist (about 270 MB) until the backup is removed.

## Tests

```sh
uv run pytest                                  # the geocoder and the database faked
(cd frontend && npm run build && npm run lint) # type check, production build, lint
```

The fast suite covers the vocabulary and its key snapshot, LIKE escaping (property-based fuzzing
with Hypothesis), neighborhood names and borough hints, GeoSearch handling (request shape, the
confidence floor, the word check, caching, every failure mode), the five-borough check, both kinds
of daily limit and failing closed, request handling (origin, content type, size, methods over a
real socket, no leaked errors), response shapes, place formatting, the setup script and the
pipeline's helpers, and the dev server.

`tests/test_schema_pg.py` runs `db/schema.sql`, the real loader and the function's real database
code against a throwaway Postgres with PostGIS: role privileges, what the loader keeps and drops,
the swap and its refusal, a failed load leaving the live table untouched, the search query (nearest
first, substring, typo and category-word matches, literal wildcards, LIKE escaping fuzzed against
real Postgres), borough and neighborhood lookups, the geocode cache, concurrent limit checks, and a
full request. It is skipped unless you point it at a local cluster as a superuser:

```sh
PLACES_TEST_ADMIN_DSN=postgresql://postgres:PASSWORD@127.0.0.1:54330/postgres uv run pytest tests/test_schema_pg.py
```

## Deploying

The Vercel project's root directory is `frontend/`: Vite builds the page and
`frontend/api/search.py` becomes the function, with `psycopg` from `frontend/requirements.txt`.
`scripts/setup_db.py` creates the extensions, the schema and the role on Neon from the owner's
connection URL (`PLACES_OWNER_URL`), generates the role password and the hash salt, writes the
pooled `PLACES_DB_URL` and `PLACES_HASH_SALT` to a private env file outside the repository, and
verifies every grant. Then the first data load runs as in "Refreshing the data". Those two values,
plus `PLACES_ALLOWED_ORIGINS`, go into the Vercel project's environment.

## Known gaps

- NYC only, by design: a point outside the five boroughs gets `out_of_area`.
- Name matching is case-insensitive but not accent-insensitive ("cafe" will not find "Café" by
  name, although "cafe" as a category works).
- Overture has no opening hours, so none are shown.
- A neighborhood name that is not in the 2020 NTA list (for example a colloquial one such as
  "Bed-Stuy") goes to GeoSearch, which usually cannot place it, so it comes back as not found.
- Some Overture addresses give "New York" as the locality for places in other boroughs; the
  borough shown next to it comes from the boundary check and is correct.
- The daily counter table gains one small row per visitor per day and has no pruning job yet.
- The page has no social preview image.

## Credits and licenses

Place data from the [Overture Maps Foundation](https://overturemaps.org), release 2026-10-21.0.
Overture's places theme combines several sources, each under its own license; the ones present in
this site's NYC selection are Meta, Microsoft, BrightQuery, Uberall, PinMeTo, DAC, Birdeye,
OpenTravel Alliance and RenderSEO under the [Community Data License Agreement (CDLA) Permissive
2.0](https://cdla.dev/permissive-2-0/); Foursquare under the
[Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0) ("Copyright 2024 Foursquare Labs,
Inc. All rights reserved." Foursquare data was transformed to the Overture schema;
[NOTICE](https://opensource.foursquare.com/places-notice-txt/)); and AllThePlaces under
[Creative Commons Zero (CC0) 1.0](https://creativecommons.org/publicdomain/zero/1.0/). See
Overture's [attribution page](https://docs.overturemaps.org/attribution/).

Borough boundaries and the 2020 Neighborhood Tabulation Areas from
[NYC Open Data](https://opendata.cityofnewyork.us/), published by the NYC Department of City
Planning. Street addresses located by [NYC GeoSearch](https://geosearch.planninglabs.nyc), also from
the Department of City Planning. Map tiles &copy;
[OpenStreetMap contributors](https://www.openstreetmap.org/copyright), displayed with
[Leaflet](https://leafletjs.com).

Built by Rakim Francis.
