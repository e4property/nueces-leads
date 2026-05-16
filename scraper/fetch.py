"""
Nueces County Motivated Seller Lead Scraper v2.0
FINAL: Internal IDs found as 3xxxxxxxx numbers in page source.
Extract them in order, pair with table rows by index.
Then visit /doc/{id} for party names.
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
ENRICH_LIMIT      = 60

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

def wait_and_get(driver, url, wait_sel="table tr td", timeout=30, sleep=3):
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.common.by import By
    driver.get(url)
    try:
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, wait_sel)))
        time.sleep(sleep)
    except Exception:
        time.sleep(sleep + 2)
    return driver.page_source

def extract_internal_ids(src):
    """Extract 3xxxxxxxx internal IDs from page source, deduplicated in order."""
    ids = re.findall(r'\b(3\d{8})\b', src)
    seen = set()
    unique = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            unique.append(i)
    return unique

def extract_table_rows(src, known_docs, skip_known=True):
    """Extract doc records from table HTML."""
    records = []
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', src, re.DOTALL | re.IGNORECASE)
    for row in rows:
        if re.search(r'<th|thead|DOC.TYPE|RECORDED.DATE', row, re.IGNORECASE):
            continue
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
        cells = [re.sub(r'<[^>]+>', '', c).strip() for c in cells if c.strip()]
        if len(cells) < 3:
            continue
        doc_num = next((c for c in cells if re.match(r'^\d{9,12}$', c.strip())), "")
        if not doc_num:
            continue
        if skip_known and doc_num in known_docs:
            continue
        dates = [c for c in cells if re.match(r'^\d{1,2}/\d{1,2}/\d{4}$', c.strip())]
        address = next((c for c in cells
            if len(c) > 8 and c not in dates
            and not re.match(r'^\d{9,12}$', c)
            and 'FORECLOSURE' not in c.upper()
            and c.upper() not in ('N/A','')), "N/A")
        records.append({
            "doc_number": doc_num, "internal_id": "",
            "address": address,
            "date_filed": dates[0] if dates else "",
            "sale_date": dates[1] if len(dates) > 1 else "",
        })
    return records

def get_party_from_detail(driver, internal_id):
    """Visit /doc/{id} and extract grantor from page source 3xxxxxxxx JSON."""
    url = f"{PUBLICSEARCH_BASE}/doc/{internal_id}"
    src = wait_and_get(driver, url, wait_sel="table, [class*='summary'], h1", timeout=20, sleep=2)

    # The detail page source contains parties in JSON format
    # Look for "parties":[{"name":"..."}] pattern
    party_m = re.search(r'"parties"\s*:\s*\[([^\]]{0,500})\]', src)
    if party_m:
        name_m = re.search(r'"name"\s*:\s*"([^"]{3,60})"', party_m.group(1))
        if name_m:
            return name_m.group(1).strip()

    # Try broader search for name near grantor/mortgagor
    for label in ["grantor","mortgagor","trustor","borrower","debtor"]:
        m = re.search(
            rf'"{label}[^"]*"\s*:\s*"([A-Z][^"{{}}]{{3,60}})"',
            src, re.IGNORECASE)
        if m:
            return m.group(1).strip()

    # Try rendered DOM text via JS
    try:
        name = driver.execute_script("""
            try {
                var src = document.documentElement.innerHTML;
                var m = src.match(/"parties":\[{"name":"([^"]{3,60})"/);
                if (m) return m[1];
                var m2 = src.match(/"name":"([A-Z][A-Z &,.']{4,50})","type":"(?:grantor|mortgagor)"/i);
                if (m2) return m2[1];
                return '';
            } catch(e) { return ''; }
        """)
        if name:
            return name.strip()
    except Exception:
        pass

    return ""

def get_address_from_detail(src):
    """Extract property address from detail page source."""
    addr_m = re.search(r'"propAddress"\s*:\s*\[([^\]]{0,300})\]', src)
    if addr_m:
        a1 = re.search(r'"address1"\s*:\s*"([^"]{3,60})"', addr_m.group(1))
        city = re.search(r'"city"\s*:\s*"([^"]{2,30})"', addr_m.group(1))
        if a1:
            parts = [a1.group(1)]
            if city: parts.append(city.group(1))
            parts.append("TX")
            return " ".join(parts)
    return ""

def scrape_and_map(known_docs):
    """Scrape search results, extract doc numbers AND internal IDs."""
    driver = None
    all_records = []
    doc_id_map = {}  # doc_number -> internal_id
    cutoff_str = CUTOFF.strftime("%Y%m%d")
    today_str = TODAY.strftime("%Y%m%d")

    try:
        driver = get_driver()
        offset = 0
        consecutive_empty = 0

        while True:
            url = (f"{PUBLICSEARCH_BASE}/results"
                   f"?department=FC"
                   f"&instrumentDateRange={cutoff_str}%2C{today_str}"
                   f"&keywordSearch=false&offset={offset}")
            log.info(f"Fetching offset={offset}")
            src = wait_and_get(driver, url)

            # Extract internal IDs from page source (3xxxxxxxx pattern)
            internal_ids = extract_internal_ids(src)

            # Extract table rows (all, including known — to build ID map)
            all_rows = extract_table_rows(src, known_docs, skip_known=False)
            new_rows = [r for r in all_rows if r["doc_number"] not in known_docs]

            log.info(f"offset={offset} | rows={len(all_rows)} | internal_ids={len(internal_ids)} | new={len(new_rows)}")

            # Pair internal IDs with doc numbers by index
            for i, row in enumerate(all_rows):
                if i < len(internal_ids):
                    doc_id_map[row["doc_number"]] = internal_ids[i]

            count_m = re.search(r'(\d[\d,]*)\s*of\s*(\d[\d,]*)\s*results?', src, re.IGNORECASE)
            if count_m:
                log.info(f"Results: {count_m.group(0)}")

            # Add new records
            for rec in new_rows:
                if rec["doc_number"] not in known_docs:
                    known_docs.add(rec["doc_number"])
                    flags, days = [], None
                    if rec.get("sale_date"):
                        sd = parse_date(rec["sale_date"])
                        if sd:
                            days = (sd - TODAY).days
                            if days <= 14: flags.append("URGENT")
                            elif days <= 30: flags.append("AUCTION SOON")
                    rec.update({
                        "type": "NOF", "source": "publicsearch", "county": "nueces",
                        "city": "CORPUS CHRISTI", "zip": "", "owner": "",
                        "is_new": True, "run_ts": RUN_TIMESTAMP,
                        "score": 5, "flags": flags, "absentee": False,
                        "duplicate": False, "days_until_sale": days,
                        "loan_amount": "", "loan_date": "", "lender": "",
                        "trustee": "", "appraised_value": "", "annual_taxes": "",
                        "mail_addr": "", "ps_doc_id": "",
                    })
                    all_records.append(rec)

            if len(all_rows) == 0:
                consecutive_empty += 1
            else:
                consecutive_empty = 0
            if consecutive_empty >= 2 or (0 < len(all_rows) < 50):
                break
            offset += 50
            time.sleep(1.5)

        log.info(f"doc_id_map built: {len(doc_id_map)} entries")
        sample = list(doc_id_map.items())[:3]
        log.info(f"Sample mappings: {sample}")

    except Exception as e:
        log.error(f"Scraper error: {e}", exc_info=True)
    finally:
        if driver:
            try: driver.quit()
            except Exception: pass

    return all_records, doc_id_map

def enrich_records(records, doc_id_map):
    """Visit detail pages for records missing owner names."""
    bad = {'Window.','Window','Search Results','Nueces County','Document Preview'}
    for r in records:
        if r.get("owner","") in bad:
            r["owner"] = ""
        # Update internal_id from map
        if not r.get("internal_id") and r.get("doc_number") in doc_id_map:
            r["internal_id"] = doc_id_map[r["doc_number"]]

    needs = [r for r in records if not r.get("owner") and r.get("internal_id")][:ENRICH_LIMIT]
    log.info(f"Enriching {len(needs)} records with detail page data...")

    if not needs:
        no_id = sum(1 for r in records if not r.get("owner"))
        log.info(f"No records to enrich ({no_id} missing names but no internal_id)")
        return 0

    driver = None
    enriched = 0
    try:
        driver = get_driver()
        for i, rec in enumerate(needs):
            grantor = get_party_from_detail(driver, rec["internal_id"])
            if grantor:
                rec["owner"] = grantor.title()
                enriched += 1
            # Also try to get address
            if rec.get("address") in ("N/A", "", None):
                src = driver.page_source
                addr = get_address_from_detail(src)
                if addr:
                    rec["address"] = addr
            if (i+1) % 10 == 0:
                log.info(f"  {i+1}/{len(needs)} | {enriched} named")
            time.sleep(0.8)
    except Exception as e:
        log.error(f"Enrichment error: {e}")
    finally:
        if driver:
            try: driver.quit()
            except Exception: pass

    log.info(f"Enrichment: {enriched}/{len(needs)} named")
    return enriched

def load_known_docs():
    try:
        existing = json.loads(RECORDS_PATH.read_text())
        log.info(f"Loaded {len(existing)} existing records")
        return {r["doc_number"] for r in existing if r.get("doc_number")}, existing
    except Exception:
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
    log.info("Nueces County Lead Scraper v2.0")
    log.info(f"Cutoff: {CUTOFF} (21 days) | Today: {TODAY}")
    log.info("=" * 60)

    known_docs, prev_records = load_known_docs()
    new_records, doc_id_map = scrape_and_map(known_docs)

    for r in prev_records:
        r["is_new"] = False

    seen = {}
    for r in new_records + prev_records:
        doc = r.get("doc_number","")
        if doc and doc not in seen:
            seen[doc] = r
    records = list(seen.values())
    log.info(f"After merge: {len(records)} total | {len(doc_id_map)} IDs mapped")

    enrich_records(records, doc_id_map)

    for r in records:
        s = 5
        if r.get("days_until_sale") is not None:
            if r["days_until_sale"] <= 14: s += 2
            elif r["days_until_sale"] <= 30: s += 1
        r["score"] = min(s, 10)

    new_ct = sum(1 for r in records if r.get("is_new"))
    named  = sum(1 for r in records if r.get("owner"))
    urgent = sum(1 for r in records if "URGENT" in r.get("flags",[]))
    log.info(f"Final: {len(records)} total | {new_ct} new | {named} named | {urgent} URGENT")
    write_records(records)
    log.info(f"Dashboard: {len(records)} records, {RECORDS_PATH.stat().st_size:,} bytes")
    log.info("Done.")

if __name__ == "__main__":
    main()
