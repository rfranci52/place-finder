-- Indexes for the staging table, built after the bulk copy (faster than maintaining them row by
-- row). The load renames them along with the table.

-- Nearest-first search within a radius: ST_DWithin and the <-> ordering both use this.
create index places_staging_geog_idx on places.places_staging using gist (geog);

-- Name search for terms outside the vocabulary: ILIKE substring matches and the <% word
-- similarity operator both use trigram indexes.
create index places_staging_name_trgm_idx on places.places_staging using gin (name gin_trgm_ops);

-- Category filters.
create index places_staging_category_idx on places.places_staging (category);
