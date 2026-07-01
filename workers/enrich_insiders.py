"""Insider tape: for each board ticker, pull recent Form 4 filings from EDGAR,
parse the XML, and record open-market purchases (code P) and sales (code S)
from the last 90 days. Cluster buying ahead of a binary event is one of the
few academically persistent signals — we surface it, we don't trade it.
"""
import datetime as dt
import xml.etree.ElementTree as ET
from .common import log, sb_upsert, sec_get, cik_for, submissions, board_events, TODAY

LOOKBACK = 90

def _txt(node, path):
    el = node.find(path)
    return el.text.strip() if el is not None and el.text else None

def parse_form4(xml_text):
    """Return list of {owner, role, code, shares, price, value} from one Form 4."""
    out = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out

    owner = _txt(root, ".//reportingOwner/reportingOwnerId/rptOwnerName") or "Unknown"
    rel = root.find(".//reportingOwner/reportingOwnerRelationship")
    role = "Insider"
    if rel is not None:
        title = _txt(rel, "officerTitle")
        if title:
            role = title
        elif _txt(rel, "isDirector") in ("1", "true"):
            role = "Director"
        elif _txt(rel, "isTenPercentOwner") in ("1", "true"):
            role = "10% owner"

    for tx in root.findall(".//nonDerivativeTable/nonDerivativeTransaction"):
        code = _txt(tx, "transactionCoding/transactionCode")
        if code not in ("P", "S"):
            continue
        shares = _txt(tx, "transactionAmounts/transactionShares/value")
        price  = _txt(tx, "transactionAmounts/transactionPricePerShare/value")
        try:
            shares_f = float(shares) if shares else None
            price_f  = float(price) if price else None
        except ValueError:
            shares_f = price_f = None
        value = round(shares_f * price_f) if shares_f and price_f else None
        out.append({"owner": owner, "role": role, "code": code,
                    "shares": shares_f, "price": price_f, "value": value})
    return out

def form4_xml_url(cik, accession, primary_doc):
    acc = accession.replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}"
    # primary_doc is sometimes the styled 'xslF345X.../foo.xml'; strip to raw
    doc = primary_doc.split("/")[-1]
    return f"{base}/{doc}"

def run():
    events = board_events()
    seen_ticker_event = {}
    for e in events:
        seen_ticker_event.setdefault(e["ticker"].upper(), []).append(e["id"])

    rows = []
    for ticker, event_ids in seen_ticker_event.items():
        cik = cik_for(ticker)
        if not cik:
            log.info("%s: no CIK (sample ticker?) — skipping insiders", ticker)
            continue
        try:
            sub = submissions(cik)
        except Exception as ex:
            log.warning("%s: submissions failed: %s", ticker, ex)
            continue

        rec = sub.get("filings", {}).get("recent", {})
        forms = rec.get("form", [])
        dates = rec.get("filingDate", [])
        accs  = rec.get("accessionNumber", [])
        docs  = rec.get("primaryDocument", [])

        cutoff = TODAY - dt.timedelta(days=LOOKBACK)
        for form, fdate, acc, doc in zip(forms, dates, accs, docs):
            if form != "4":
                continue
            fd = dt.date.fromisoformat(fdate)
            if fd < cutoff:
                continue
            try:
                xml_text = sec_get(form4_xml_url(cik, acc, doc), is_json=False)
            except Exception as ex:
                log.warning("%s: form4 fetch failed (%s): %s", ticker, acc, ex)
                continue
            for tx in parse_form4(xml_text):
                for eid in event_ids:
                    rows.append({"event_id": eid, "ticker": ticker,
                                 "filed": fdate, **tx})
        log.info("%s: insider scan complete", ticker)

    if rows:
        sb_upsert("insider_txns", rows)
        log.info("wrote %d insider transactions", len(rows))
    else:
        log.info("no insider transactions found")

if __name__ == "__main__":
    run()
