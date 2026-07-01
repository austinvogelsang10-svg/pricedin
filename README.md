# Priced In

Forward-looking catalyst terminal: for every upcoming binary event, compare the
options-implied move against the historical realized move for that event class.

- `db/schema.sql` — Supabase schema (events, enrichment, outcomes library, `event_board` view)
- `workers/` — Python enrichment: Alpaca options pricing, EDGAR Form 4 insiders,
  XBRL cash runway + shelf flags, realized-outcome recorder, Discord notifier
- `systemd/` — hourly + daily timers
- `dashboard/index.html` — single-file React dashboard (Netlify drag-and-drop)
- `SETUP.md` — full browser-only setup guide (GitHub + DO web console + Supabase + Netlify)

Research tool, not investment advice. Sample tickers in the seed data are fictional.
