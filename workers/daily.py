"""Daily pass: CT.gov sweep, insiders, balance, settle outcomes, digest —
each stage crash-isolated; crashes ping Discord."""
from .common import log
from . import discover, enrich_insiders, enrich_balance, record_outcomes, notify

def _safe(name, fn):
    try:
        fn()
    except Exception as ex:
        log.exception("%s crashed", name)
        try:
            notify.send(f"⚠️ PRICED IN — {name} crashed: "
                        f"{type(ex).__name__}: {str(ex)[:180]}")
        except Exception:
            pass

def main():
    _safe("discover.slow", discover.run_slow)
    _safe("enrich_insiders", enrich_insiders.run)
    _safe("enrich_balance", enrich_balance.run)
    _safe("record_outcomes", record_outcomes.run)
    _safe("digest", notify.digest)

if __name__ == "__main__":
    main()
