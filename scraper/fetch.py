"""
Nueces County Motivated Seller Lead Scraper v1.6
v1.6: Extract internal doc IDs from search results href="/doc/{id}"
      Hit /api/documents/{id} directly for party names and address
      No more Selenium detail page scraping — pure urllib for enrichment
"""

import json, logging, re, time, urllib.request, urllib.parse
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
API_FETCH_LIMIT   = 249

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

def fetch_doc_api(internal_id):
    """
    Hit /api/documents/{id} directly — returns JSON with parties and address.
    No Selenium needed for this step.
    """
    url = f"{PUBLICSEARCH_BASE}/api/documents/{internal_id}"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json, text/plain, */*",
            "Referer": f"{PUBLICSEARCH_BASE}/doc/{internal_id}",
        })
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace"))
        return data
    except Exception as e:
        log.debug(f"API fetch error for doc {internal_id}: {e}")
        return {}

def extract_from_api(data):
    """Parse grantor name and property address from API response."""
    grantor = ""
    address = ""

    # Parties array
    parties = data.get("parties") or data.get("data", {}).get("parties") or []
    for party in parties:
        role = (party.get("type") or party.get("role") or "").lower()
        name = (party.get("name") or party.get("fullName") or "").strip()
        if role in ("grantor", "mortgagor", "trustor", "borrower", "debtor") and name:
            grantor = name
            break
    # If no typed grantor, take first party
    if not grantor and parties:
        grantor = (parties[0].get("name") or parties[0].get("fullName") or "").strip()

    # Property address
    prop_addrs = (data.get("propAddress") or
                  data.get("data", {}).get("propAddress") or
                  data.get("propertyAddresses") or [])
    if isinstance(prop_addrs, list) and prop_addrs:
        a = prop_addrs[0]
        parts = [a.get("address1",""), a.get("address2",""),
                 a.get("city",""), a.get("state",""), a.get("zip","")]
        address = " ".join(p for p in parts if p).strip()
    elif isinstance(prop_addrs, str):
        address = prop_addrs.strip()

    # Alternative address field names
    if not address:
        address = (data.get("propertyAddress") or
                   data.get("data", {}).get("propertyAddress") or "").strip()

    return grantor, address

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

            # Extract internal doc IDs from href="/doc/{id}" links
            internal_ids = re.findall(r'href=["\']\/doc\/(\d+)["\']', src)
            log.info(f"Internal doc IDs found: {len(internal_ids)}")

            page_records = []
            rows = re.findall(r'<tr[^>]*>(.*?)</tr>', src, re.DOTALL | re.IGNORECASE)
            row_idx = 0
            for row in rows:
                if re.search(r'<th|thead|DOC.TYPE|RECORDED.DATE', row, re.IGNORECASE):
                    continue
                cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
                cells = [re.sub(r'<[^>]+>', '', c).strip() for c in cells if c.strip()]
                if len(cells) < 3:
                    continue
                doc_num = next((c for c in cells if re.match(r'^\d{9,12}$', c.strip())), "")
                if not doc_num or doc_num in known_docs:
                    row_idx += 1
                    continue
                dates = [c for c in cells if re.match(r'^\d{1,2}/\d{1,2}/\d{4}$', c.strip())]
                address = next((c for c in cells
                    if len(c) > 8 and c not in dates
                    and not re.match(r'^\d{9,12}$', c)
                    and 'FORECLOSURE' not in c.upper()
                    and c.upper() not in ('N/A','')), "N/A")

                # Get matching internal ID for this row
                internal_id = internal_ids[row_idx] if row_idx < len(internal_ids) else ""

                page_records.append({
                    "doc_number":  doc_num,
                    "internal_id": internal_id,
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
                row_idx += 1

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

    return new_records

def enrich_with_api(records):
    """
    For all records missing owner names, hit /api/documents/{internal_id}
    directly using urllib — fast, no Selenium needed.
    """
    # Clear bad names from previous runs
    bad = {'Window.', 'Window', 'Search Results', 'Nueces County', 'Document Preview'}
    for r in records:
        if r.get("owner","") in bad:
            r["owner"] = ""

    needs = [r for r in records if not r.get("owner") and r.get("internal_id")]
    no_id = [r for r in records if not r.get("owner") and not r.get("internal_id")]
    log.info(f"Enrichment: {len(needs)} with internal_id | {len(no_id)} without")

    needs = needs[:API_FETCH_LIMIT]
    enriched = 0
    addr_filled = 0

    for i, rec in enumerate(needs):
        internal_id = rec["internal_id"]
        data = fetch_doc_api(internal_id)

        if data:
            grantor, address = extract_from_api(data)
            if grantor:
                rec["owner"] = grantor.title()
                enriched += 1
            if address and address != "N/A":
                rec["address"] = address
                addr_filled += 1

        if (i + 1) % 20 == 0:
            log.info(f"  API progress: {i+1}/{len(needs)} | {enriched} named | {addr_filled} addresses")
        time.sleep(0.3)

    log.info(f"API enrichment: {enriched}/{len(needs)} named | {addr_filled} addresses filled")
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
    log.info("Nueces County Lead Scraper v1.6")
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
    log.info(f"After merge: {len(records)} total")

    # Enrich all missing owner names via API
    enrich_with_api(records)

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
