-- Place Finder database objects. Safe to run repeatedly with the owner connection.
-- scripts/setup_db.py runs this file in one transaction, then gives places_app its password.
-- The place data itself (places.places) is built by scripts/load_places.py; see
-- db/places_table.sql and db/places_indexes.sql.
--
-- The app logs in as places_app. It can read the public place data and borough outlines, and
-- everything else it does goes through the security definer functions below, which hold the
-- rules: how long geocodes are cached, how big that cache can grow, and the daily limits.

create extension if not exists postgis;
create extension if not exists pg_trgm;

create schema if not exists places;
revoke all on schema places from public;

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'places_app') then
    create role places_app nologin;
  end if;
end;
$$;

-- Applied at every login, including through the connection pooler.
alter role places_app set statement_timeout = '5s';
alter role places_app set lock_timeout = '2s';
alter role places_app set idle_in_transaction_session_timeout = '10s';

-- ------------------------------------------------------------------ reference data

-- The five boroughs, from NYC Open Data (Borough Boundaries, water areas included). Filled by
-- scripts/load_places.py. A point inside one of these is inside the area this site covers.
create table if not exists places.boroughs (
  borocode smallint primary key check (borocode between 1 and 5),
  boroname text not null,
  geom     geometry(MultiPolygon, 4326) not null
);
create index if not exists boroughs_geom_idx on places.boroughs using gist (geom);

-- Neighborhood and park names for typed locations, so "Greenwich Village" or "Prospect Park"
-- resolves here instead of at the geocoder. Built from NYC Open Data's 2020 Neighborhood
-- Tabulation Areas by scripts/load_places.py: one row per name (each part of a hyphenated NTA
-- name counts as a name) and borough, at a point inside the matching areas.
create table if not exists places.neighborhoods (
  alias    text not null check (length(alias) between 3 and 80),
  borocode smallint not null check (borocode between 1 and 5),
  boroname text not null,
  label    text not null,
  lat      double precision not null check (lat between 40 and 41.5),
  lng      double precision not null check (lng between -74.5 and -73.5),
  primary key (alias, borocode)
);

-- Facts about the loaded data (release, row count, confidence floor), written by each load.
create table if not exists places.meta (
  key   text primary key,
  value text not null
);

-- ------------------------------------------------------------------ geocode cache
--
-- Bounded by construction: a key hashes to one of 16,384 slots and a write replaces whatever
-- was in its slot, so the table never grows past that and needs no cleanup job. A read checks
-- the full key, so two keys sharing a slot only ever cost a cache miss, never a wrong answer.

create table if not exists places.geocode_cache (
  slot       int primary key check (slot >= 0 and slot < 16384),
  query      text not null check (length(query) between 1 and 80),
  found      boolean not null,
  lat        double precision check (lat between -90 and 90),
  lng        double precision check (lng between -180 and 180),
  label      text check (length(label) <= 200),
  fetched_at timestamptz not null default now(),
  check (found = (lat is not null and lng is not null))
);

-- ------------------------------------------------------------------ daily limits

-- One row per counter per UTC day. Keys are '<kind>:v:<visitor hash>' for one visitor and
-- '<kind>:site' for the whole site, where kind is 'search' or 'geocode'.
create table if not exists places.daily_counts (
  day date not null,
  key text not null check (length(key) <= 64),
  n   int  not null default 0 check (n >= 0),
  primary key (day, key)
);

-- Start every run from nothing so a past grant cannot linger.
revoke all on all tables in schema places from public, places_app;
grant select on places.boroughs, places.neighborhoods, places.meta to places_app;
do $$
begin
  -- The place table exists once the first load has run; keep the app's read access to it.
  if to_regclass('places.places') is not null then
    grant select on places.places to places_app;
  end if;
end;
$$;

-- ------------------------------------------------------------------ functions

-- Which cache slot a key lands in. Internal: only the functions below call it.
create or replace function places.slot_for(key text, buckets int)
returns int
language sql
immutable
strict
set search_path = pg_catalog, pg_temp
as $$
  select ((hashtextextended(slot_for.key, 0) & 9223372036854775807) % slot_for.buckets)::int
$$;

-- A cached geocode, or no row when there is none or it is older than 30 days.
create or replace function places.geocode_get(query text)
returns table (found boolean, lat double precision, lng double precision, label text, fetched_at timestamptz)
language sql
stable
security definer
set search_path = places, pg_temp
as $$
  select c.found, c.lat, c.lng, c.label, c.fetched_at
  from places.geocode_cache c
  where c.slot = places.slot_for(geocode_get.query, 16384)
    and c.query = geocode_get.query
    and c.fetched_at > now() - interval '30 days'
$$;

create or replace function places.geocode_put(
  query text,
  found boolean,
  lat   double precision,
  lng   double precision,
  label text
)
returns void
language sql
security definer
set search_path = places, pg_temp
as $$
  insert into places.geocode_cache as c (slot, query, found, lat, lng, label, fetched_at)
  values (
    places.slot_for(geocode_put.query, 16384),
    geocode_put.query,
    geocode_put.found,
    geocode_put.lat,
    geocode_put.lng,
    left(geocode_put.label, 200),
    now()
  )
  on conflict (slot) do update
    set query = excluded.query, found = excluded.found, lat = excluded.lat,
        lng = excluded.lng, label = excluded.label, fetched_at = excluded.fetched_at
$$;

-- Takes one unit of a daily allowance: first the visitor's, then the site's. A visitor over
-- their own limit never spends the site's allowance, and a request the site limit refuses is
-- given back to the visitor. Returns 'ok', 'visitor_limit' or 'site_limit'. Callers must
-- treat a database error as a refusal.
create or replace function places.take_slot(kind text, visitor text, per_visitor int, site_limit int)
returns text
language plpgsql
security definer
set search_path = places, pg_temp
as $$
declare
  today     date := (now() at time zone 'utc')::date;
  v_key     text;
  s_key     text;
  visitor_n int;
  site_n    int;
begin
  if kind is null or kind not in ('search', 'geocode') then
    raise exception 'unknown counter kind %', kind;
  end if;
  v_key := kind || ':v:' || left(coalesce(visitor, ''), 40);
  s_key := kind || ':site';

  if per_visitor <= 0 then
    return 'visitor_limit';
  end if;
  if site_limit <= 0 then
    return 'site_limit';
  end if;

  insert into places.daily_counts as q (day, key, n)
  values (today, v_key, 1)
  on conflict (day, key) do update set n = q.n + 1
    where q.n < per_visitor
  returning q.n into visitor_n;

  if visitor_n is null then
    return 'visitor_limit';
  end if;

  insert into places.daily_counts as q (day, key, n)
  values (today, s_key, 1)
  on conflict (day, key) do update set n = q.n + 1
    where q.n < site_limit
  returning q.n into site_n;

  if site_n is null then
    update places.daily_counts as q set n = q.n - 1
    where q.day = today and q.key = v_key and q.n > 0;
    return 'site_limit';
  end if;

  return 'ok';
end;
$$;

-- ------------------------------------------------------------------ grants

-- New functions are executable by everyone until revoked, so this runs over all of them on
-- every apply. (A per-schema default privilege cannot remove that grant, and the global one
-- belongs to the shared owner role, so it is left alone.)
revoke all on all functions in schema places from public, places_app;

grant usage on schema places to places_app;
grant execute on function places.geocode_get(text) to places_app;
grant execute on function places.geocode_put(text, boolean, double precision, double precision, text) to places_app;
grant execute on function places.take_slot(text, text, int, int) to places_app;
