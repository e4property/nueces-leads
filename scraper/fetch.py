"""
Nueces County Motivated Seller Lead Scraper v1.3
v1.3: Add doc detail fetch for owner name, address, sale date
      Fetches nueces.tx.publicsearch.us/results/{doc_number}
      for each new record to pull grantor, property address
"""

import json, logging, re, time, urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger(__name__)

PUBLICSEARCH_BASE = "https://nueces.tx.publicsearch.us"
RECORDS_PATH      = Path("dashboard/records.json")
RUN_TIMESTAMP     = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
TODAY             = datetime.now(timezone.utc).date()
CUTOFF            = TODAY - timedelta(days=21)
DOC_FETCH_LIMIT   = 50  # max detail pages to fetch per run

def get_driver():
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    opts = Options()
    for a in ["--headless","--no-sandbox","--disable-dev-shm-usage","--disable-gpu","--window-size=1280,800"]:
        opts.add_argument(a)
    opts.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
    try:
        return webdriver.Chrome(options=opts)
    except Exception:
        return webdriver.Chrome(service=Service("/usr/bin/chromedriver"), options=opts)

def parse_date(s):
    try:
        p = s.strip().split("/")
        if len(p) == 3 and len(p[2]) == 4:
            return datetime(int(p[2]), int(p[0]), int(p[1])).date()
        p2 = s.strip().split("-")
        if len(p2) == 3 and len(p2[0]) == 4:
            return datetime(int(p2[0]), int(p2[1]), int(p2[2])).date()
    except Exception:
        pass
    return None

def scrape_publicsearch(known_docs):
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    new_records = []
    driver = None
    cutoff_str = CUTOFF.strftime("%Y%m%d")
    today_str  = TODAY.strftime("%Y%m%d")

    try:
        driver = get_driver()
        offset = 0
        consecutive_empty = 0

        while True:
            url = (
                f"{PUBLICSEARCH_BASE}/results"
                f"?department=FC"
                f"&instrumentDateRange={cutoff_str}%2C{today_str}"
                f"&keywordSearch=false"
                f"&offset={offset}"
            )
            log.info(f"Fetching offset={offset}")

            try:
                driver.get(url)
                WebDriverWait(driver, 30).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "table tr, .result-document"))
                )
                time.sleep(2)
            except Exception as e:
                log.warning(f"Timeout at offset {offset}: {e}")
                time.sleep(3)

            src = driver.page_source
            count_match = re.search(r'(\d[\d,]*)\s*of\s*(\d[\d,]*)\s*results?', src, re.IGNORECASE)
            if count_match:
                log.info(f"Results: {count_match.group(0)}")

            page_records = []

            # HTML table extraction
            rows = re.findall(r'<tr[^>]*>(.*?)</tr>', src, re.DOTALL | re.IGNORECASE)
            for row in rows:
                if re.search(r'<th|thead|DOC.TYPE|RECORDED.DATE', row, re.IGNORECASE):
                    continue
                cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
                cells = [re.sub(r'<[^>]+>', '', c).strip() for c in cells if c.strip()]
                if len(cells) < 3:
                    continue
                # Doc number: 9-12 digits
                doc_num = next((c for c in cells if re.match(r'^\d{9,12}$', c.strip())), "")
                if not doc_num or doc_num in known_docs:
                    continue
                dates = [c for c in cells if re.match(r'^\d{1,2}/\d{1,2}/\d{4}$', c.strip())]
                address = next((c for c in cells 
                    if len(c) > 8 and c not in dates and not re.match(r'^\d{9,12}$', c)
                    and 'FORECLOSURE' not in c.upper() and c.upper() != 'N/A'), "N/A")
                page_records.append({
                    "doc_number":  doc_num,
                    "type":        "NOF",
                    "source":      "publicsearch",
                    "county":      "nueces",
                    "address":     address,
                    "city":        "CORPUS CHRISTI",
                    "zip":         "",
                    "owner":       "",
                    "date_filed":  dates[0] if dates else "",
                    "sale_date":   dates[1] if len(dates) > 1 else "",
                })

            log.info(f"offset={offset} | {len(page_records)} extracted")

            page_new = 0
            for rec in page_records:
                doc = rec["doc_number"]
                if doc not in known_docs:
                    known_docs.add(doc)
                    flags, days = [], None
                    if rec.get("sale_date"):
                        sd = parse_date(rec["sale_date"])
                        if sd:
                            days = (sd - TODAY).days
                            if days <= 14: flags.append("URGENT")
                            elif days <= 30: flags.append("AUCTION SOON")
                    rec.update({
                        "is_new": True, "run_ts": RUN_TIMESTAMP,
                        "score": 5, "flags": flags,
                        "absentee": False, "duplicate": False,
                        "days_until_sale": days,
                        "loan_amount": "", "loan_date": "", "lender": "",
                        "trustee": "", "appraised_value": "", "annual_taxes": "",
                        "mail_addr": "", "ps_doc_id": "",
                    })
                    new_records.append(rec)
                    page_new += 1

            log.info(f"offset={offset} | {page_new} new | {len(new_records)} total")

            if len(page_records) == 0:
                consecutive_empty += 1
            else:
                consecutive_empty = 0

            if consecutive_empty >= 2:
                log.info("2 empty pages — stopping")
                break
            if 0 < len(page_records) < 50:
                log.info("Last page — stopping")
                break

            offset += 50
            time.sleep(1.5)

        # ── Doc detail fetch for owner names ──────────────────────────────────
        needs_detail = [r for r in new_records if not r.get("owner")][:DOC_FETCH_LIMIT]
        log.info(f"Fetching details for {len(needs_detail)} records (limit {DOC_FETCH_LIMIT})...")

        enriched = 0
        for i, rec in enumerate(needs_detail):
            doc_url = f"{PUBLICSEARCH_BASE}/results/{rec['doc_number']}"
            try:
                driver.get(doc_url)
                WebDriverWait(driver, 20).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "table, h1, .detail, [class*='detail']"))
                )
                time.sleep(1.5)
                detail_src = driver.page_source

                # Extract grantor (owner) — labeled as Grantor, Mortgagor, or Trustor
                grantor = ""
                for label in ["Grantor", "Mortgagor", "Trustor", "Borrower"]:
                    m = re.search(
                        rf'{label}[:\s]*</(?:td|th|label|span|div)[^>]*>\s*<(?:td|th|span|div)[^>]*>\s*([A-Z][^<]{{3,60}})',
                        detail_src, re.IGNORECASE)
                    if m:
                        grantor = m.group(1).strip()
                        break
                    # Try adjacent cell pattern
                    m2 = re.search(
                        rf'<(?:td|th)[^>]*>\s*{label}\s*</(?:td|th)>\s*<(?:td|th)[^>]*>\s*([A-Z &,\.\']+)\s*</(?:td|th)>',
                        detail_src, re.IGNORECASE)
                    if m2:
                        grantor = m2.group(1).strip()
                        break

                # Fallback: look for party name in detail page
                if not grantor:
                    party_m = re.search(
                        r'(?:party|name|grantor)[^<]*</[^>]+>\s*<[^>]+>\s*([A-Z][A-Z &,\.\']{4,50})',
                        detail_src, re.IGNORECASE)
                    if party_m:
                        grantor = party_m.group(1).strip()

                # Extract property address from detail
                addr = ""
                addr_m = re.search(
                    r'(?:property address|situs|address)[:\s]*</[^>]+>\s*<[^>]+>\s*(\d+[^<]{5,60})',
                    detail_src, re.IGNORECASE)
                if addr_m:
                    addr = re.sub(r'<[^>]+>', '', addr_m.group(1)).strip()

                if grantor:
                    rec["owner"] = grantor
                    enriched += 1
                if addr and addr != "N/A":
                    rec["address"] = addr

                if (i + 1) % 10 == 0:
                    log.info(f"  Detail fetch progress: {i+1}/{len(needs_detail)} | {enriched} enriched")

            except Exception as e:
                log.debug(f"  Detail fetch failed for {rec['doc_number']}: {e}")

            time.sleep(0.8)

        log.info(f"Detail fetch complete: {enriched}/{len(needs_detail)} enriched with owner names")

    except Exception as e:
        log.error(f"Scraper error: {e}", exc_info=True)
    finally:
        if driver:
            try: driver.quit()
            except Exception: pass

    log.info(f"PublicSearch total: {len(new_records)} new records")
    return new_records

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

def main():
    log.info("=" * 60)
    log.info("Nueces County Lead Scraper v1.3")
    log.info(f"Cutoff: {CUTOFF} (21 days) | Today: {TODAY}")
    log.info("=" * 60)

    known_docs, prev_records = load_known_docs()
    new_records = scrape_publicsearch(known_docs)

    for r in prev_records:
        r["is_new"] = False

    # Merge
    seen = {}
    for r in new_records + prev_records:
        doc = r.get("doc_number","")
        if doc and doc not in seen:
            seen[doc] = r
    records = list(seen.values())

    # Also enrich prev records that are missing owner — pick up to limit
    # (handled in next run naturally)

    for r in records:
        s = 5
        if r.get("days_until_sale") is not None:
            if r["days_until_sale"] <= 14: s += 2
            elif r["days_until_sale"] <= 30: s += 1
        r["score"] = min(s, 10)

    new_ct  = sum(1 for r in records if r.get("is_new"))
    named   = sum(1 for r in records if r.get("owner"))
    urgent  = sum(1 for r in records if "URGENT" in r.get("flags",[]))
    log.info(f"Final: {len(records)} total | {new_ct} new | {named} named | {urgent} URGENT")
    write_records(records)
    log.info(f"Dashboard: {len(records)} records, {RECORDS_PATH.stat().st_size:,} bytes")
    log.info("Done.")

if __name__ == "__main__":
    main()
