"""
Nueces County Motivated Seller Lead Scraper v2.2
NEW APPROACH: Get owner names from Nueces CAD (esearch.nuecescad.net)
using address/legal description search — same BIS platform as Bexar CAD.
Search by street address or legal description to get owner name + appraised value.
"""

import json, logging, re, time
from datetime import datetime, timezone, timedelta
from pathlib import Path

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger(__name__)

PUBLICSEARCH_BASE = "https://nueces.tx.publicsearch.us"
NUECES_CAD        = "https://esearch.nuecescad.net"
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
    ids = re.findall(r'\b(3\d{8})\b', src)
    seen, unique = set(), []
    for i in ids:
        if i not in seen:
            seen.add(i)
            unique.append(i)
    return unique

def extract_table_rows(src, known_docs, skip_known=True):
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

def cad_search(driver, query, search_type="address"):
    """
    Search Nueces CAD for owner name and appraised value.
    Returns (owner, appraised_value) or ("", "")
    """
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.common.by import By

    import urllib.parse
    encoded = urllib.parse.quote(query)
    url = f"{NUECES_CAD}/Property/SearchResults?searchType={search_type}&searchText={encoded}&take=5&skip=0"

    try:
        driver.get(url)
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.CSS_SELECTOR,
                ".search-results, table, .property-card, [class*='result'], .no-results")))
        time.sleep(1.5)
        src = driver.page_source

        # Extract owner name from results
        owner = ""
        appraised = ""

        # BIS platform returns results as JSON in page or as HTML table
        # Try JSON first
        json_m = re.search(r'"ownerName"\s*:\s*"([^"]{3,60})"', src)
        if json_m:
            owner = json_m.group(1).strip()

        if not owner:
            json_m2 = re.search(r'"Owner"\s*:\s*"([^"]{3,60})"', src)
            if json_m2:
                owner = json_m2.group(1).strip()

        # Try HTML table
        if not owner:
            rows = re.findall(r'<tr[^>]*>(.*?)</tr>', src, re.DOTALL | re.IGNORECASE)
            for row in rows:
                cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
                cells = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]
                for cell in cells:
                    if re.match(r'^[A-Z][A-Z ,&\.\'\-]{5,50}$', cell) and ' ' in cell:
                        bad = {'CORPUS CHRISTI', 'NUECES COUNTY', 'TEXAS', 'PROPERTY'}
                        if cell not in bad:
                            owner = cell
                            break

        # Extract appraised value
        val_m = re.search(r'"(?:TotalValue|AppraisedValue|totalAppraised)"\s*:\s*(\d+)', src)
        if val_m:
            appraised = f"${int(val_m.group(1)):,}"

        return owner, appraised

    except Exception as e:
        log.debug(f"CAD search error for '{query}': {e}")
        return "", ""

def scrape_and_map(known_docs):
    driver = None
    all_new_records = []
    doc_id_map = {}
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

            internal_ids = extract_internal_ids(src)
            all_rows = extract_table_rows(src, known_docs, skip_known=False)
            new_rows = [r for r in all_rows if r["doc_number"] not in known_docs]

            count_m = re.search(r'(\d[\d,]*)\s*of\s*(\d[\d,]*)\s*results?', src, re.IGNORECASE)
            if count_m:
                log.info(f"Results: {count_m.group(0)} | rows={len(all_rows)} | ids={len(internal_ids)} | new={len(new_rows)}")

            for i, row in enumerate(all_rows):
                if i < len(internal_ids):
                    doc_id_map[row["doc_number"]] = internal_ids[i]

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
                    all_new_records.append(rec)

            if len(all_rows) == 0:
                consecutive_empty += 1
            else:
                consecutive_empty = 0
            if consecutive_empty >= 2 or (0 < len(all_rows) < 50):
                break
            offset += 50
            time.sleep(1.5)

        log.info(f"doc_id_map: {len(doc_id_map)} entries | new: {len(all_new_records)}")

    except Exception as e:
        log.error(f"Scraper error: {e}", exc_info=True)
    finally:
        if driver:
            try: driver.quit()
            except Exception: pass

    return all_new_records, doc_id_map

def enrich_from_cad(records):
    """Search Nueces CAD for owner names using property address/legal description."""
    bad = {'Window.','Window','Search Results','Nueces County','Document Preview'}
    for r in records:
        if r.get("owner","") in bad:
            r["owner"] = ""

    needs = [r for r in records if not r.get("owner") and r.get("address","") not in ("N/A","","")]
    needs = needs[:ENRICH_LIMIT]
    log.info(f"CAD enrichment: {len(needs)} records to search...")

    if not needs:
        log.info("No records with addresses to search CAD")
        return 0

    driver = None
    enriched = 0
    try:
        driver = get_driver()

        # Test first record
        test = needs[0]
        addr = test.get("address","")
        log.info(f"Testing CAD search for: '{addr}'")
        owner, appraised = cad_search(driver, addr)
        log.info(f"CAD test result: owner='{owner}' | appraised='{appraised}'")

        if owner:
            test["owner"] = owner.title()
            if appraised:
                test["appraised_value"] = appraised
            enriched += 1

        for i, rec in enumerate(needs[1:], 1):
            addr = rec.get("address","")
            if not addr or addr == "N/A":
                continue

            # Try full address first, then first part of legal description
            owner, appraised = cad_search(driver, addr)

            # If no result, try just the subdivision name (first 3 words)
            if not owner:
                short = " ".join(addr.split()[:3])
                if short != addr:
                    owner, appraised = cad_search(driver, short)

            if owner:
                rec["owner"] = owner.title()
                if appraised:
                    rec["appraised_value"] = appraised
                enriched += 1

            if (i+1) % 10 == 0:
                log.info(f"  CAD: {i+1}/{len(needs)} | {enriched} named")
            time.sleep(0.5)

    except Exception as e:
        log.error(f"CAD enrichment error: {e}")
    finally:
        if driver:
            try: driver.quit()
            except Exception: pass

    log.info(f"CAD enrichment: {enriched}/{len(needs)} named")
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
    log.info("Nueces County Lead Scraper v2.2")
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

    enrich_from_cad(records)

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
