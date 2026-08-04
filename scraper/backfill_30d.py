"""
Nueces Foreclosure Backfill — re-scrape last N days of FC filings
==================================================================
One-time (or re-runnable) fix-up for FC/NOF and FC/TAX leads filed in
the last N days (default 30). Re-pulls those docs from PublicSearch
IGNORING the known-docs cache, re-parses them with the fixed address
(roll signature match + Selenium doc-page fallback) and sale-date
extraction, and merges the results into dashboard/records.json.

Merge behavior (via fetch.py's dedup()):
  - Existing leads: only BLANK fields get filled (address, sale_date,
    owner, etc.) — never overwrites anything already there, and never
    touches ghl_pushed / dash_phone / dash_notes / dash_dispo.
  - Docs that were never captured at all in the window get added as
    new records.

Must live in the same folder as fetch.py (scraper/), since it imports
from it directly rather than duplicating scrape/enrich logic.

Usage:
  python scraper/backfill_30d.py              # default 30-day lookback
  python scraper/backfill_30d.py --days 45     # custom lookback window
"""

import argparse
import json
import logging

from fetch import (
    RECORDS_PATH,
    TODAY,
    get_driver,
    scrape_publicsearch,
    enrich_from_lookup,
    fetch_doc_address,
    load_lookup,
    dedup,
    score_record,
    days_until_sale,
    is_coastal,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# Higher cap than a normal run since this is a deliberate one-off catch-up
MAX_DOC_FETCH_BACKFILL = 150


def main():
    parser = argparse.ArgumentParser(description="Backfill recent FC leads with fixed address/sale-date parsing")
    parser.add_argument("--days", type=int, default=30, help="Lookback window in days (default 30)")
    args = parser.parse_args()

    run_ts = TODAY.isoformat()
    log.info("=" * 60)
    log.info(f"Nueces FC Backfill — last {args.days} days")
    log.info(f"Run: {run_ts}")
    log.info("=" * 60)

    existing = []
    if RECORDS_PATH.exists():
        try:
            existing = json.loads(RECORDS_PATH.read_text(encoding="utf-8"))
            log.info(f"Loaded {len(existing)} existing records")
        except Exception as e:
            log.warning(f"Could not load records: {e}")

    # Clear stale is_new flags before merging — matches fetch.py's main() behavior.
    # Without this, leads flagged new by an earlier run keep showing as new forever.
    for rec in existing:
        rec["is_new"] = False

    lookup = load_lookup()

    log.info("Starting WebDriver...")
    driver = get_driver()
    all_scraped = []

    try:
        # Empty known_docs on purpose — nothing in the window gets skipped,
        # so this catches both mis-parsed AND never-captured docs.
        nof_recs = scrape_publicsearch(
            department="FC", search_term="", lead_type="NOF",
            known_docs=set(), driver=driver, run_ts=run_ts, days=args.days,
        )
        all_scraped.extend(nof_recs)

        tax_recs = scrape_publicsearch(
            department="FC", search_term="TAX", lead_type="TAX",
            known_docs=set(), driver=driver, run_ts=run_ts, days=args.days,
        )
        all_scraped.extend(tax_recs)

        log.info(f"Re-pulled {len(all_scraped)} FC docs from the last {args.days} days")

        # Roll enrichment (address, sale-date is already parsed in scrape_publicsearch)
        filled = 0
        for rec in all_scraped:
            before_addr = rec.get("address", "")
            rec = enrich_from_lookup(rec, lookup)
            if rec.get("address") and not before_addr:
                filled += 1
        log.info(f"Roll enrichment: {filled}/{len(all_scraped)} addresses filled")

        # Selenium fallback for anything still missing an address
        still_missing = [r for r in all_scraped if not r.get("address") and r.get("ps_doc_id")]
        log.info(f"Selenium fallback: {len(still_missing)} still missing address")
        fetched = 0
        for rec in still_missing[:MAX_DOC_FETCH_BACKFILL]:
            street, city, zipc = fetch_doc_address(driver, rec["ps_doc_id"])
            if street:
                rec["address"] = street.upper()
                if city:
                    rec["city"] = city
                if zipc:
                    rec["zip"] = zipc
                rec["is_coastal"] = is_coastal(rec.get("zip", ""))
                fetched += 1
        skipped = max(0, len(still_missing) - MAX_DOC_FETCH_BACKFILL)
        log.info(
            f"Selenium fallback: {fetched}/{min(len(still_missing), MAX_DOC_FETCH_BACKFILL)} recovered"
            + (f" ({skipped} deferred — cap hit, re-run to continue)" if skipped else "")
        )

    finally:
        try:
            driver.quit()
        except Exception:
            pass

    # Score / tag
    for rec in all_scraped:
        rec["score"] = score_record(rec)
        rec["days_until_sale"] = days_until_sale(rec.get("sale_date", ""))
        rec["ghl_tag"] = "nueces_lead"

    # Merge — fills blanks on existing docs, adds anything never captured
    before_total = len(existing)
    merged = dedup(existing, all_scraped)
    log.info(f"Merge: {before_total} existing → {len(merged)} total after backfill")

    RECORDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RECORDS_PATH.write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")
    size_kb = RECORDS_PATH.stat().st_size / 1024
    log.info(f"Saved: {len(merged)} records, {size_kb:.0f} KB")
    log.info("Backfill done.")


if __name__ == "__main__":
    main()
