-- ============================================================
-- PRICED IN — Supabase schema
-- Paste this whole file into Supabase → SQL Editor → Run.
-- Safe to re-run (idempotent-ish: drops views, creates tables if absent).
-- ============================================================

create extension if not exists pgcrypto;

-- ---------- core: the catalyst board ----------
create table if not exists events (
  id            uuid primary key default gen_random_uuid(),
  ticker        text not null,
  company       text,
  cat           text not null check (cat in ('FDA','READOUT','MA','FLOW')),
  badge         text,                    -- PDUFA / PH3 / ADCOM / 13D / LOCKUP ...
  event         text not null,           -- one-line description
  event_date    date,                    -- best single estimate
  window_start  date,
  window_end    date,
  window_label  text default 'Confirmed',-- 'Confirmed' | 'Est. window' | 'PR guided'
  event_class   text,                    -- key into class_priors / event_outcomes
  status        text not null default 'upcoming',  -- upcoming | passed | resolved | dropped
  note          text,
  sources       jsonb not null default '[]',
  is_sample     boolean not null default false,
  created_at    timestamptz not null default now()
);

-- ---------- enrichment: options pricing snapshots ----------
create table if not exists option_metrics (
  event_id      uuid not null references events(id) on delete cascade,
  ts            timestamptz not null default now(),
  spot          numeric,
  expiry        date,
  call_symbol   text,
  put_symbol    text,
  straddle_mid  numeric,
  implied_move  numeric,   -- percent, straddle_mid / spot * 100
  atm_iv        numeric,   -- percent, if available from feed
  spread_pct    numeric,   -- avg (ask-bid)/mid of the two legs, percent
  open_interest numeric,   -- call OI + put OI
  primary key (event_id, ts)
);

-- ---------- enrichment: insider tape (Form 4) ----------
create table if not exists insider_txns (
  id        bigserial primary key,
  event_id  uuid references events(id) on delete cascade,
  ticker    text not null,
  owner     text,
  role      text,
  code      text check (code in ('P','S')),  -- P = open-market buy, S = sale
  shares    numeric,
  price     numeric,
  value     numeric,
  filed     date,
  unique (ticker, owner, filed, code, shares)
);

-- ---------- enrichment: balance sheet / dilution ----------
create table if not exists balance_flags (
  event_id       uuid primary key references events(id) on delete cascade,
  cash           numeric,
  quarterly_burn numeric,
  runway_q       numeric,
  shelf_active   boolean,
  shelf_note     text,
  updated_at     timestamptz not null default now()
);

-- ---------- the moat: realized outcomes library ----------
create table if not exists event_outcomes (
  id            bigserial primary key,
  ticker        text,
  event_class   text,
  event_date    date,
  close_before  numeric,
  close_after   numeric,
  realized_move numeric,   -- percent, absolute
  recorded_at   timestamptz not null default now(),
  unique (ticker, event_class, event_date)
);

-- ---------- bootstrap priors until your own library has depth ----------
-- Replace/refine these with your own computed stats over time. The board
-- prefers your measured class history once a class has n >= 8 outcomes.
create table if not exists class_priors (
  event_class text primary key,
  hist_move   numeric not null,
  n           int not null
);
insert into class_priors (event_class, hist_move, n) values
  ('pdufa_post_adcom',   62, 41),
  ('pdufa_no_adcom',     45, 35),
  ('adcom_panel',        68, 27),
  ('crl_resubmission',   41, 22),
  ('ph3_topline',        78, 30),
  ('ph3_topline_ipf',    92, 18),
  ('ph2b_topline_cns',   57, 24),
  ('ph3_interim_dsmb',   64, 19),
  ('merger_vote_cash',    5, 33),
  ('strategic_review',   33, 26),
  ('lockup_large',       19, 45)
on conflict (event_class) do nothing;

-- ---------- small key/value store for the notifier ----------
create table if not exists kv (
  key text primary key,
  val jsonb not null default '{}'
);

-- ---------- views ----------
drop view if exists event_board;
drop view if exists class_history;

create view class_history with (security_invoker = true) as
select event_class, count(*)::int as n, round(avg(realized_move)::numeric, 0) as avg_move
from event_outcomes
group by event_class;

create view event_board with (security_invoker = true) as
select
  e.id, e.ticker, e.company, e.cat, e.badge, e.event,
  e.event_date, e.window_start, e.window_end, e.window_label,
  e.event_class, e.note, e.sources, e.is_sample, e.created_at,
  om.spot, om.expiry, om.call_symbol, om.put_symbol,
  om.straddle_mid, om.implied_move, om.atm_iv, om.spread_pct,
  om.open_interest, om.ts as priced_at,
  coalesce(ch.avg_move, cp.hist_move) as hist_move,
  coalesce(ch.n, cp.n)                as hist_n,
  (ch.n is not null)                  as hist_measured,
  bf.runway_q, bf.shelf_active, bf.shelf_note,
  it.buys_90d, it.sells_90d, it.tape
from events e
left join lateral (
  select * from option_metrics om2
  where om2.event_id = e.id
  order by om2.ts desc limit 1
) om on true
left join class_history ch on ch.event_class = e.event_class and ch.n >= 8
left join class_priors  cp on cp.event_class = e.event_class
left join balance_flags bf on bf.event_id = e.id
left join lateral (
  select
    count(*) filter (where code = 'P')::int as buys_90d,
    count(*) filter (where code = 'S')::int as sells_90d,
    coalesce(
      jsonb_agg(
        jsonb_build_object('owner', owner, 'role', role, 'code', code,
                           'value', value, 'filed', filed)
        order by filed desc
      ),
      '[]'::jsonb
    ) as tape
  from insider_txns t
  where t.event_id = e.id and t.filed > current_date - 90
) it on true
where e.status = 'upcoming';

-- ---------- row level security: public read, service-key write ----------
alter table events         enable row level security;
alter table option_metrics enable row level security;
alter table insider_txns   enable row level security;
alter table balance_flags  enable row level security;
alter table event_outcomes enable row level security;
alter table class_priors   enable row level security;
alter table kv             enable row level security;

do $$ begin
  create policy anon_read_events    on events         for select to anon, authenticated using (true);
  create policy anon_read_om        on option_metrics for select to anon, authenticated using (true);
  create policy anon_read_it        on insider_txns   for select to anon, authenticated using (true);
  create policy anon_read_bf        on balance_flags  for select to anon, authenticated using (true);
  create policy anon_read_eo        on event_outcomes for select to anon, authenticated using (true);
  create policy anon_read_cp        on class_priors   for select to anon, authenticated using (true);
exception when duplicate_object then null; end $$;
-- note: no anon policy on kv (notifier state stays private).
-- Workers use the service_role key, which bypasses RLS.

grant select on class_history, event_board to anon, authenticated;

-- ---------- sample rows so the board renders on day one ----------
-- Fictional tickers: the options worker will log "no chain" for these.
-- Replace with real events via Table Editor → events → Insert row.
insert into events (ticker, company, cat, badge, event, event_date, window_label, event_class, note, is_sample)
values
  ('AXPH', 'Axiapharm (sample)', 'FDA', 'PDUFA',
   'PDUFA — axelotinib in 2L NSCLC', current_date + 17, 'Confirmed',
   'pdufa_post_adcom', '[SAMPLE] Replace me with a real event.', true),
  ('ZPHR', 'Zephyra Bio (sample)', 'READOUT', 'PH3',
   'Ph3 topline — ZPH-201 in IPF', current_date + 33, 'Est. window',
   'ph3_topline_ipf', '[SAMPLE] Replace me with a real event.', true),
  ('TRBX', 'Terabax (sample)', 'MA', '13D',
   'Strategic review — 13D filed (8.4% holder)', current_date + 45, 'Est. window',
   'strategic_review', '[SAMPLE] Replace me with a real event.', true)
on conflict do nothing;
