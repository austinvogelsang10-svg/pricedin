-- Run once in Supabase SQL Editor (after 002).
-- Lexicon score for curation sorting + a status index for board queries.
alter table events add column if not exists discovery_score int;
create index if not exists idx_events_status on events(status);
