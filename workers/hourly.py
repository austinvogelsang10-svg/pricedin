"""Hourly pass: refresh options pricing, then evaluate alert rules."""
from . import enrich_options, notify

def main():
    enrich_options.run()
    notify.run()

if __name__ == "__main__":
    main()
