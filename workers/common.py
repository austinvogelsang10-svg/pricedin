"""Shared helpers for Priced In workers.

Everything talks plain HTTPS via `requests`:
  - Supabase PostgREST (service key -> bypasses RLS for writes)
  - Alpaca trading + market data APIs
  - SEC EDGAR (submissions, Form 4 XML, XBRL company facts)
"""
import os, time, logging, datetime as dt
import requests
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("pricedin")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SERVICE_KEY  = os.environ["SUPABASE_SERVICE_KEY"]

ALPACA_KEY    = os.environ.get("ALPACA_KEY_ID", "")
ALPACA_SECRET = os.environ.get("ALPACA_SECRET", "")
ALPACA_TRADE  = os.environ.get("ALPACA_TRADING_HOST", "https://paper-api.alpaca.markets").rstrip("/")
ALPACA_DATA   = "https://data.alpaca.markets"

SEC_CONTACT     = os.environ.get("SEC_CONTACT", "pricedin@example.com")
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")

TODAY = dt.date.today()

# ---------------- Supabase (PostgREST) ----------------

def _sb_headers(extra=None):
    h = {
        "apikey": SERVICE_KEY,
        "Authorization": f"Bearer {SERVICE_KEY}",
        "Content-Type": "application/json",
    }
    if extra:
        h.update(extra)
    return h

def sb_select(table, params=None):
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{table}",
                     headers=_sb_headers(), params=params or {}, timeout=30)
    r.raise_for_status()
    return r.json()

def sb_insert(table, rows, upsert=False):
    if not rows:
        return []
    # PostgREST bulk writes require every row to share the same key set
    # (else: PGRST102 "All object keys must match"). Normalize with nulls.
    keys = sorted({k for r in rows for k in r})
    rows = [{k: r.get(k) for k in keys} for r in rows]
    extra = {"Prefer": "return=minimal"}
    if upsert:
        extra["Prefer"] = "resolution=merge-duplicates,return=minimal"
    r = requests.post(f"{SUPABASE_URL}/rest/v1/{table}",
                      headers=_sb_headers(extra), json=rows, timeout=30)
    if r.status_code >= 400:
        log.error("supabase insert %s -> %s %s", table, r.status_code, r.text[:300])
    return r

def sb_upsert(table, rows):
    return sb_insert(table, rows, upsert=True)

def sb_update(table, match_params, patch):
    r = requests.patch(f"{SUPABASE_URL}/rest/v1/{table}",
                       headers=_sb_headers({"Prefer": "return=minimal"}),
                       params=match_params, json=patch, timeout=30)
    if r.status_code >= 400:
        log.error("supabase update %s -> %s %s", table, r.status_code, r.text[:300])
    return r

def kv_get(key, default=None):
    rows = sb_select("kv", {"key": f"eq.{key}", "select": "val"})
    return rows[0]["val"] if rows else default

def kv_set(key, val):
    sb_upsert("kv", [{"key": key, "val": val}])

# ---------------- Alpaca ----------------

def alpaca_headers():
    return {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}

def alpaca_get(url, params=None):
    r = requests.get(url, headers=alpaca_headers(), params=params or {}, timeout=30)
    if r.status_code == 429:
        time.sleep(2)
        r = requests.get(url, headers=alpaca_headers(), params=params or {}, timeout=30)
    r.raise_for_status()
    return r.json()

def spot_price(ticker):
    """Latest trade price via the free IEX feed."""
    try:
        j = alpaca_get(f"{ALPACA_DATA}/v2/stocks/{ticker}/snapshot", {"feed": "iex"})
        lt = j.get("latestTrade") or {}
        px = lt.get("p")
        if not px:
            px = (j.get("dailyBar") or {}).get("c")
        return float(px) if px else None
    except Exception as e:
        log.warning("spot %s failed: %s", ticker, e)
        return None

def daily_closes(ticker, start, end):
    """{date: close} from Alpaca daily bars (IEX feed)."""
    out = {}
    try:
        j = alpaca_get(f"{ALPACA_DATA}/v2/stocks/{ticker}/bars", {
            "timeframe": "1Day", "feed": "iex", "adjustment": "split",
            "start": f"{start}T00:00:00Z", "end": f"{end}T23:59:59Z", "limit": 10000,
        })
        for b in j.get("bars") or []:
            out[b["t"][:10]] = float(b["c"])
    except Exception as e:
        log.warning("bars %s failed: %s", ticker, e)
    return out

# ---------------- SEC EDGAR ----------------

def sec_get(url, is_json=True):
    """SEC requires a descriptive User-Agent with contact info; be polite on rate."""
    time.sleep(0.15)
    r = requests.get(url, headers={"User-Agent": f"PricedIn/1.0 ({SEC_CONTACT})",
                                   "Accept-Encoding": "gzip"}, timeout=30)
    r.raise_for_status()
    return r.json() if is_json else r.text

_CIK_CACHE = None

def cik_for(ticker):
    global _CIK_CACHE
    if _CIK_CACHE is None:
        j = sec_get("https://www.sec.gov/files/company_tickers.json")
        _CIK_CACHE = {v["ticker"].upper(): int(v["cik_str"]) for v in j.values()}
    return _CIK_CACHE.get(ticker.upper())

def submissions(cik):
    return sec_get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")

# ---------------- misc ----------------

def board_events(days_ahead=120):
    """Upcoming events worth enriching."""
    rows = sb_select("events", {
        "select": "id,ticker,event_date,window_start,window_end,event_class,status,is_sample",
        "status": "eq.upcoming",
        "order": "event_date.asc",
    })
    keep = []
    for e in rows:
        d = e.get("window_end") or e.get("event_date")
        if not d:
            continue
        dd = dt.date.fromisoformat(d)
        if dd <= TODAY + dt.timedelta(days=days_ahead):
            e["_target_date"] = dd
            keep.append(e)
    return keep
