"""Balance sheet / dilution flags:

  runway_q     ≈ cash / quarterly operating burn   (XBRL company-concept API)
  shelf_active = S-3 / S-3ASR / F-3 filed in the last 3 years (crude but useful:
                 flags the *ability* to sell stock into strength; it does not
                 confirm remaining capacity or effectiveness)
"""
import datetime as dt
from .common import log, sb_upsert, sec_get, cik_for, submissions, board_events, TODAY

CASH_TAGS = [
    "CashAndCashEquivalentsAtCarryingValue",
    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
]
BURN_TAG = "NetCashProvidedByUsedInOperatingActivities"
SHELF_FORMS = {"S-3", "S-3/A", "S-3ASR", "F-3", "F-3/A", "F-3ASR"}

def concept(cik, tag):
    url = f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/us-gaap/{tag}.json"
    return sec_get(url)

def latest_cash(cik):
    for tag in CASH_TAGS:
        try:
            j = concept(cik, tag)
        except Exception:
            continue
        pts = (j.get("units") or {}).get("USD") or []
        pts = [p for p in pts if p.get("end")]
        if pts:
            pts.sort(key=lambda p: p["end"])
            return float(pts[-1]["val"]), pts[-1]["end"]
    return None, None

def quarterly_burn(cik):
    """Most recent ~quarterly operating cash flow; negative -> burn."""
    try:
        j = concept(cik, BURN_TAG)
    except Exception:
        return None
    pts = (j.get("units") or {}).get("USD") or []
    best = None
    for p in pts:
        s, e = p.get("start"), p.get("end")
        if not s or not e:
            continue
        days = (dt.date.fromisoformat(e) - dt.date.fromisoformat(s)).days
        cand = None
        if 80 <= days <= 100:
            cand = float(p["val"])
        elif 350 <= days <= 380:
            cand = float(p["val"]) / 4.0
        if cand is None:
            continue
        if best is None or e > best[0]:
            best = (e, cand)
    if best is None:
        return None
    ocf = best[1]
    return -ocf if ocf < 0 else 0.0   # burn only if cash flow negative

def shelf_status(sub):
    rec = sub.get("filings", {}).get("recent", {})
    cutoff = TODAY - dt.timedelta(days=3 * 365)
    for form, fdate in zip(rec.get("form", []), rec.get("filingDate", [])):
        if form in SHELF_FORMS and dt.date.fromisoformat(fdate) >= cutoff:
            return True, f"{form} filed {fdate}"
    return False, "No recent shelf on file"

def run():
    rows = []
    done = set()
    for e in board_events():
        tkr = e["ticker"].upper()
        cik = cik_for(tkr)
        if not cik:
            log.info("%s: no CIK — skipping balance", tkr)
            continue
        if tkr not in done:
            done.add(tkr)
        cash, _ = latest_cash(cik)
        burn = quarterly_burn(cik)
        runway = round(cash / burn, 1) if (cash and burn and burn > 0) else None
        try:
            active, note = shelf_status(submissions(cik))
        except Exception as ex:
            log.warning("%s: shelf check failed: %s", tkr, ex)
            active, note = None, None
        rows.append({
            "event_id": e["id"],
            "cash": cash, "quarterly_burn": burn, "runway_q": runway,
            "shelf_active": active, "shelf_note": note,
        })
        log.info("%s: cash=%s burn=%s runway=%sq shelf=%s",
                 tkr, cash, burn, runway, note)
    if rows:
        sb_upsert("balance_flags", rows)
        log.info("wrote %d balance flags", len(rows))

if __name__ == "__main__":
    run()
