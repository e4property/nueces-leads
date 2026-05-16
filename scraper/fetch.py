"""
Nueces County Motivated Seller Lead Scraper v1.1
Primary: nueces.tx.publicsearch.us (Selenium, runs 2x daily)
Foreclosures only — no VBP/CE for Nueces County.

v1.1 fixes:
  - Correct URL format: instrumentDateRange instead of dateRange
  - No docTypes filter — department=FC returns all foreclosure notices
  - 21-day window for fresh leads
  - Correct HTML extraction for Nueces PublicSearch structure
"""

import json
import logging
import os
import re
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

PUBLICSEARCH_BASE = "https://nueces.tx.publicsearch.us"
RECORDS_PATH      = Path("dashboard/records.json")

RUN_TIMESTAMP = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
TODAY         = datetime.now(timezone.utc).date()
CUTOFF        = TODAY - timedelta(days=21)

log.info(f"Date range: {CUTOFF.strftime('%Y%m%d')} to {TODAY.strftime('%Y%m%d')}")

# ── SELENIUM ──────────────────────────────────────────────────────────────────
def get_driver():
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    opts = Options()
    opts.add_argument("--headless")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1280,800")
    opts.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
    try:
        return webdriver.Chrome(options=opts)
    except Exception:
        svc = Service("/usr/bin/chromedriver")
        return webdriver.Chrome(service=svc, options=opts)


# ── PUBLICSEARCH SCRAPER ──────────────────────────────────────────────────────
def scrape_publicsearch(known_docs):
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    new_records = []
    driver = None

    try:
        driver = get_driver()

        cutoff_str = CUTOFF.strftime("%Y%m%d")
        today_str  = TODAY.strftime("%Y%m%d")

        # URL format from Nueces PublicSearch — department=FC, instrumentDateRange
        offset = 0
        consecutive_empty = 0
        total_new = 0

        log.info(f"Scraping Nueces Foreclosures (department=FC)...")
        log.info(f"Date range: {cutoff_str} to {today_str}")

        while True:
            url = (
                f"{PUBLICSEARCH_BASE}/results"
                f"?department=FC"
                f"&instrumentDateRange={cutoff_str}%2C{today_str}"
                f"&keywordSearch=false"
                f"&offset={offset}"
            )

            log.info(f"  Fetching offset={offset}: {url}")

            try:
                driver.get(url)
                # Wait for results table or no-results message
                WebDriverWait(driver, 30).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR,
                        "table, .results-table, [class*='result'], .no-results, h2"))
                )
                time.sleep(2)
            except Exception as e:
                log.warning(f"  Page load timeout at offset {offset}: {e}")
                break

            page_source = driver.page_source

            # Log page title for debugging
            title_match = re.search(r'<title[^>]*>(.*?)</title>', page_source, re.DOTALL)
            if title_match:
                log.info(f"  Page title: {title_match.group(1).strip()[:80]}")

            # Check for no results
            if re.search(r'no results|0 results|no records found', page_source, re.IGNORECASE):
                log.info(f"  No results page detected — stopping")
                break

            # Extract records from page
            page_records = []

            # Try to find result count
            count_match = re.search(r'(\d+(?:,\d+)?)\s*(?:of\s*\d+\s*)?results', page_source, re.IGNORECASE)
            if count_match:
                log.info(f"  Results indicator: {count_match.group(0)}")

            # Extract rows from the results table
            # Nueces PublicSearch shows: DOC TYPE | RECORDED DATE | SALE DATE | DOC NUMBER | PROPERTY ADDRESS
            rows = re.findall(
                r'<tr[^>]*>(.*?)</tr>',
                page_source, re.DOTALL | re.IGNORECASE
            )

            for row in rows:
                # Skip header rows
                if re.search(r'<th|thead|DOC TYPE|RECORDED DATE', row, re.IGNORECASE):
                    continue

                cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL | re.IGNORECASE)
                cells = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]
                cells = [c for c in cells if c]  # remove empty

                if len(cells) < 3:
                    continue

                # Try to find doc number — usually a numeric string
                doc_num = ""
                date_filed = ""
                sale_date = ""
                address = ""
                doc_type = ""

                for cell in cells:
                    cell_clean = cell.strip()
                    # Doc number pattern: year + sequential (e.g. 2026000297)
                    if re.match(r'^\d{10}$', cell_clean) and not doc_num:
                        doc_num = cell_clean
                    # Date pattern MM/DD/YYYY
                    elif re.match(r'^\d{1,2}/\d{1,2}/\d{4}$', cell_clean):
                        if not date_filed:
                            date_filed = cell_clean
                        elif not sale_date:
                            sale_date = cell_clean
                    # Doc type
                    elif 'FORECLOSURE' in cell_clean.upper() and not doc_type:
                        doc_type = cell_clean
                    # Address — contains numbers and street words
                    elif re.search(r'\d+\s+[A-Z]', cell_clean) and len(cell_clean) > 5 and not address:
                        address = cell_clean
                    elif cell_clean.upper() not in ('N/A', '', 'DOC TYPE', 'RECORDED DATE', 'SALE DATE', 'DOC NUMBER', 'PROPERTY ADDRESS'):
                        if len(cell_clean) > 8 and not address and re.search(r'[A-Z]{2,}', cell_clean):
                            address = cell_clean

                if doc_num and doc_num not in known_docs:
                    page_records.append({
                        "doc_number":  doc_num,
                        "type":        "NOF",
                        "source":      "publicsearch",
                        "county":      "nueces",
                        "address":     address or "N/A",
                        "city":        "CORPUS CHRISTI",
                        "zip":         "",
                        "owner":       "",
                        "date_filed":  date_filed,
                        "sale_date":   sale_date,
                        "doc_type":    doc_type or "FORECLOSURE NOTICE",
                    })

            # Also try JSON extraction from page state
            json_match = re.search(
                r'window\.__NEXT_DATA__\s*=\s*(\{.*?\})\s*;?\s*</script>',
                page_source, re.DOTALL
            )
            if json_match and not page_records:
                try:
                    state = json.loads(json_match.group(1))
                    # Navigate to results
                    props = state.get("props", {})
                    page_props = props.get("pageProps", {})
                    results = (
                        page_props.get("results", []) or
                        page_props.get("data", {}).get("results", []) or
                        []
                    )
                    for r in results:
                        doc_num = str(r.get("documentNumber") or r.get("docNumber") or "")
                        if doc_num and doc_num not in known_docs:
                            page_records.append({
                                "doc_number":  doc_num,
                                "type":        "NOF",
                                "source":      "publicsearch",
                                "county":      "nueces",
                                "address":     r.get("propertyAddress") or "N/A",
                                "city":        "CORPUS CHRISTI",
                                "zip":         r.get("zip") or "",
                                "owner":       r.get("grantorName") or r.get("grantor") or "",
                                "date_filed":  (r.get("recordDate") or r.get("instrumentDate") or "")[:10],
                                "sale_date":   r.get("saleDate") or "",
                                "doc_type":    r.get("docType") or "FORECLOSURE NOTICE",
                            })
                    if page_records:
                        log.info(f"  Extracted {len(page_records)} records from JSON state")
                except Exception as e:
                    log.debug(f"  JSON state parse error: {e}")

            log.info(f"  offset={offset} | {len(page_records)} on page")

            # Add new records
            page_new = 0
            for rec in page_records:
                doc = rec["doc_number"]
                if doc and doc not in known_docs:
                    known_docs.add(doc)
                    rec.update({
                        "is_new":          True,
                        "run_ts":          RUN_TIMESTAMP,
                        "score":           5,
                        "flags":           [],
                        "absentee":        False,
                        "duplicate":       False,
                        "days_until_sale": None,
                        "loan_amount":     "",
                        "loan_date":       "",
                        "lender":          "",
                        "trustee":         "",
                        "appraised_value": "",
                        "annual_taxes":    "",
                        "mail_addr":       "",
                        "ps_doc_id":       "",
                    })
                    # Calculate days until sale
                    if rec.get("sale_date"):
                        try:
                            parts = rec["sale_date"].split("/")
                            if len(parts) == 3:
                                sd = datetime(int(parts[2]), int(parts[0]), int(parts[1])).date()
                                rec["days_until_sale"] = (sd - TODAY).days
                                if rec["days_until_sale"] <= 14:
                                    rec["flags"].append("URGENT")
                                elif rec["days_until_sale"] <= 30:
                                    rec["flags"].append("AUCTION SOON")
                        except Exception:
                            pass
                    new_records.append(rec)
                    page_new += 1
                    total_new += 1

            log.info(f"  offset={offset} | {page_new} new | {total_new} total new so far")

            if len(page_records) == 0:
                consecutive_empty += 1
            else:
                consecutive_empty = 0

            if consecutive_empty >= 2:
                log.info(f"  2 consecutive empty pages — stopping")
                break

            if len(page_records) < 50:
                log.info(f"  Last page (fewer than 50 results) — stopping")
                break

            offset += 50
            time.sleep(1.5)

    except Exception as e:
        log.error(f"Scraper error: {e}")
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

    log.info(f"PublicSearch: {len(new_records)} new Nueces records")
    return new_records


# ── LOAD / SAVE ───────────────────────────────────────────────────────────────
def load_known_docs():
    try:
        existing = json.loads(RECORDS_PATH.read_text())
        log.info(f"Loaded {len(existing)} existing records")
        return {r["doc_number"] for r in existing if r.get("doc_number")}, existing
    except Exception:
        log.info("No existing records — starting fresh")
        return set(), []


def write_records(records):
    RECORDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(records)
    json.loads(text)
    tmp = RECORDS_PATH.with_suffix(".tmp")
    tmp.write_text(text)
    tmp.replace(RECORDS_PATH)


# ── SCORE ─────────────────────────────────────────────────────────────────────
def score_record(r):
    s = 5
    if r.get("absentee"):      s += 2
    if r.get("type") == "TAX": s += 1
    if r.get("days_until_sale") is not None and r["days_until_sale"] <= 14: s += 2
    elif r.get("days_until_sale") is not None and r["days_until_sale"] <= 30: s += 1
    return min(s, 10)


# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    log.info("=" * 60)
    log.info("Nueces County Lead Scraper v1.1")
    log.info(f"Run: {RUN_TIMESTAMP} | Cutoff: {CUTOFF} (21 days)")
    log.info("=" * 60)

    known_docs, prev_records = load_known_docs()
    new_records = scrape_publicsearch(known_docs)

    for r in prev_records:
        r["is_new"] = False

    seen = {}
    for r in new_records + prev_records:
        doc = r.get("doc_number", "")
        if doc and doc not in seen:
            seen[doc] = r
    records = list(seen.values())
    log.info(f"After merge: {len(records)} total records")

    for r in records:
        r["score"] = score_record(r)

    new_ct = sum(1 for r in records if r.get("is_new"))
    nof_ct = sum(1 for r in records if r.get("type") == "NOF")
    urgent = sum(1 for r in records if "URGENT" in r.get("flags", []))
    log.info(f"Final: {len(records)} total | {new_ct} new | {nof_ct} NOF | {urgent} URGENT")

    write_records(records)
    log.info(f"Dashboard: {len(records)} records, {RECORDS_PATH.stat().st_size:,} bytes")
    log.info("Done.")


if __name__ == "__main__":
    main()
