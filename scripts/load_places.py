#!/usr/bin/env python3
"""The monthly data pipeline: Overture Maps places for New York City's five boroughs, into Postgres.

    uv run --group pipeline python scripts/load_places.py extract
    uv run --group pipeline python scripts/load_places.py plan
    uv run --group pipeline python scripts/load_places.py load
    uv run --group pipeline python scripts/load_places.py check-vocabulary

extract   Reads the Overture places release from its public bucket (no key needed) with DuckDB,
          only for row groups inside NYC's bounding box, then keeps the points inside the five
          boroughs. The borough outlines come from NYC Open Data. Writes a local Parquet file in
          data/ (so the other steps never download again) and the simplified city outline the
          page uses. Prints how many megabytes it read.
plan      Prints the confidence distribution and, for a confidence floor, the row count and the
          estimated size in Postgres including indexes. Writes nothing.
load      Loads that same selection into Postgres: a new staging table, its indexes, checks,
          then one transaction that swaps it in. A failure at any point leaves the live table
          as it was. The previous table is kept as places.places_previous for rollback, and the
          load refuses to start while one exists, so removing it is a deliberate step.
check-vocabulary
          Counts the loaded rows behind every Overture category the search vocabulary uses, and
          fails if any of them is missing.

Sources (read 2026-11-09):
  Overture release catalog  https://stac.overturemaps.org/catalog.json (latest: 2026-10-21.0,
                            schema v2.0.0: taxonomy and basic_category replace categories)
  Overture attribution      https://docs.overturemaps.org/attribution/
  Borough boundaries        NYC Open Data, "Borough Boundaries (water areas included)", NYC
                            Department of City Planning, https://data.cityofnewyork.us/d/wh2p-dxnf
                            Water areas are included so places on piers stay inside the city.
  Neighborhood names        NYC Open Data, "2020 Neighborhood Tabulation Areas (NTAs)", NYC
                            Department of City Planning, https://data.cityofnewyork.us/d/9nt8-h7nd
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"
OUTLINE_FILE = REPO_ROOT / "frontend" / "src" / "nyc-outline.json"
TABLE_SQL = REPO_ROOT / "db" / "places_table.sql"
DEFAULT_RELEASE = "2026-10-21.0"
OVERTURE_PLACES = "s3://overturemaps-us-west-2/release/{release}/theme=places/type=place/*.parquet"
BOROUGHS_URL = "https://data.cityofnewyork.us/resource/wh2p-dxnf.geojson"
BOROUGHS_PAGE = "https://data.cityofnewyork.us/d/wh2p-dxnf"
NTAS_URL = "https://data.cityofnewyork.us/resource/9nt8-h7nd.geojson?$limit=1000"
BOROUGH_CODES = {"Manhattan": 1, "Bronx": 2, "Brooklyn": 3, "Queens": 4, "Staten Island": 5}
USER_AGENT = "place-finder-demo/1.0 (portfolio project; data pipeline)"
# Chosen from the measured 2026-10-21.0 distribution (see README, "The data"): 72 to 74 percent
# of places scored 0.5 to 0.7 are backed only by a business registry, with no consumer source.
DEFAULT_MIN_CONFIDENCE = 0.7
DEFAULT_MAX_MB = 200
# Kept places must not be known to be closed.
OPEN_STATUSES = ("open",)


def fail(message: str, code: int = 2):
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(code)


def load_search_module():
    """The function's own cleaning helpers, so the pipeline and the API agree on what is valid."""
    spec = importlib.util.spec_from_file_location("place_search", REPO_ROOT / "frontend" / "api" / "search.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def duck():
    import duckdb

    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; INSTALL spatial; LOAD spatial;")
    con.execute("SET s3_region = 'us-west-2';")
    return con


def raw_path(release: str) -> Path:
    return DATA_DIR / f"overture_places_nyc_bbox_{release}.parquet"


def nyc_path(release: str) -> Path:
    return DATA_DIR / f"overture_places_nyc_{release}.parquet"


def boroughs_path() -> Path:
    return DATA_DIR / "nyc_borough_boundaries_wi.geojson"


def ntas_path() -> Path:
    return DATA_DIR / "nyc_nta_2020.geojson"


# ------------------------------------------------------------------ extract


def tls_context():
    """Certificate verification that also works on Python builds without a system store."""
    import ssl

    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def fetch_boroughs() -> Path:
    path = boroughs_path()
    if path.exists():
        return path
    request = urllib.request.Request(BOROUGHS_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60, context=tls_context()) as response:
        body = response.read()
    data = json.loads(body)
    names = sorted(f["properties"]["boroname"] for f in data.get("features", []))
    if names != ["Bronx", "Brooklyn", "Manhattan", "Queens", "Staten Island"]:
        fail(f"unexpected borough file contents: {names}")
    DATA_DIR.mkdir(exist_ok=True)
    path.write_bytes(body)
    print(f"boroughs: {len(body) / 1e6:.2f} MB from {BOROUGHS_URL}")
    return path


def fetch_ntas() -> Path:
    path = ntas_path()
    if path.exists():
        return path
    request = urllib.request.Request(NTAS_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60, context=tls_context()) as response:
        body = response.read()
    features = json.loads(body).get("features", [])
    if len(features) < 200 or any("ntaname" not in f.get("properties", {}) for f in features):
        fail(f"unexpected neighborhood file contents ({len(features)} features)")
    DATA_DIR.mkdir(exist_ok=True)
    path.write_bytes(body)
    print(f"neighborhoods: {len(body) / 1e6:.2f} MB from {NTAS_URL}")
    return path


def search_fold(text: str) -> str:
    return load_search_module().fold(text)


def nta_aliases(name: str) -> dict[str, str]:
    """Folded alias -> display label for one NTA name.

    "SoHo-Little Italy-Hudson Square" gives the whole name and each part; a qualifier such as
    "(West)" is dropped, so the two Bedford-Stuyvesant areas share one name.
    """
    search = load_search_module()
    base = re.sub(r"\s*\([^)]*\)", "", name).strip()
    labels = {base} | {part.strip() for part in re.split(r"-|&", base)}
    out = {}
    for label in labels:
        alias = search.fold(label)
        if len(alias) >= 3:
            out.setdefault(alias, label)
    return out


def neighborhood_rows(con) -> list[tuple]:
    """(alias, borocode, boroname, label, lat, lng): one point inside each alias's areas."""
    con.execute("CREATE OR REPLACE TABLE ntas AS SELECT ntaname, boroname, ntatype, geom FROM ST_Read(?)",
                [str(fetch_ntas())])
    pairs = []
    for ntaname, boroname, ntatype in con.execute("SELECT ntaname, boroname, ntatype FROM ntas").fetchall():
        if str(ntatype) == "5":  # Rikers Island: not a place anyone searches for
            continue
        for alias, label in nta_aliases(ntaname).items():
            pairs.append((alias, label, ntaname, boroname))
    # Each borough's own name, pointing inside its residential neighborhoods (not into a bay).
    for boroname in BOROUGH_CODES:
        for ntaname, ntatype in con.execute("SELECT ntaname, ntatype FROM ntas WHERE boroname = ?", [boroname]).fetchall():
            if str(ntatype) == "0":
                for alias in {search_fold(boroname), search_fold("the " + boroname) if boroname == "Bronx" else None} - {None}:
                    pairs.append((alias, "The Bronx" if boroname == "Bronx" else boroname, ntaname, boroname))
    con.execute("CREATE OR REPLACE TABLE alias_map (alias VARCHAR, label VARCHAR, ntaname VARCHAR, boroname VARCHAR)")
    con.executemany("INSERT INTO alias_map VALUES (?, ?, ?, ?)", pairs)
    # The center of the areas when it lies inside them, otherwise a point that surely does.
    rows = con.execute("""
        WITH shapes AS (
            SELECT a.alias, a.boroname, min(a.label) AS label, ST_Union_Agg(n.geom) AS g
            FROM alias_map a JOIN ntas n ON n.ntaname = a.ntaname AND n.boroname = a.boroname
            GROUP BY a.alias, a.boroname
        )
        SELECT alias, boroname, label,
               CASE WHEN ST_Contains(g, ST_Centroid(g)) THEN ST_Centroid(g) ELSE ST_PointOnSurface(g) END AS pt
        FROM shapes
    """).fetchall()
    return [(alias, BOROUGH_CODES[boro], boro, label, pt_y, pt_x) for alias, boro, label, pt_x, pt_y in
            ((r[0], r[1], r[2], *con.execute("SELECT ST_X(?::GEOMETRY), ST_Y(?::GEOMETRY)", [r[3], r[3]]).fetchone())
             for r in rows)]


def http_mib(profile_text: str) -> float:
    """The 'in:' figure from DuckDB's HTTPFS stats in an EXPLAIN ANALYZE profile, in MiB."""
    match = re.search(r"in:\s*([\d.]+)\s*(B|KiB|MiB|GiB)", profile_text)
    if not match:
        return 0.0
    value, unit = float(match.group(1)), match.group(2)
    return value * {"B": 1 / 2**20, "KiB": 1 / 1024, "MiB": 1, "GiB": 1024}[unit]


def extract(args) -> None:
    con = duck()
    path = fetch_boroughs()
    fetch_ntas()
    con.execute("CREATE TABLE boroughs AS SELECT boroname, borocode::INT AS borocode, geom FROM ST_Read(?)", [str(path)])
    xmin, xmax, ymin, ymax = con.execute(
        "SELECT min(ST_XMin(geom)), max(ST_XMax(geom)), min(ST_YMin(geom)), max(ST_YMax(geom)) FROM boroughs"
    ).fetchone()
    print(f"bounding box: {xmin:.4f}, {ymin:.4f} to {xmax:.4f}, {ymax:.4f}")

    raw = raw_path(args.release)
    if raw.exists():
        print(f"using the cached bounding-box extract {raw.name}; nothing downloaded")
    else:
        source = OVERTURE_PLACES.format(release=args.release)
        started = time.time()
        # Row groups carry bbox statistics, so this filter skips everything outside the box.
        profile = con.execute(f"""
            EXPLAIN ANALYZE CREATE TABLE bbox_places AS
            SELECT id,
                   names.primary AS name,
                   taxonomy.primary AS category,
                   basic_category,
                   confidence,
                   operating_status,
                   addresses[1].freeform AS street,
                   addresses[1].locality AS locality,
                   addresses[1].postcode AS postcode,
                   phones,
                   websites,
                   list_distinct(list_transform(sources, lambda s: s.dataset)) AS datasets,
                   list_distinct(list_transform(sources, lambda s: s.license)) AS licenses,
                   ST_X(geometry) AS lng,
                   ST_Y(geometry) AS lat
            FROM read_parquet('{source}')
            WHERE bbox.xmin <= {xmax} AND bbox.xmax >= {xmin}
              AND bbox.ymin <= {ymax} AND bbox.ymax >= {ymin}
        """).fetchall()[0][1]
        mib = http_mib(profile)
        rows = con.execute("SELECT count(*) FROM bbox_places").fetchone()[0]
        DATA_DIR.mkdir(exist_ok=True)
        con.execute(f"COPY bbox_places TO '{raw}' (FORMAT parquet, COMPRESSION zstd)")
        print(f"overture: read {mib:.1f} MiB ({mib * 1.048576:.1f} MB) in {time.time() - started:.0f} s; "
              f"{rows:,} places in the bounding box")
        (DATA_DIR / f"extract_{args.release}.json").write_text(json.dumps(
            {"release": args.release, "http_mib": round(mib, 2), "bbox_rows": rows,
             "bbox": [xmin, ymin, xmax, ymax], "seconds": round(time.time() - started)}, indent=2))

    # Points inside the five boroughs. A point exactly on a shared boundary keeps one borough.
    con.execute(f"""
        CREATE TABLE nyc AS
        SELECT p.*, b.boroname AS borough
        FROM read_parquet('{raw}') p
        JOIN boroughs b ON ST_Intersects(b.geom, ST_Point(p.lng, p.lat))
        QUALIFY row_number() OVER (PARTITION BY p.id ORDER BY b.borocode) = 1
    """)
    out = nyc_path(args.release)
    con.execute(f"COPY nyc TO '{out}' (FORMAT parquet, COMPRESSION zstd)")
    total, inside = con.execute(f"SELECT (SELECT count(*) FROM read_parquet('{raw}')), count(*) FROM nyc").fetchone()
    print(f"inside the five boroughs: {inside:,} of {total:,} places in the box")
    print(con.execute("SELECT borough, count(*) FROM nyc GROUP BY 1 ORDER BY 2 DESC").fetchall())
    write_outline(con)


def write_outline(con) -> None:
    """A simplified city outline for the page: enough to tell whether the map is over NYC."""
    geojson = con.execute(
        "SELECT ST_AsGeoJSON(ST_SimplifyPreserveTopology(ST_Union_Agg(geom), 0.002)) FROM boroughs"
    ).fetchone()[0]
    shape = json.loads(geojson)

    def rounded(value):
        if isinstance(value, list):
            return [rounded(v) for v in value]
        return round(value, 4)

    outline = {
        "source": f"NYC Department of City Planning, Borough Boundaries (water areas included), {BOROUGHS_PAGE}",
        "note": "Simplified to about 200 meters; the server checks the full-detail boundaries.",
        "type": shape["type"],
        "coordinates": rounded(shape["coordinates"]),
    }
    OUTLINE_FILE.write_text(json.dumps(outline, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"outline: {OUTLINE_FILE.relative_to(REPO_ROOT)} ({OUTLINE_FILE.stat().st_size / 1024:.1f} KiB)")


# ------------------------------------------------------------------ plan


def selection_sql(release: str, min_confidence: float) -> str:
    statuses = ", ".join(f"'{s}'" for s in OPEN_STATUSES)
    return f"""
        SELECT * FROM read_parquet('{nyc_path(release)}')
        WHERE confidence >= {float(min_confidence)}
          AND (operating_status IS NULL OR operating_status IN ({statuses}))
          AND name IS NOT NULL AND length(trim(name)) > 0
          AND coalesce(category, basic_category) IS NOT NULL
    """


def require_extract(release: str) -> None:
    if not nyc_path(release).exists():
        fail(f"{nyc_path(release).name} not found; run the extract step first")


def estimate_bytes(con, release: str, min_confidence: float) -> dict:
    """Postgres size estimate for the selection: heap plus the four indexes in db/places_table.sql.

    Heap rows follow Postgres's layout (24-byte header, 4-byte line pointer, 1-byte varlena
    headers for short text, 8-byte alignment, 8 KB pages). Index figures are per-row allowances
    measured from the 2026-10-21.0 load (296,035 rows: primary key 11.9 MB, GiST 22.2 MB,
    trigram 20.8 MB, category 2.2 MB); the load prints the real sizes afterwards.
    """
    n, text_bytes, name_bytes = con.execute(f"""
        SELECT count(*),
               sum(1 + strlen(coalesce(name, '')) + 1 + strlen(coalesce(category, basic_category))
                   + 1 + strlen(coalesce(street, '')) + 1 + strlen(coalesce(locality, ''))
                   + 1 + strlen(coalesce(postcode, '')) + 1 + 16 + 1 + 40
                   + 1 + strlen(borough)
                   + 24 + 8 * len(coalesce(datasets, []))),
               sum(strlen(coalesce(name, '')))
        FROM ({selection_sql(release, min_confidence)})
    """).fetchone()
    if not n:
        return {"rows": 0, "heap": 0, "indexes": 0, "total": 0}
    # id (uuid 16) + confidence (float4 4) + geography point (32 + 1) + header, pointer, alignment.
    per_row = 24 + 4 + 16 + 4 + 33 + text_bytes / n
    per_row = (per_row + 7) // 8 * 8
    heap = n * per_row / 0.97
    indexes = {
        "primary key (uuid)": n * 40,
        "gist (geography)": n * 75,
        "gin trigram (name)": name_bytes * 3.4,
        "btree (category)": n * 8,
    }
    return {"rows": n, "heap": heap, "indexes": sum(indexes.values()), "index_detail": indexes,
            "total": heap + sum(indexes.values())}


def plan(args) -> None:
    require_extract(args.release)
    con = duck()
    path = nyc_path(args.release)
    total = con.execute(f"SELECT count(*) FROM read_parquet('{path}')").fetchone()[0]
    print(f"{total:,} places inside the five boroughs (release {args.release})")
    print("\nconfidence      places   cumulative at or above")
    rows = con.execute(f"""
        SELECT floor(confidence * 10) / 10 AS bucket, count(*)
        FROM read_parquet('{path}') GROUP BY 1 ORDER BY 1 DESC
    """).fetchall()
    running = 0
    for bucket, count in rows:
        running += count
        label = "missing" if bucket is None else f"{bucket:.1f} to {min(bucket + 0.1, 1.0):.1f}"
        print(f"  {label:>12}  {count:>9,}  {running:>9,}")
    status = con.execute(f"SELECT operating_status, count(*) FROM read_parquet('{path}') GROUP BY 1 ORDER BY 2 DESC").fetchall()
    print(f"\noperating status: {status}")
    for floor_value in sorted({0.4, 0.5, 0.6, 0.7, 0.8, args.min_confidence}):
        est = estimate_bytes(con, args.release, floor_value)
        marker = "  <- chosen" if floor_value == args.min_confidence else ""
        print(f"floor {floor_value:.2f}: {est['rows']:>9,} rows, estimated {est['total'] / 1e6:6.1f} MB "
              f"(heap {est['heap'] / 1e6:.1f} MB, indexes {est['indexes'] / 1e6:.1f} MB){marker}")
    est = estimate_bytes(con, args.release, args.min_confidence)
    for name, size in est["index_detail"].items():
        print(f"    {name}: {size / 1e6:.1f} MB")
    datasets = con.execute(f"""
        SELECT d, count(*) FROM ({selection_sql(args.release, args.min_confidence)}), unnest(datasets) AS t(d)
        GROUP BY 1 ORDER BY 2 DESC
    """).fetchall()
    print(f"\nsource datasets at the chosen floor: {datasets}")
    licenses = con.execute(f"""
        SELECT l, count(*) FROM ({selection_sql(args.release, args.min_confidence)}), unnest(licenses) AS t(l)
        GROUP BY 1 ORDER BY 2 DESC
    """).fetchall()
    print(f"source licenses at the chosen floor: {licenses}")
    if est["total"] / 1e6 > args.max_mb:
        print(f"\nover the {args.max_mb} MB target: raise --min-confidence or trim columns")
        raise SystemExit(1)


# ------------------------------------------------------------------ load


def output_rows(con, release: str, min_confidence: float, search):
    """The cleaned rows for COPY, in db/places_table.sql column order."""
    cursor = con.execute(f"""
        SELECT id, name, coalesce(category, basic_category), street, locality, postcode, phones, websites,
               confidence, datasets, borough, lng, lat
        FROM ({selection_sql(release, min_confidence)})
    """)
    while batch := cursor.fetchmany(10_000):
        for gers, name, category, street, locality, postcode, phones, websites, conf, datasets, borough, lng, lat in batch:
            name = search.clean_text(name)[:120]
            if not name:
                continue
            phone = next((p for p in (search.clean_text(x)[:32] for x in phones or []) if search.PHONE_SHAPE.fullmatch(p)), None)
            website = next((u for u in (search.https_url(w) for w in websites or []) if u), None)
            yield (
                gers, name, category[:80], search.clean_text(street or "")[:120] or None,
                search.clean_text(locality or "")[:60] or None, search.clean_text(postcode or "")[:12] or None,
                phone, website, round(float(conf), 4), sorted(set(datasets or [])), borough,
                f"SRID=4326;POINT({lng:.7f} {lat:.7f})",
            )


def owner_dsn(args) -> str:
    if args.env_file:
        for raw in Path(args.env_file).read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip().removeprefix("export ").strip(), value.strip().strip("'\""))
    dsn = os.environ.get(args.dsn_env, "")
    if not dsn:
        fail(f"{args.dsn_env} is not set")
    return dsn


def load(args) -> None:
    import psycopg

    require_extract(args.release)
    con = duck()
    est = estimate_bytes(con, args.release, args.min_confidence)
    print(f"plan: {est['rows']:,} rows at confidence >= {args.min_confidence}, "
          f"estimated {est['total'] / 1e6:.1f} MB with indexes")
    if est["total"] / 1e6 > args.max_mb:
        fail(f"estimated size is over the {args.max_mb} MB target; raise --min-confidence")
    search = load_search_module()
    meta = json.loads((DATA_DIR / f"extract_{args.release}.json").read_text()) if (DATA_DIR / f"extract_{args.release}.json").exists() else {}
    boroughs = json.loads(boroughs_path().read_text())

    with psycopg.connect(owner_dsn(args), autocommit=True, prepare_threshold=None, connect_timeout=15) as conn:
        exists = dict(conn.execute(
            "SELECT c.relname, true FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'places' AND c.relname IN ('places', 'places_staging', 'places_previous')"
        ).fetchall())
        if "places_previous" in exists:
            fail("places.places_previous exists from the last refresh. Check the live table, then remove the "
                 "previous one deliberately (see 'Refreshing the data' in the README) and run the load again.")
        if "places_staging" in exists:
            fail("places.places_staging exists, which no completed load leaves behind; inspect it first")

        started = time.time()
        with conn.transaction():
            conn.execute("SET LOCAL statement_timeout = 0")
            conn.execute(TABLE_SQL.read_text(encoding="utf-8"))
            with conn.cursor().copy(
                "COPY places.places_staging (id, name, category, street, locality, postcode, phone, website, "
                "confidence, datasets, borough, geog) FROM STDIN"
            ) as copy:
                loaded = 0
                for row in output_rows(con, args.release, args.min_confidence, search):
                    copy.write_row(row)
                    loaded += 1
            print(f"copied {loaded:,} rows in {time.time() - started:.0f} s")
            conn.execute(open(REPO_ROOT / "db" / "places_indexes.sql", encoding="utf-8").read())
            conn.execute("ANALYZE places.places_staging")
            checks(conn, loaded)

            for feature in boroughs["features"]:
                conn.execute(
                    "INSERT INTO places.boroughs (borocode, boroname, geom) "
                    "VALUES (%s, %s, ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326))) "
                    "ON CONFLICT (borocode) DO UPDATE SET boroname = excluded.boroname, geom = excluded.geom",
                    (int(feature["properties"]["borocode"]), feature["properties"]["boroname"],
                     json.dumps(feature["geometry"])),
                )
            # Neighborhood names change about once a decade, so they are upserted, not swapped.
            names = neighborhood_rows(con)
            for alias, borocode, boroname, label, lat, lng in names:
                conn.execute(
                    "INSERT INTO places.neighborhoods (alias, borocode, boroname, label, lat, lng) "
                    "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (alias, borocode) DO UPDATE SET "
                    "boroname = excluded.boroname, label = excluded.label, lat = excluded.lat, lng = excluded.lng",
                    (alias, borocode, boroname, label, round(lat, 6), round(lng, 6)),
                )
            print(f"neighborhood names: {len(names)}")
            # The swap. Readers keep seeing the old table until this transaction commits.
            if "places" in exists:
                conn.execute("ALTER TABLE places.places RENAME TO places_previous")
                for suffix in ("pkey", "geog_idx", "name_trgm_idx", "category_idx"):
                    conn.execute(f"ALTER INDEX IF EXISTS places.places_{suffix} RENAME TO places_previous_{suffix}")
                conn.execute("REVOKE ALL ON places.places_previous FROM places_app")
            conn.execute("ALTER TABLE places.places_staging RENAME TO places")
            for suffix in ("pkey", "geog_idx", "name_trgm_idx", "category_idx"):
                conn.execute(f"ALTER INDEX places.places_staging_{suffix} RENAME TO places_{suffix}")
            conn.execute("GRANT SELECT ON places.places TO places_app")
            for key, value in {
                "release": args.release, "min_confidence": str(args.min_confidence), "rows": str(loaded),
                "loaded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "overture_mib_read": str(meta.get("http_mib", "")),
            }.items():
                conn.execute("INSERT INTO places.meta (key, value) VALUES (%s, %s) "
                             "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value))
        sizes = conn.execute(
            "SELECT pg_relation_size('places.places'), pg_indexes_size('places.places'), "
            "pg_total_relation_size('places.places')"
        ).fetchone()
        detail = conn.execute(
            "SELECT indexrelid::regclass::text, pg_relation_size(indexrelid) FROM pg_index "
            "WHERE indrelid = 'places.places'::regclass ORDER BY 1"
        ).fetchall()
    print(f"swapped in {loaded:,} rows in {time.time() - started:.0f} s. Measured: table {sizes[0] / 1e6:.1f} MB, "
          f"indexes {sizes[1] / 1e6:.1f} MB, total {sizes[2] / 1e6:.1f} MB "
          f"(estimate was {est['total'] / 1e6:.1f} MB)")
    for name, size in detail:
        print(f"    {name}: {size / 1e6:.1f} MB")
    if "places" in exists:
        print("the previous table is kept as places.places_previous for rollback")


def checks(conn, loaded: int) -> None:
    """Sanity checks before the swap. Any failure rolls the whole load back."""
    count, distinct_ids, outside = conn.execute(
        "SELECT count(*), count(DISTINCT id), count(*) FILTER (WHERE NOT ST_Intersects(geog, "
        "ST_MakeEnvelope(-74.30, 40.45, -73.65, 40.95, 4326)::geography)) FROM places.places_staging"
    ).fetchone()
    if count != loaded or count != distinct_ids:
        raise RuntimeError(f"row check failed: {count} rows, {distinct_ids} distinct ids, {loaded} copied")
    if outside:
        raise RuntimeError(f"{outside} rows fall outside the New York City area")
    if count < 1000:
        raise RuntimeError(f"only {count} rows; refusing to replace the live table with so little data")


# ------------------------------------------------------------------ vocabulary


def check_vocabulary(args) -> None:
    import psycopg

    search = load_search_module()
    with psycopg.connect(owner_dsn(args), autocommit=True, prepare_threshold=None) as conn:
        counts = dict(conn.execute("SELECT category, count(*) FROM places.places GROUP BY 1").fetchall())
    missing = []
    for category in search.VOCABULARY:
        found = {key: counts.get(key, 0) for key in category.keys}
        print(f"{category.key:>18}: {sum(found.values()):>7,}  {found}")
        missing += [f"{category.key}:{key}" for key, n in found.items() if n == 0]
    if missing:
        print(f"\nkeys with no loaded rows: {missing}")
        raise SystemExit(1)
    print(f"\nall {sum(len(c.keys) for c in search.VOCABULARY)} category keys have rows")


def main() -> None:
    parser = argparse.ArgumentParser(description="Overture places for NYC into Postgres")
    parser.add_argument("step", choices=["extract", "plan", "load", "check-vocabulary"])
    parser.add_argument("--release", default=DEFAULT_RELEASE)
    parser.add_argument("--min-confidence", type=float, default=DEFAULT_MIN_CONFIDENCE)
    parser.add_argument("--max-mb", type=float, default=DEFAULT_MAX_MB)
    parser.add_argument("--dsn-env", default="PLACES_OWNER_URL", help="environment variable holding the owner URL")
    parser.add_argument("--env-file", help="KEY=VALUE file to read the owner URL from")
    args = parser.parse_args()
    {"extract": extract, "plan": plan, "load": load, "check-vocabulary": check_vocabulary}[args.step](args)


if __name__ == "__main__":
    main()
