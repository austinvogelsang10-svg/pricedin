"""Outcomes recorder — the compounding asset.

Once an event's date (or window end) is 2+ days past, compute the realized
move from daily closes:

  realized_move = |close(first session after event) - close(last session before)|
                  / close(before) * 100

and file it under the event_class. The dashboard prefers YOUR measured class
history over the seeded priors once a class has n >= 8 outcomes. Every day
this runs, the product gets harder to clone.
"""
import datetime as dt
from .common import log, sb_select, sb_upsert, sb_update, daily_closes, TODAY

def realized_move(ticker, event_date):
    start = event_date - dt.timedelta(days=10)
    end   = event_date + dt.timedelta(days=7)
    closes = daily_closes(ticker, start.isoformat(), end.isoformat())
    if not closes:
        return None
    days = sorted(closes)
    before = [d for d in days if d < event_date.isoformat()]
    after  = [d for d in days if d > event_date.isoformat()]
    if not before or not after:
        return None
    cb, ca = closes[before[-1]], closes[after[0]]
    if not cb:
        return None
    return cb, ca, round(abs(ca - cb) / cb * 100, 1)

def run():
    rows = sb_select("events", {
        "select": "id,ticker,event_date,window_end,event_class,is_sample",
        "status": "eq.upcoming",
    })
    outcomes = []
    for e in rows:
        d = e.get("window_end") or e.get("event_date")
        if not d:
            continue
        edate = dt.date.fromisoformat(d)
        if edate > TODAY - dt.timedelta(days=2):
            continue  # not settled yet
        if e.get("is_sample"):
            sb_update("events", {"id": f"eq.{e['id']}"}, {"status": "passed"})
            continue
        rm = realized_move(e["ticker"], dt.date.fromisoformat(e.get("event_date") or d))
        if rm:
            cb, ca, move = rm
            outcomes.append({
                "ticker": e["ticker"], "event_class": e.get("event_class"),
                "event_date": e.get("event_date") or d,
                "close_before": cb, "close_after": ca, "realized_move": move,
            })
            log.info("%s %s: realized %.1f%% (%.2f -> %.2f)",
                     e["ticker"], e.get("event_class"), move, cb, ca)
        sb_update("events", {"id": f"eq.{e['id']}"}, {"status": "passed"})
    if outcomes:
        sb_upsert("event_outcomes", outcomes)
        log.info("library +%d outcomes", len(outcomes))
    else:
        log.info("no events to settle")

if __name__ == "__main__":
    run()
