"""Daily pass: insider tape, balance sheet flags, settle passed events."""
from . import enrich_insiders, enrich_balance, record_outcomes

def main():
    enrich_insiders.run()
    enrich_balance.run()
    record_outcomes.run()

if __name__ == "__main__":
    main()
