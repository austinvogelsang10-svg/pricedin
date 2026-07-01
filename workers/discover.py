"""Discovery worker v2 — puts events ON the board automatically, with taste.

Improvements over v1 (and over the old standalone bot):

  GATE      Every candidate must be *tradeable for this product*: real spot
            price on Alpaca, price within band, and at least one listed
            option contract. Kills OTC shells, foreign filers, and junk
            13Ds on random industrials before they ever hit the board.
  LEXICON   Biotech/M&A term scoring stored per event (discovery_score) so
            curation in the table editor can sort signal from noise. Noise
            blocklist skips ETFs/trusts/SPAC shells outright.
  DEPTH     8-Ks are scanned across up to 3 filing documents (press-release
            exhibits included, where the dates actually live) with a multi-
            pattern date battery. One fetch, two detectors: PDUFA dates AND
            AdCom meeting dates. Priority Review gets flagged in the note.
  PROXIES   DEFM14A meeting dates are PARSED from the document ("special
            meeting ... on Month D, YYYY") -> Confirmed; heuristic +35d is
            only the fallback.
  LOCKUPS   New source: 424B4 IPO pricings -> lockup expiry events at
            T+180d (est. window).
  SANITY    Discovered dates clamped to (today, today+400d) to catch regex
            misfires.

Passes:
  run_fast()  (hourly) — EDGAR FTS since last check: 13D, DEFM14A, 8-K
                          (PDUFA + AdCom), 424B4 lockups
  run_slow()  (daily)  — CT.gov industry Phase 3 sweep (120d horizon)

Dedupe: on_conflict(ticker,event_class,event_date) + ignore-duplicates.
Requires db/002_discovery.sql (unique index) and db/003_quality.sql
(discovery_score column).
"""
import os
import re
import datetime as dt
from urllib.parse import urlencode
import requests

from .common import (log, sec_get, kv_get, kv_set, TODAY,
                     SUPABASE_URL, SERVICE_KEY, spot_price, alpaca_get,
                     ALPACA_TRADE)

FTS = "https://efts.sec.gov/LATEST/search-index"
CTG = "https://clinicaltrials.gov/api/v2/studies"

MAX_SLOW_INSERTS = int(os.environ.get("DISCOVER_MAX_INSERTS", "12"))
MAX_DOC_FETCH    = int(os.environ.get("DISCOVER_MAX_DOCS", "14"))
PRICE_MIN        = float(os.environ.get("TRADEABLE_MIN_PRICE", "1"))
PRICE_MAX        = float(os.environ.get("TRADEABLE_MAX_PRICE", "1000"))

MONTHS = ("January|February|March|April|May|June|July|August|"
          "September|October|November|December")
MONTH_NUM = {m: i + 1 for i, m in enumerate(MONTHS.split("|"))}
DATE_RE  = re.compile(r"(" + MONTHS + r")\s+(\d{1,2}),?\s+(20\d{2})", re.I)

PDUFA_KW = re.compile(r"PDUFA|target\s+action\s+date|goal\s+date|"
                      r"user\s+fee\s+(?:act\s+)?(?:goal\s+)?date", re.I)
ADCOM_KW = re.compile(r"advisory\s+committee\s+(?:meeting|will|is|has|scheduled)", re.I)
MEET_KW  = re.compile(r"special\s+meeting", re.I)
PRIORITY_RE = re.compile(r"priority\s+review", re.I)

# ---------- lexicon: score what the old bot scored, plus M&A ----------
LEXICON = {
    "pdufa": 3, "nda": 2, "bla": 2, "snda": 2, "crl": 2,
    "phase 3": 2, "phase iii": 2, "topline": 2, "top-line": 2,
    "advisory committee": 2, "breakthrough therapy": 2, "priority review": 2,
    "orphan drug": 1, "fast track": 1, "accelerated approval": 2,
    "definitive merger": 3, "merger agreement": 2, "tender offer": 2,
    "strategic alternatives": 2, "unsolicited": 2, "going private": 2,
}
NOISE = {"ETF", "TRUST", "FUND", "ACQUISITION", "SPAC", "DEPOSITARY",
         "MUNICIPAL", "INCOME"}

def lex_score(*texts):
    blob = " ".join(t.lower() for t in texts if t)
    return sum(w for term, w in LEXICON.items() if term in blob)

def noisy(company):
    toks = set(re.sub(r"[^A-Za-z ]", " ", (company or "").upper()).split())
    return bool(toks & NOISE)

# ---------- tradeability gate ----------
_gate_cache = {}

def tradeable(ticker):
    """(ok, why). Fails open on API errors — a blip shouldn't drop a catalyst."""
    if ticker in _gate_cache:
        return _gate_cache[ticker]
    try:
        px = spot_price(ticker)
        if px is None:
            res = (False, "no US equity data")
        elif not (PRICE_MIN <= px <= PRICE_MAX):
            res = (False, f"price {px} outside band")
        else:
            j = alpaca_get(f"{ALPACA_TRADE}/v2/options/contracts", {
                "underlying_symbols": ticker, "limit": 1, "status": "active",
                "expiration_date_gte": TODAY.isoformat()})
            res = ((True, "ok") if (j.get("option_contracts") or [])
                   else (False, "no listed options"))
    except Exception as ex:
        log.warning("%s: gate check errored (%s) — failing open", ticker, ex)
        res = (True, "unverified")
    _gate_cache[ticker] = res
    if not res[0]:
        log.info("%s: gated out — %s", ticker, res[1])
    return res

def sane_date(d):
    return d and TODAY < d <= TODAY + dt.timedelta(days=400)

# ---------- supabase insert with dedupe ----------

def push_events(rows):
    """Returns inserted count, or None on failure (so callers can avoid
    advancing their seen-state and will retry the same window next pass)."""
    if not rows:
        return 0
    # PostgREST bulk insert requires uniform keys across all rows.
    keys = sorted({k for r in rows for k in r})
    rows = [{k: r.get(k) for k in keys} for r in rows]
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/events?on_conflict=ticker,event_class,event_date",
        headers={"apikey": SERVICE_KEY,
                 "Authorization": f"Bearer {SERVICE_KEY}",
                 "Content-Type": "application/json",
                 "Prefer": "resolution=ignore-duplicates,return=minimal"},
        json=rows, timeout=30)
    if r.status_code >= 400:
        log.error("discover insert -> %s %s (did you run db/002 + db/003?)",
                  r.status_code, r.text[:300])
        return None
    return len(rows)

# ---------- EDGAR full-text search plumbing ----------

def fts_hits(query, forms, start):
    params = {"q": f'"{query}"', "forms": forms, "dateRange": "custom",
              "startdt": start, "enddt": TODAY.isoformat()}
    try:
        j = sec_get(FTS + "?" + urlencode(params))
    except Exception as ex:
        log.warning("FTS %s/%s failed: %s", forms, query, ex)
        return []
    return (j.get("hits") or {}).get("hits") or []

def hit_info(h):
    s = h.get("_source") or {}
    ticker = company = None
    for n in (s.get("display_names") or []):
        m = re.search(r"^(.*?)\s*\(([A-Z][A-Z0-9.\-]{0,5})\)\s*\(CIK", n)
        if m:
            company, ticker = m.group(1).strip(), m.group(2)
            break
    ciks = s.get("ciks") or []
    _id = h.get("_id", "")
    return {"ticker": ticker, "company": company,
            "adsh": s.get("adsh") or "", "filed": s.get("file_date"),
            "cik": int(ciks[0]) if ciks else None,
            "fname": _id.split(":", 1)[1] if ":" in _id else None}

def filing_docs(cik, adsh, primary=None, cap=3):
    """Yield stripped text of up to `cap` html docs in a filing —
    press-release exhibits first, since that's where dates live."""
    base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{adsh.replace('-', '')}"
    names = []
    try:
        idx = sec_get(f"{base}/index.json")
        items = ((idx.get("directory") or {}).get("item")) or []
        names = [it["name"] for it in items
                 if it.get("name", "").lower().endswith((".htm", ".html"))]
    except Exception:
        pass
    if primary and primary not in names:
        names.insert(0, primary)
    names.sort(key=lambda n: 0 if re.search(r"ex[-_]?99|press", n, re.I) else 1)
    for name in names[:cap]:
        try:
            raw = sec_get(f"{base}/{name}", is_json=False)[:500_000]
            yield re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw))
        except Exception as ex:
            log.warning("doc fetch %s/%s failed: %s", adsh, name, ex)

def find_date_near(text, kw_re, span=240):
    for m in kw_re.finditer(text):
        dm = DATE_RE.search(text[m.end(): m.end() + span])
        if not dm:
            continue
        try:
            d = dt.date(int(dm.group(3)),
                        MONTH_NUM[dm.group(1).capitalize()],
                        int(dm.group(2)))
        except ValueError:
            continue
        if sane_date(d):
            return d
    return None

# ---------- fast pass ----------

def run_fast():
    state = kv_get("discover_state", {}) or {}
    seen = set(state.get("seen", []))
    start = state.get("last_fts") or (TODAY - dt.timedelta(days=7)).isoformat()
    rows, fetched = [], 0

    def fresh(i):
        return i["ticker"] and i["filed"] and i["adsh"] not in seen

    # 1) SC 13D -> strategic review windows
    for h in fts_hits("Schedule 13D", "SC 13D", start):
        i = hit_info(h)
        if not fresh(i):
            continue
        seen.add(i["adsh"])
        if noisy(i["company"]) or not tradeable(i["ticker"])[0]:
            continue
        filed = dt.date.fromisoformat(i["filed"])
        rows.append({"ticker": i["ticker"], "company": i["company"],
                     "cat": "MA", "badge": "13D",
                     "event": "SC 13D filed — new ≥5% holder",
                     "event_date": (filed + dt.timedelta(days=60)).isoformat(),
                     "window_start": filed.isoformat(),
                     "window_end": (filed + dt.timedelta(days=120)).isoformat(),
                     "window_label": "Est. window",
                     "event_class": "strategic_review",
                     "discovery_score": lex_score(i["company"]),
                     "note": f"auto · 13D filed {i['filed']}",
                     "sources": [f"SC 13D · {i['adsh']}"]})

    # 2) DEFM14A -> vote events, meeting date parsed when possible
    for h in fts_hits("merger agreement", "DEFM14A", start):
        i = hit_info(h)
        if not fresh(i):
            continue
        seen.add(i["adsh"])
        if noisy(i["company"]) or not tradeable(i["ticker"])[0]:
            continue
        filed = dt.date.fromisoformat(i["filed"])
        meet = None
        if i["cik"] and fetched < MAX_DOC_FETCH:
            fetched += 1
            for text in filing_docs(i["cik"], i["adsh"], i["fname"], cap=1):
                meet = find_date_near(text, MEET_KW)
                if meet:
                    break
        row = {"ticker": i["ticker"], "company": i["company"], "cat": "MA",
               "badge": "VOTE",
               "event": "Definitive merger proxy — shareholder vote",
               "event_class": "merger_vote_cash",
               "discovery_score": lex_score(i["company"], "merger agreement"),
               "note": f"auto · DEFM14A filed {i['filed']}",
               "sources": [f"DEFM14A · {i['adsh']}"]}
        if meet:
            row.update({"event_date": meet.isoformat(),
                        "window_label": "Confirmed"})
        else:
            row.update({"event_date": (filed + dt.timedelta(days=35)).isoformat(),
                        "window_start": (filed + dt.timedelta(days=21)).isoformat(),
                        "window_end": (filed + dt.timedelta(days=60)).isoformat(),
                        "window_label": "Est. window"})
        rows.append(row)

    # 3) 8-Ks: one fetch, two detectors — PDUFA dates and AdCom dates
    kw_hits = {}
    for q in ("PDUFA", "advisory committee"):
        for h in fts_hits(q, "8-K", start):
            i = hit_info(h)
            if fresh(i):
                kw_hits[i["adsh"]] = i
    for i in kw_hits.values():
        if fetched >= MAX_DOC_FETCH:
            break
        seen.add(i["adsh"])
        if noisy(i["company"]) or not i["cik"]:
            continue
        if not tradeable(i["ticker"])[0]:
            continue
        fetched += 1
        pdufa = adcom = None
        priority = False
        blob = ""
        for text in filing_docs(i["cik"], i["adsh"], i["fname"]):
            blob += text[:8000]
            pdufa = pdufa or find_date_near(text, PDUFA_KW)
            adcom = adcom or find_date_near(text, ADCOM_KW)
            priority = priority or bool(PRIORITY_RE.search(text))
            if pdufa and adcom:
                break
        score = lex_score(i["company"], blob)
        tail = " · Priority Review" if priority else ""
        if pdufa:
            rows.append({"ticker": i["ticker"], "company": i["company"],
                         "cat": "FDA", "badge": "PDUFA",
                         "event": "PDUFA — disclosed in 8-K",
                         "event_date": pdufa.isoformat(),
                         "window_label": "Confirmed",
                         "event_class": "pdufa_no_adcom",
                         "discovery_score": score,
                         "note": f"auto · 8-K {i['filed']}{tail}",
                         "sources": [f"8-K · {i['adsh']}"]})
        if adcom:
            rows.append({"ticker": i["ticker"], "company": i["company"],
                         "cat": "FDA", "badge": "ADCOM",
                         "event": "FDA advisory committee — disclosed in 8-K",
                         "event_date": adcom.isoformat(),
                         "window_label": "Confirmed",
                         "event_class": "adcom_panel",
                         "discovery_score": score,
                         "note": f"auto · 8-K {i['filed']}{tail}",
                         "sources": [f"8-K · {i['adsh']}"]})
        if not pdufa and not adcom:
            log.info("%s: FDA-flavored 8-K, no parseable date — review: adsh %s",
                     i["ticker"], i["adsh"])

    # 4) 424B4 IPO pricings -> lockup expiry (assumed 180d)
    for h in fts_hits("lock-up", "424B4", start):
        i = hit_info(h)
        if not fresh(i):
            continue
        seen.add(i["adsh"])
        if noisy(i["company"]) or not tradeable(i["ticker"])[0]:
            continue
        filed = dt.date.fromisoformat(i["filed"])
        exp = filed + dt.timedelta(days=180)
        if not sane_date(exp):
            continue
        rows.append({"ticker": i["ticker"], "company": i["company"],
                     "cat": "FLOW", "badge": "LOCKUP",
                     "event": "IPO lockup expiry (assumed 180d)",
                     "event_date": exp.isoformat(),
                     "window_start": (exp - dt.timedelta(days=7)).isoformat(),
                     "window_end": (exp + dt.timedelta(days=7)).isoformat(),
                     "window_label": "Est. window",
                     "event_class": "lockup_large",
                     "discovery_score": lex_score(i["company"]),
                     "note": f"auto · priced {i['filed']}",
                     "sources": [f"424B4 · {i['adsh']}"]})

    n = push_events(rows)
    if n is None:
        log.warning("insert failed — discovery state NOT advanced; "
                    "same window will be retried next pass")
        return
    kv_set("discover_state", {**state, "last_fts": TODAY.isoformat(),
                              "seen": sorted(seen)[-800:]})
    log.info("discover fast: +%d events (13D/proxy/8-K/lockup), %d docs opened",
             n, fetched)

# ---------- slow pass: CT.gov Phase 3 sweep ----------

_SUFFIX = {"INC", "INCORPORATED", "CORP", "CORPORATION", "LTD", "LIMITED",
           "PLC", "CO", "COMPANY", "HOLDINGS", "HOLDING", "GROUP", "SA",
           "NV", "AG", "AB"}

def _norm(s):
    s = re.sub(r"[^A-Za-z0-9 ]", " ", (s or "").upper())
    return " ".join(t for t in s.split() if t not in _SUFFIX)

def ticker_map():
    j = sec_get("https://www.sec.gov/files/company_tickers.json")
    return {_norm(v["title"]): v["ticker"].upper() for v in j.values()}

def match_ticker(sponsor, tmap):
    n = _norm(sponsor)
    if n in tmap:
        return tmap[n]
    if len(n) >= 8:
        for k, v in tmap.items():
            if len(k) >= 8 and (k.startswith(n) or n.startswith(k)):
                return v
    return None

def parse_pcd(datestr):
    parts = (datestr or "").split("-")
    try:
        if len(parts) == 2:
            y, m = int(parts[0]), int(parts[1])
            first = dt.date(y, m, 1)
            last = (dt.date(y + (m == 12), (m % 12) + 1, 1)
                    - dt.timedelta(days=1))
            return dt.date(y, m, 15), first, last
        if len(parts) == 3:
            d = dt.date.fromisoformat(datestr)
            return d, d - dt.timedelta(days=10), d + dt.timedelta(days=14)
    except ValueError:
        pass
    return None, None, None

def run_slow():
    state = kv_get("discover_state", {}) or {}
    seen_nct = set(state.get("seen_nct", []))
    horizon = TODAY + dt.timedelta(days=120)
    params = {
        "filter.overallStatus": "ACTIVE_NOT_RECRUITING",
        "query.term": (f'AREA[Phase]"PHASE3" AND AREA[LeadSponsorClass]INDUSTRY '
                       f'AND AREA[PrimaryCompletionDate]RANGE[{TODAY},{horizon}]'),
        "fields": ("protocolSection.identificationModule,"
                   "protocolSection.statusModule,"
                   "protocolSection.sponsorCollaboratorsModule,"
                   "protocolSection.designModule"),
        "pageSize": "100", "format": "json",
    }
    try:
        r = requests.get(CTG, params=params, timeout=30,
                         headers={"User-Agent": "PricedIn/1.0"})
        r.raise_for_status()
        studies = r.json().get("studies") or []
    except Exception as ex:
        log.warning("CT.gov sweep failed: %s", ex)
        return

    tmap = ticker_map()
    rows = []
    for st in studies:
        if len(rows) >= MAX_SLOW_INSERTS:
            break
        p = st.get("protocolSection") or {}
        nct = (p.get("identificationModule") or {}).get("nctId")
        if not nct or nct in seen_nct:
            continue
        seen_nct.add(nct)
        sponsor = (((p.get("sponsorCollaboratorsModule") or {})
                    .get("leadSponsor") or {}).get("name"))
        tkr = match_ticker(sponsor, tmap)
        if not tkr:
            log.info("CT.gov %s: no ticker match for '%s' — skipped", nct, sponsor)
            continue
        if noisy(sponsor) or not tradeable(tkr)[0]:
            continue
        pcd = ((p.get("statusModule") or {})
               .get("primaryCompletionDateStruct") or {}).get("date")
        ed, ws, we = parse_pcd(pcd)
        if not ed or not sane_date(ed):
            continue
        title = ((p.get("identificationModule") or {})
                 .get("briefTitle") or "")[:90]
        rows.append({"ticker": tkr, "company": sponsor, "cat": "READOUT",
                     "badge": "PH3",
                     "event": f"Ph3 primary completion — {title}",
                     "event_date": ed.isoformat(),
                     "window_start": ws.isoformat(),
                     "window_end": we.isoformat(),
                     "window_label": "Est. window",
                     "event_class": "ph3_topline",
                     "discovery_score": lex_score(sponsor, title, "phase 3"),
                     "note": f"auto · {nct}",
                     "sources": [f"CT.gov · {nct}"]})

    n = push_events(rows)
    if n is None:
        log.warning("insert failed — CT.gov state NOT advanced; will retry")
        return
    kv_set("discover_state", {**state, "seen_nct": sorted(seen_nct)[-1500:]})
    log.info("discover slow: +%d Ph3 readout windows (CT.gov)", n)

if __name__ == "__main__":
    run_fast()
    run_slow()
