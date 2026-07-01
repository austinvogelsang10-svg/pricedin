# Priced In — Setup Guide (100% browser, no local terminal)

Everything below happens in four browser tabs: **GitHub**, **Supabase**, **DigitalOcean web console**, and **Netlify**. Works from a phone; the GitHub upload step is easiest on a desktop browser.

## What you'll have at the end

```
DigitalOcean droplet ──(hourly/daily systemd timers)──► Supabase (Postgres)
  enrich_options   Alpaca ATM straddle → implied move,        ▲
  enrich_insiders  EDGAR Form 4 buys/sells (90d)              │ anon key, read-only
  enrich_balance   XBRL cash runway + S-3 shelf flag          │
  record_outcomes  realized moves → your historical library   ▼
  notify           Discord alerts                    Netlify: dashboard/index.html
```

Monthly cost: **$6–12 droplet + $0 Supabase free tier + $0 Netlify + $0 data feeds.**

---

## Step 1 — Supabase (5 min)

1. supabase.com → your existing org → **New project** (or reuse a project — tables are namespaced by name and won't collide with your catalyst-bot tables unless you already have tables named `events`, `kv`, etc. If you do, use a fresh project).
2. Left sidebar → **SQL Editor** → **New query** → paste the entire contents of `db/schema.sql` → **Run**. You should see "Success" and three sample rows in the events table.
3. **Project Settings → API** — copy three things into a note:
   - Project URL (`https://xxxx.supabase.co`)
   - `anon` public key (for the dashboard)
   - `service_role` key (for the droplet — keep this one secret)

## Step 2 — GitHub repo (5 min)

1. github.com → **New repository** → name it `pricedin` → **Public** (lets the droplet clone without auth; the repo contains no secrets — keys live only in `.env` on the droplet and in the dashboard file on Netlify) → Create.
2. **Add file → Upload files** → drag in the extracted contents of the zip (keep the folder structure: `db/`, `workers/`, `systemd/`, `dashboard/`, plus the root files). Commit.
   - *Phone fallback:* use **Add file → Create new file**, type e.g. `workers/common.py` as the name (the `/` creates the folder), paste the contents, commit. Repeat per file. Tedious but works.
3. If you'd rather keep the repo **private**: on the droplet, clone with a fine-grained personal access token instead: `git clone https://<TOKEN>@github.com/YOU/pricedin.git`.

## Step 3 — Droplet (10 min)

**Specs:** Create Droplet → **Ubuntu 24.04 LTS** → Basic → Regular → **$12/mo (1 vCPU / 2GB)** — or $6/1GB, the setup script adds swap automatically. Region: NYC. Enable Monitoring. Password auth is fine since you'll use the web console.

*(Reusing your existing bot droplet? Skip creation — the workload is a few hundred HTTP calls an hour. Just make sure nothing else owns `/opt/pricedin`.)*

Open **your droplet → Access → Launch Droplet Console** (log in as root) and paste, one block at a time:

```bash
apt update && apt install -y git
git clone https://github.com/YOURUSER/pricedin.git /opt/pricedin
cd /opt/pricedin && bash setup.sh
```

Then add your keys:

```bash
nano .env
```

Fill in `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `ALPACA_KEY_ID`, `ALPACA_SECRET`, `SEC_CONTACT` (any email — the SEC requires a contact in the User-Agent), and optionally `DISCORD_WEBHOOK`. Save with **Ctrl+O, Enter, Ctrl+X**.

> **Alpaca note:** in the Alpaca dashboard, make sure **options trading is enabled** on your paper account (Account → Configure). The workers use the free `indicative` options feed.

First live run + check the logs:

```bash
systemctl start pricedin-hourly
journalctl -u pricedin-hourly -n 40 --no-pager
systemctl start pricedin-daily
journalctl -u pricedin-daily -n 40 --no-pager
```

Expected on a fresh install: the three SAMPLE tickers log `no spot price / no CIK — skipping`. That means the plumbing works and the fictional tickers correctly find nothing.

## Step 4 — Add your first real event (2 min)

Supabase → **Table Editor → events → Insert row**:

| column | example |
|---|---|
| ticker | a real ticker your scanner flagged |
| cat | `FDA`, `READOUT`, `MA`, or `FLOW` |
| badge | `PDUFA`, `PH3`, `13D`, `LOCKUP`… |
| event | one-line description |
| event_date | the date |
| window_label | `Confirmed` or `Est. window` |
| event_class | one of the keys in `class_priors` (or add your own prior row) |

Then in the DO console: `systemctl start pricedin-hourly && systemctl start pricedin-daily` and watch the logs — you should see a real implied move, Form 4 lines, and a runway calc within a minute or two.

**Wiring your existing catalyst bot:** point it at the same Supabase project and have it `INSERT` into `events` when it finds a catalyst (PostgREST insert with the service key — same pattern as `sb_insert` in `workers/common.py`). Discovery stays your bot's job; enrichment is this repo's job.

## Step 5 — Dashboard on Netlify (5 min)

1. In GitHub, open `dashboard/index.html` → pencil icon → paste your **Project URL** and **anon key** into the `CONFIG` block at the top (anon key only — never the service key; anon is safe to ship because RLS makes it read-only). Commit.
2. Download that file (Raw → save), drop it in a folder named anything, and deploy to Netlify the same drag-and-drop way as your other single-file apps.
3. Open the site: the header dot should read **LIVE · EDGAR + ALPACA**. If it says SAMPLE MODE, the config block is blank or the fetch failed (check keys).

## Day-to-day

- **Deploy loop for worker changes:** edit on GitHub → DO console: `cd /opt/pricedin && git pull && systemctl start pricedin-hourly`.
- **Dashboard changes:** edit `index.html` on GitHub → re-drop on Netlify.
- **Logs:** `journalctl -u pricedin-hourly --since today --no-pager`
- **Timers status:** `systemctl list-timers | grep pricedin`

## Verify checklist

- [ ] `schema.sql` ran clean; `event_board` view returns rows in SQL Editor (`select * from event_board;`)
- [ ] hourly log shows an implied-move line for your real event
- [ ] daily log shows Form 4 parsing and a runway number
- [ ] dashboard shows LIVE and the gap gauge renders with real numbers
- [ ] (optional) Discord webhook received the 🆕 NEW alert

## Known v1 edges (honest list)

- Implied move is measured **through the expiry after the event**, so it slightly overstates the pure event move. Fine for ranking; term-structure extraction is the v2 refinement.
- Shelf flag = "S-3 filed in 3 years," not remaining capacity or effectiveness.
- Events with **no listed options** show "no chain" — which is itself useful information.
- Alerts are whole-board via one Discord webhook. Per-user rules/channels arrive with Supabase auth when you build the paid tier.
- Outcomes settle T+2 using daily IEX closes; halted or delisted names may need a manual row.

## Next steps toward the paid tier

Supabase Auth (magic link) → `watchlists` table keyed to user id → per-user alert rules → Stripe checkout gating the PRO sections. All of that layers on this schema without touching the workers.
