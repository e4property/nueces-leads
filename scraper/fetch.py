"""
Nueces County Motivated Seller Lead Scraper v1.4
v1.4: Detail fetch runs on ALL records missing owner names
      (new + existing), not just new ones
"""

import json, logging, re, time
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
DOC_FETCH_LIMIT   = 60

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
                f"&keywordSearch=false&offset={offset}"
            )
            log.info(f"Fetching offset={offset}")
            try:
                driver.get(url)
                WebDriverWait(driver, 30).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "table tr")))
                time.sleep(2)
            except Exception as e:
                log.warning(f"Timeout at offset {offset}: {e}")
                time.sleep(3)

            src = driver.page_source
            count_m = re.search(r'(\d[\d,]*)\s*of\s*(\d[\d,]*)\s*results?', src, re.IGNORECASE)
            if count_m:
                log.info(f"Results: {count_m.group(0)}")

            page_records = []
            rows = re.findall(r'<tr[^>]*>(.*?)</tr>', src, re.DOTALL | re.IGNORECASE)
            for row in rows:
                if re.search(r'<th|thead|DOC.TYPE|RECORDED.DATE', row, re.IGNORECASE):
                    continue
                cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
                cells = [re.sub(r'<[^>]+>', '', c).strip() for c in cells if c.strip()]
                if len(cells) < 3:
                    continue
                doc_num = next((c for c in cells if re.match(r'^\d{9,12}$', c.strip())), "")
                if not doc_num or doc_num in known_docs:
                    continue
                dates = [c for c in cells if re.match(r'^\d{1,2}/\d{1,2}/\d{4}$', c.strip())]
                address = next((c for c in cells
                    if len(c) > 8 and c not in dates
                    and not re.match(r'^\d{9,12}$', c)
                    and 'FORECLOSURE' not in c.upper()
                    and c.upper() not in ('N/A','')), "N/A")
                page_records.append({
                    "doc_number": doc_num, "type": "NOF",
                    "source": "publicsearch", "county": "nueces",
                    "address": address, "city": "CORPUS CHRISTI", "zip": "",
                    "owner": "", "date_filed": dates[0] if dates else "",
                    "sale_date": dates[1] if len(dates) > 1 else "",
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
                        "score": 5, "flags": flags, "absentee": False,
                        "duplicate": False, "days_until_sale": days,
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

    except Exception as e:
        log.error(f"Scraper error: {e}", exc_info=True)
    finally:
        if driver:
            try: driver.quit()
            except Exception: pass

    return new_records, get_driver


def fetch_owner_details(records, get_driver_fn):
    """Fetch owner names for records missing them — new + existing"""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    needs = [r for r in records if not r.get("owner") or r["owner"] == ""]
    needs = needs[:DOC_FETCH_LIMIT]
    log.info(f"Fetching owner details for {len(needs)} records...")

    if not needs:
        return 0

    driver = None
    enriched = 0
    try:
        driver = get_driver_fn()
        for i, rec in enumerate(needs):
            url = f"{PUBLICSEARCH_BASE}/results/{rec['doc_number']}"
            try:
                driver.get(url)
                WebDriverWait(driver, 20).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "table, h1, [class*='detail']")))
                time.sleep(1.5)
                src = driver.page_source

                # Extract grantor name
                grantor = ""
                # Pattern 1: label in one cell, value in next
                for label in ["Grantor", "Mortgagor", "Trustor", "Borrower", "Debtor"]:
                    m = re.search(
                        rf'<td[^>]*>\s*{label}\s*</td>\s*<td[^>]*>\s*([A-Z][A-Z ,&\.\'\-]{{3,60}})\s*</td>',
                        src, re.IGNORECASE)
                    if m:
                        grantor = m.group(1).strip()
                        break
                    # Try span/div patterns
                    m2 = re.search(
                        rf'{label}[^<]*</[^>]+>\s*(?:<[^>]+>)*\s*([A-Z][A-Z ,&\.\'\-]{{3,60}})',
                        src, re.IGNORECASE)
                    if m2:
                        candidate = m2.group(1).strip()
                        if len(candidate) > 4 and candidate not in ("FORECLOSURE", "NOTICE"):
                            grantor = candidate
                            break

                # Pattern 2: __NEXT_DATA__ JSON on detail page
                if not grantor:
                    nd_m = re.search(r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', src, re.DOTALL)
                    if nd_m:
                        try:
                            nd = json.loads(nd_m.group(1))
                            pp = nd.get("props",{}).get("pageProps",{})
                            doc_data = pp.get("document",{}) or pp.get("result",{}) or pp
                            grantor = (doc_data.get("grantorName") or
                                      doc_data.get("grantor") or
                                      doc_data.get("mortgagor") or "")
                            if not grantor and "parties" in doc_data:
                                for party in doc_data["parties"]:
                                    if party.get("type","").lower() in ("grantor","mortgagor"):
                                        grantor = party.get("name","")
                                        break
                        except Exception:
                            pass

                # Extract address from detail
                addr = ""
                addr_m = re.search(
                    r'(?:property.?address|situs|address)[:\s]*</[^>]+>\s*<[^>]+>\s*(\d+[^<]{5,80})',
                    src, re.IGNORECASE)
                if addr_m:
                    addr = re.sub(r'<[^>]+>', '', addr_m.group(1)).strip()
                    # Clean up
                    addr = re.sub(r'\s+', ' ', addr).strip()

                if grantor:
                    rec["owner"] = grantor.title()
                    enriched += 1
                if addr and addr != "N/A" and len(addr) > 5:
                    rec["address"] = addr

                if (i+1) % 10 == 0:
                    log.info(f"  Detail progress: {i+1}/{len(needs)} | {enriched} with names")

            except Exception as e:
                log.debug(f"  Detail fetch error for {rec['doc_number']}: {e}")
            time.sleep(0.8)

    except Exception as e:
        log.error(f"Detail fetch driver error: {e}")
    finally:
        if driver:
            try: driver.quit()
            except Exception: pass

    log.info(f"Owner enrichment: {enriched}/{len(needs)} enriched")
    return enriched


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
    log.info("Nueces County Lead Scraper v1.4")
    log.info(f"Cutoff: {CUTOFF} (21 days) | Today: {TODAY}")
    log.info("=" * 60)

    known_docs, prev_records = load_known_docs()
    new_records, get_driver_fn = scrape_publicsearch(known_docs)

    for r in prev_records:
        r["is_new"] = False

    # Merge
    seen = {}
    for r in new_records + prev_records:
        doc = r.get("doc_number","")
        if doc and doc not in seen:
            seen[doc] = r
    records = list(seen.values())
    log.info(f"After merge: {len(records)} total records")

    # Enrich ALL records missing owner names (new + existing)
    fetch_owner_details(records, get_driver_fn)

    # Score
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
