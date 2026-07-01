"""Alerts v1 — Discord webhook, whole-board rules.

Fires on:
  - NEW      event enters the board
  - SHIFT    implied move changed >10% (relative) since last alerted level
  - CLUSTER  3+ open-market insider buys within the 90d window
  - T-3      event is three days out

Per-user channels/rules come later with Supabase auth; this gets a real
alert loop running today with zero extra infrastructure. If DISCORD_WEBHOOK
is unset, alerts are logged instead of sent (dry run).
"""
import datetime as dt
import requests
from .common import log, sb_select, kv_get, kv_set, DISCORD_WEBHOOK, TODAY

def send(msg):
    if not DISCORD_WEBHOOK:
        log.info("[dry-run alert] %s", msg)
        return
    try:
        requests.post(DISCORD_WEBHOOK, json={"content": msg}, timeout=15)
    except Exception as ex:
        log.warning("discord send failed: %s", ex)

def run():
    board = sb_select("event_board", {"select": "*"})
    state = kv_get("notify_state", {}) or {}
    known   = set(state.get("known", []))
    implied = state.get("implied", {})     # event_id -> last alerted implied
    flagged = set(state.get("clusters", []))
    t3done  = set(state.get("t3", []))

    for e in board:
        eid = e["id"]
        tag = f"**{e['ticker']}** {e.get('badge') or ''} · {e.get('event_date') or 'window'}"

        if eid not in known:
            send(f"🆕 NEW — {tag}\n{e['event']}")
            known.add(eid)

        imp = e.get("implied_move")
        if imp:
            last = implied.get(eid)
            if last and last > 0 and abs(imp - last) / last > 0.10:
                arrow = "▲" if imp > last else "▼"
                send(f"{arrow} SHIFT — {tag}\nImplied move {last:.0f}% → {imp:.0f}% "
                     f"(hist {e.get('hist_move') or '?'}%)")
                implied[eid] = imp
            elif last is None:
                implied[eid] = imp

        if (e.get("buys_90d") or 0) >= 3 and eid not in flagged:
            send(f"👥 INSIDER CLUSTER — {tag}\n{e['buys_90d']} open-market buys in 90d")
            flagged.add(eid)

        ed = e.get("event_date")
        if ed and eid not in t3done:
            if dt.date.fromisoformat(ed) - TODAY == dt.timedelta(days=3):
                send(f"⏳ T-3 — {tag}\n{e['event']}\n"
                     f"imp {e.get('implied_move') or '?'}% vs hist {e.get('hist_move') or '?'}%")
                t3done.add(eid)

    kv_set("notify_state", {
        "known": sorted(known), "implied": implied,
        "clusters": sorted(flagged), "t3": sorted(t3done),
    })
    log.info("notify pass complete (%d events on board)", len(board))

if __name__ == "__main__":
    run()
