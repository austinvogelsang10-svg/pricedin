"""Hourly pass: discover, price, alert — each stage crash-isolated so one
failing source never kills the whole pass. Crashes ping Discord."""
from .common import log
from . import discover, enrich_options, notify

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
    _safe("discover.fast", discover.run_fast)
    _safe("enrich_options", enrich_options.run)
    _safe("notify", notify.run)

if __name__ == "__main__":
    main()
