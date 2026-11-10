-- Place Finder database objects. Safe to run repeatedly with the owner connection.
-- scripts/setup_db.py runs this file in one transaction, then gives places_app its password.
--
-- The app logs in as places_app, which owns nothing and holds no privilege on any table.
-- Everything it does goes through the security definer functions below, and those hold
-- the rules: how long cache entries live, how big the caches can grow, the daily limits,
-- and how far apart upstream calls must be.

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

-- ------------------------------------------------------------------ caches
--
-- Both caches are bounded by construction: a key hashes to one of a fixed number of slots
-- and a write replaces whatever was in its slot, so the tables can never grow past that
-- many rows and need no cleanup job. A read checks the full key, so two keys sharing a
-- slot only ever cost a cache miss, never a wrong answer.

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

create table if not exists places.results_cache (
  slot       int primary key check (slot >= 0 and slot < 4096),
  term_key   text not null check (length(term_key) between 3 and 64),
  lat_e3     int not null check (lat_e3 between -90000 and 90000),
  lng_e3     int not null check (lng_e3 between -180000 and 180000),
  radius_m   int not null check (radius_m in (500, 1000, 1500, 2500)),
  places     jsonb not null check (
               jsonb_typeof(places) = 'array'
               and jsonb_array_length(places) <= 60
               and octet_length(places::text) <= 200000),
  truncated  boolean not null default false,
  fetched_at timestamptz not null default now()
);

-- ------------------------------------------------------------------ limits and pacing

-- One row per counter per UTC day: 'v:<visitor hash>' for a visitor's searches that
-- needed live data, 'upstream' for the site's calls to the upstream services.
create table if not exists places.daily_counts (
  day date not null,
  key text not null check (length(key) <= 64),
  n   int  not null default 0 check (n >= 0),
  primary key (day, key)
);

-- One row per upstream service. next_at is the earliest moment the next call may start.
create table if not exists places.upstreams (
  name            text primary key,
  min_interval_ms int not null check (min_interval_ms >= 0),
  next_at         timestamptz not null default 'epoch',
  -- Nominatim's usage policy: an absolute maximum of one request per second.
  check (name <> 'nominatim' or min_interval_ms >= 1000)
);

-- 1100 ms leaves room for the time between the reservation and the request leaving.
insert into places.upstreams (name, min_interval_ms) values
  ('nominatim', 1100),
  ('overpass', 1000),
  ('overpass-mirror', 1000)
on conflict (name) do update set min_interval_ms = excluded.min_interval_ms;

-- Start every run from nothing so a past grant cannot linger.
revoke all on all tables in schema places from public, places_app;

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

-- A cached place list, or no row when there is none or it is older than 24 hours.
create or replace function places.results_get(term_key text, lat_e3 int, lng_e3 int, radius_m int)
returns table (places jsonb, truncated boolean, fetched_at timestamptz)
language sql
stable
security definer
set search_path = places, pg_temp
as $$
  select c.places, c.truncated, c.fetched_at
  from places.results_cache c
  where c.slot = places.slot_for(
          concat_ws('|', results_get.term_key, results_get.lat_e3, results_get.lng_e3, results_get.radius_m),
          4096)
    and c.term_key = results_get.term_key
    and c.lat_e3 = results_get.lat_e3
    and c.lng_e3 = results_get.lng_e3
    and c.radius_m = results_get.radius_m
    and c.fetched_at > now() - interval '24 hours'
$$;

create or replace function places.results_put(
  term_key  text,
  lat_e3    int,
  lng_e3    int,
  radius_m  int,
  places    jsonb,
  truncated boolean
)
returns timestamptz
language sql
security definer
set search_path = places, pg_temp
as $$
  insert into places.results_cache as c (slot, term_key, lat_e3, lng_e3, radius_m, places, truncated, fetched_at)
  values (
    places.slot_for(
      concat_ws('|', results_put.term_key, results_put.lat_e3, results_put.lng_e3, results_put.radius_m),
      4096),
    results_put.term_key,
    results_put.lat_e3,
    results_put.lng_e3,
    results_put.radius_m,
    results_put.places,
    results_put.truncated,
    now()
  )
  on conflict (slot) do update
    set term_key = excluded.term_key, lat_e3 = excluded.lat_e3, lng_e3 = excluded.lng_e3,
        radius_m = excluded.radius_m, places = excluded.places, truncated = excluded.truncated,
        fetched_at = excluded.fetched_at
  returning c.fetched_at
$$;

-- Starts a search that needs live data: one search on the visitor's daily count, then one
-- call on the site's daily upstream count. The visitor is checked first, so a visitor over
-- their limit never spends the site's allowance. Returns 'ok', 'visitor_limit' or
-- 'global_limit'. Callers must treat a database error as a refusal.
create or replace function places.begin_live_search(visitor text, per_visitor int, upstream_limit int)
returns text
language plpgsql
security definer
set search_path = places, pg_temp
as $$
declare
  today     date := (now() at time zone 'utc')::date;
  v_key     text := 'v:' || left(coalesce(visitor, ''), 62);
  visitor_n int;
  global_n  int;
begin
  if per_visitor <= 0 then
    return 'visitor_limit';
  end if;
  if upstream_limit <= 0 then
    return 'global_limit';
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
  values (today, 'upstream', 1)
  on conflict (day, key) do update set n = q.n + 1
    where q.n < upstream_limit
  returning q.n into global_n;

  if global_n is null then
    -- The search is refused, so it should not count against this visitor.
    update places.daily_counts as q set n = q.n - 1
    where q.day = today and q.key = v_key and q.n > 0;
    return 'global_limit';
  end if;

  return 'ok';
end;
$$;

-- One more upstream call for a search that already started, such as the mirror retry.
create or replace function places.take_upstream_slot(upstream_limit int)
returns boolean
language plpgsql
security definer
set search_path = places, pg_temp
as $$
declare
  taken int;
begin
  if upstream_limit <= 0 then
    return false;
  end if;

  insert into places.daily_counts as q (day, key, n)
  values ((now() at time zone 'utc')::date, 'upstream', 1)
  on conflict (day, key) do update set n = q.n + 1
    where q.n < upstream_limit
  returning q.n into taken;

  return taken is not null;
end;
$$;

-- Reserves the next start time for a call to one upstream service, at least that
-- service's interval after the previous reservation. Returns how many milliseconds to
-- wait before calling, or null when the service is paused or the wait would be longer
-- than max_wait_ms. The row lock makes concurrent callers take turns, and it lasts only
-- for this function's transaction, which is what Neon's transaction-mode pooler needs
-- (a session-level advisory lock would outlive the transaction on a shared connection).
create or replace function places.reserve_call(upstream text, max_wait_ms int)
returns int
language plpgsql
security definer
set search_path = places, pg_temp
as $$
declare
  u        places.upstreams%rowtype;
  now_ts   timestamptz;
  start_at timestamptz;
  wait_ms  int;
begin
  select * into u from places.upstreams where name = reserve_call.upstream for update;
  if not found then
    return null;
  end if;

  -- Read the clock after taking the lock, so time spent waiting for it is counted.
  now_ts := clock_timestamp();
  start_at := greatest(u.next_at, now_ts);
  wait_ms := ceil(extract(epoch from (start_at - now_ts)) * 1000)::int;
  if wait_ms > greatest(coalesce(max_wait_ms, 0), 0) then
    return null;
  end if;

  update places.upstreams
  set next_at = start_at + make_interval(secs => u.min_interval_ms / 1000.0)
  where name = reserve_call.upstream;

  return wait_ms;
end;
$$;

-- Leaves a service alone for a while after it refused a call (the Overpass wiki asks for
-- 30 seconds after a 429). Capped at five minutes, so a bad argument cannot stop the app.
create or replace function places.pause_upstream(upstream text, seconds int)
returns void
language sql
security definer
set search_path = places, pg_temp
as $$
  update places.upstreams
  set next_at = greatest(
    next_at,
    clock_timestamp() + make_interval(secs => least(greatest(coalesce(pause_upstream.seconds, 0), 0), 300))
  )
  where name = pause_upstream.upstream
$$;

-- ------------------------------------------------------------------ grants

-- New functions are executable by everyone until revoked, so this runs over all of them on
-- every apply. (A per-schema default privilege cannot remove that grant, and the global one
-- belongs to the shared owner role, so it is left alone.)
revoke all on all functions in schema places from public, places_app;

grant usage on schema places to places_app;
grant execute on function places.geocode_get(text) to places_app;
grant execute on function places.geocode_put(text, boolean, double precision, double precision, text) to places_app;
grant execute on function places.results_get(text, int, int, int) to places_app;
grant execute on function places.results_put(text, int, int, int, jsonb, boolean) to places_app;
grant execute on function places.begin_live_search(text, int, int) to places_app;
grant execute on function places.take_upstream_slot(int) to places_app;
grant execute on function places.reserve_call(text, int) to places_app;
grant execute on function places.pause_upstream(text, int) to places_app;
