"""
Nueces County Motivated Seller Lead Scraper v1.0
Primary: nueces.tx.publicsearch.us (Selenium, runs 2x daily)
Owner enrichment: Nueces CAD parcel lookup via esearch.nuecescad.net

Foreclosures only — no VBP/CE for Nueces County.
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
PAGES_RECORDS     = "https://e4property.github.io/nueces-leads/records.json"

LAYERS = [
    {"index": 0, "type": "NOF", "label": "Mortgage Foreclosure"},
    {"index": 1, "type": "TAX", "label": "Tax Foreclosure"},
]

RUN_TIMESTAMP = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
TODAY         = datetime.now(timezone.utc).date()
IS_SUNDAY     = TODAY.weekday() == 6
CUTOFF        = TODAY - timedelta(days=90)

NUECES_CAD_SEARCH = "https://esearch.nuecescad.net/Property/SearchResults"

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
        cutoff_str = CUTOFF.strftime("%m/%d/%Y")
        today_str  = TODAY.strftime("%m/%d/%Y")

        for layer in LAYERS:
            doc_type = "FC" if layer["type"] == "NOF" else "TAX"
            log.info(f"Scraping {layer['label']} (department=FC, type={doc_type})...")

            offset = 0
            consecutive_empty = 0
            chunk_new = 0

            while True:
                url = (
                    f"{PUBLICSEARCH_BASE}/results"
                    f"?department=FC"
                    f"&docTypes=%5B%22{doc_type}%22%5D"
                    f"&dateRange=%5B%22{urllib.parse.quote(cutoff_str)}%22%2C%22{urllib.parse.quote(today_str)}%22%5D"
                    f"&offset={offset}"
                )
                try:
                    driver.get(url)
                    WebDriverWait(driver, 30).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, ".result-row, .no-results, [class*='result']"))
                    )
                    time.sleep(1.5)
                except Exception:
                    time.sleep(3)
                    try:
                        driver.get(url)
                        time.sleep(3)
                    except Exception:
                        break

                page_source = driver.page_source
                rows = re.findall(
                    r'data-docid="([^"]+)"[^>]*>.*?'
                    r'(?:class="[^"]*party[^"]*"[^>]*>([^<]*)</|'
                    r'<td[^>]*>([^<]*(?:LLC|TRUST|CORP|INC|&amp;[^<]*)?[^<]*)</td>)',
                    page_source, re.DOTALL
                )

                # Simpler extraction using regex on the page
                doc_pattern = re.compile(
                    r'"documentNumber"\s*:\s*"([^"]+)".*?'
                    r'"grantor(?:Name)?"\s*:\s*"([^"]*)".*?'
                    r'"grantee(?:Name)?"\s*:\s*"([^"]*)".*?'
                    r'"recordDate"\s*:\s*"([^"]*)"',
                    re.DOTALL
                )

                # Try JSON extraction from page data
                json_match = re.search(r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\});', page_source, re.DOTALL)
                page_records = []

                if json_match:
                    try:
                        state = json.loads(json_match.group(1))
                        results = (
                            state.get("search", {}).get("results", []) or
                            state.get("results", {}).get("data", []) or
                            []
                        )
                        for r in results:
                            doc_num = r.get("documentNumber") or r.get("docNumber") or ""
                            if not doc_num:
                                continue
                            page_records.append({
                                "doc_number":  doc_num,
                                "owner":       r.get("grantorName") or r.get("grantor") or "",
                                "date_filed":  (r.get("recordDate") or "")[:10],
                                "address":     r.get("propertyAddress") or r.get("address") or "",
                                "city":        "CORPUS CHRISTI",
                                "zip":         r.get("zip") or "",
                                "type":        layer["type"],
                                "source":      "publicsearch",
                                "county":      "nueces",
                            })
                    except Exception as e:
                        log.debug(f"JSON parse error: {e}")

                # Fallback: scrape table rows
                if not page_records:
                    rows_html = re.findall(r'<tr[^>]*class="[^"]*result[^"]*"[^>]*>(.*?)</tr>', page_source, re.DOTALL)
                    for row_html in rows_html:
                        cells = re.findall(r'<td[^>]*>(.*?)</td>', row_html, re.DOTALL)
                        cells = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]
                        if len(cells) >= 3:
                            doc_num = cells[0].strip()
                            if doc_num and doc_num not in known_docs:
                                page_records.append({
                                    "doc_number":  doc_num,
                                    "owner":       cells[2] if len(cells) > 2 else "",
                                    "date_filed":  cells[1] if len(cells) > 1 else "",
                                    "address":     cells[3] if len(cells) > 3 else "",
                                    "city":        "CORPUS CHRISTI",
                                    "zip":         "",
                                    "type":        layer["type"],
                                    "source":      "publicsearch",
                                    "county":      "nueces",
                                })

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
                            "sale_date":       "",
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
                        new_records.append(rec)
                        page_new += 1
                        chunk_new += 1

                log.info(f"  [{layer['type']}] offset={offset} | {page_new} new | {len(page_records)} on page")

                if page_new == 0:
                    consecutive_empty += 1
                else:
                    consecutive_empty = 0

                if consecutive_empty >= 2 or len(page_records) == 0:
                    log.info(f"  [{layer['type']}] stopping — {chunk_new} new total")
                    break

                offset += 50
                time.sleep(1)

    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

    log.info(f"PublicSearch: {len(new_records)} new Nueces records")
    return new_records


# ── LOAD / SAVE RECORDS ────────────────────────────────────────────────────────
def load_known_docs():
    try:
        existing = json.loads(RECORDS_PATH.read_text())
        log.info(f"Loaded {len(existing)} existing records from {RECORDS_PATH}")
        return {r["doc_number"] for r in existing if r.get("doc_number")}, existing
    except Exception:
        log.info("No existing records — starting fresh")
        return set(), []


def write_records(records):
    RECORDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(records)
    json.loads(text)  # validate
    tmp = RECORDS_PATH.with_suffix(".tmp")
    tmp.write_text(text)
    tmp.replace(RECORDS_PATH)


# ── SCORE ──────────────────────────────────────────────────────────────────────
def score_record(r):
    s = 5
    if r.get("absentee"):    s += 2
    if r.get("type") == "TAX": s += 1
    return min(s, 10)


# ── FILTER ────────────────────────────────────────────────────────────────────
def should_keep(r):
    df = r.get("date_filed", "")
    if not df:
        return True
    try:
        parts = df.split("-")
        if len(parts) == 3:
            d = datetime(int(parts[0]), int(parts[1]), int(parts[2])).date()
            return d >= CUTOFF
    except Exception:
        pass
    return True


# ── MAIN ───────────────────────────────────────────────────────────────────────
def main():
    log.info("=" * 60)
    log.info("Nueces County Lead Scraper v1.0")
    log.info(f"Run: {RUN_TIMESTAMP} | Cutoff: {CUTOFF} | Sunday: {IS_SUNDAY}")
    log.info("=" * 60)

    known_docs, prev_records = load_known_docs()

    # Scrape PublicSearch
    new_records = scrape_publicsearch(known_docs)

    # Mark prev as not new
    for r in prev_records:
        r["is_new"] = False

    # Merge
    prev_by_doc = {r["doc_number"]: r for r in prev_records if r.get("doc_number")}
    seen = {}
    for r in new_records + prev_records:
        doc = r.get("doc_number", "")
        if doc and doc not in seen:
            seen[doc] = r
    records = list(seen.values())
    log.info(f"After merge: {len(records)} total records")

    # Filter
    before  = len(records)
    records = [r for r in records if should_keep(r)]
    log.info(f"After filter: {len(records)} kept, {before - len(records)} dropped")

    # Score
    for r in records:
        r["score"] = score_record(r)

    # Stats
    new_ct = sum(1 for r in records if r.get("is_new"))
    nof_ct = sum(1 for r in records if r.get("type") == "NOF")
    tax_ct = sum(1 for r in records if r.get("type") == "TAX")
    log.info(f"Final: {len(records)} total | {new_ct} new | {nof_ct} NOF | {tax_ct} TAX")

    write_records(records)
    log.info(f"Dashboard: {len(records)} records, {RECORDS_PATH.stat().st_size:,} bytes")
    log.info("Done.")


if __name__ == "__main__":
    main()
