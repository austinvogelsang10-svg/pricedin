"""Options enrichment: for each upcoming event, snapshot the ATM straddle
at the first expiry ON/AFTER the event date and derive:

  implied_move  = straddle mid / spot            (expected move THROUGH expiry)
  spread_pct    = avg leg (ask-bid)/mid          (liquidity — the killer stat)
  open_interest = call OI + put OI

Notes:
- Uses Alpaca's free `indicative` options feed. Enable options on your
  (paper) account in the Alpaca dashboard first.
- Tiny biotechs sometimes have NO listed options. That's recorded as a
  null snapshot and is itself signal (no defined-risk way to trade it).
- implied_move through an expiry a few days past the event slightly
  overstates the event move; fine for v1, refine with term structure later.
"""
import datetime as dt
from .common import (log, sb_upsert, alpaca_get, spot_price, board_events,
                     ALPACA_TRADE, ALPACA_DATA)

def contracts_for(ticker, on_or_after):
    """List option contracts with expiration >= event date (first page is plenty:
    we only need the nearest expiry)."""
    j = alpaca_get(f"{ALPACA_TRADE}/v2/options/contracts", {
        "underlying_symbols": ticker,
        "expiration_date_gte": on_or_after.isoformat(),
        "limit": 500,
        "status": "active",
    })
    return j.get("option_contracts") or []

def latest_quotes(symbols):
    j = alpaca_get(f"{ALPACA_DATA}/v1beta1/options/quotes/latest", {
        "symbols": ",".join(symbols), "feed": "indicative",
    })
    return j.get("quotes") or {}

def mid_and_spread(q):
    bid, ask = float(q.get("bp") or 0), float(q.get("ap") or 0)
    if ask <= 0:
        return None, None
    mid = (bid + ask) / 2 if bid > 0 else ask / 2
    if mid <= 0:
        return None, None
    return mid, (ask - bid) / mid * 100

def enrich_event(e):
    tkr, target = e["ticker"], e["_target_date"]
    spot = spot_price(tkr)
    if not spot:
        log.info("%s: no spot price (sample ticker or no data) — skipping", tkr)
        return None

    try:
        cons = contracts_for(tkr, target)
    except Exception as ex:
        log.warning("%s: contracts lookup failed: %s", tkr, ex)
        return None
    if not cons:
        log.info("%s: no listed options on/after %s", tkr, target)
        return {"event_id": e["id"], "spot": spot}

    # nearest expiry spanning the event
    expiry = min(c["expiration_date"] for c in cons)
    at_exp = [c for c in cons if c["expiration_date"] == expiry]
    strikes = sorted({float(c["strike_price"]) for c in at_exp})
    atm = min(strikes, key=lambda s: abs(s - spot))
    call = next((c for c in at_exp if float(c["strike_price"]) == atm and c["type"] == "call"), None)
    put  = next((c for c in at_exp if float(c["strike_price"]) == atm and c["type"] == "put"), None)
    if not call or not put:
        log.info("%s: incomplete ATM pair at %s %s", tkr, expiry, atm)
        return {"event_id": e["id"], "spot": spot, "expiry": expiry}

    quotes = latest_quotes([call["symbol"], put["symbol"]])
    cq, pq = quotes.get(call["symbol"]), quotes.get(put["symbol"])
    if not cq or not pq:
        log.info("%s: no quotes for ATM pair", tkr)
        return {"event_id": e["id"], "spot": spot, "expiry": expiry,
                "call_symbol": call["symbol"], "put_symbol": put["symbol"]}

    cm, cs = mid_and_spread(cq)
    pm, ps = mid_and_spread(pq)
    if cm is None or pm is None:
        return {"event_id": e["id"], "spot": spot, "expiry": expiry,
                "call_symbol": call["symbol"], "put_symbol": put["symbol"]}

    straddle = round(cm + pm, 2)
    oi = float(call.get("open_interest") or 0) + float(put.get("open_interest") or 0)
    row = {
        "event_id": e["id"],
        "spot": round(spot, 2),
        "expiry": expiry,
        "call_symbol": call["symbol"],
        "put_symbol": put["symbol"],
        "straddle_mid": straddle,
        "implied_move": round(straddle / spot * 100, 1),
        "spread_pct": round(((cs or 0) + (ps or 0)) / 2, 1),
        "open_interest": oi,
    }
    log.info("%s: imp %.1f%% (straddle %.2f / spot %.2f, exp %s, spread %.0f%%, OI %d)",
             tkr, row["implied_move"], straddle, spot, expiry, row["spread_pct"], oi)
    return row

def run():
    rows = []
    for e in board_events():
        snap = enrich_event(e)
        if snap:
            rows.append(snap)
    if rows:
        sb_upsert("option_metrics", rows)
        log.info("wrote %d option snapshots", len(rows))
    else:
        log.info("no option snapshots this run")

if __name__ == "__main__":
    run()
