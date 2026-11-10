-- The place data, from Overture Maps (see scripts/load_places.py). Each load builds it as
-- places.places_staging inside one transaction, then renames it to places.places, so the live
-- table is never half loaded. Columns are kept lean to stay well under 200 MB with indexes.

create table places.places_staging (
  id         uuid primary key,                       -- Overture's GERS id
  name       text not null check (length(name) between 1 and 120),
  category   text not null check (length(category) between 1 and 80),  -- taxonomy.primary
  street     text check (length(street) <= 120),
  locality   text check (length(locality) <= 60),
  postcode   text check (length(postcode) <= 12),
  phone      text check (length(phone) <= 32),
  website    text check (website like 'https://%' and length(website) <= 300),
  confidence real not null check (confidence between 0 and 1),
  datasets   text[] not null,                        -- source datasets, for attribution
  borough    text not null,
  geog       geography(Point, 4326) not null
);
