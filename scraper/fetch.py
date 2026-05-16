"""
Nueces County Motivated Seller Lead Scraper v2.1
KEY FIX: Use Selenium execute_async_script to fetch() the API from within
the browser session — avoids 403 since browser has valid auth cookies.
The doc_id_map approach works (confirmed v2.0). Party extraction was failing
because data loads async. Now we fetch it directly via browser's fetch().
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
ENRICH_LIMIT      = 100

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
    seen = set()
    unique = []
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

def fetch_doc_via_browser(driver, internal_id):
    """
    Use the browser's fetch() API to call /api/documents/{id}.
    The browser has valid session cookies so won't get 403.
    Returns (grantor, address) tuple.
    """
    # First navigate to the search page to ensure valid session
    js = f"""
    var done = arguments[0];
    fetch('/api/documents/{internal_id}', {{
        method: 'GET',
        headers: {{
            'Accept': 'application/json',
            'Content-Type': 'application/json'
        }},
        credentials: 'include'
    }})
    .then(function(r) {{ return r.json(); }})
    .then(function(data) {{
        var result = {{grantor: '', address: ''}};
        // Try parties array
        var parties = data.parties || (data.data && data.data.parties) || [];
        for (var i = 0; i < parties.length; i++) {{
            var p = parties[i];
            if (p && p.name && p.name.length > 2) {{
                result.grantor = p.name;
                break;
            }}
        }}
        // Try propAddress
        var addrs = data.propAddress || (data.data && data.data.propAddress) || [];
        if (addrs && addrs.length > 0) {{
            var a = addrs[0];
            result.address = (a.address1 || '') + ' ' + (a.city || '') + ' ' + (a.state || '');
            result.address = result.address.trim();
        }}
        // Also check top-level fields
        if (!result.grantor) {{
            result.grantor = data.grantorName || data.grantor || '';
        }}
        // Log full response for first doc
        result.raw_keys = Object.keys(data).join(',');
        result.parties_count = parties.length;
        done(JSON.stringify(result));
    }})
    .catch(function(e) {{
        done(JSON.stringify({{error: e.toString(), grantor: '', address: ''}}));
    }});
    """
    try:
        result_str = driver.execute_async_script(js)
        result = json.loads(result_str)
        if result.get("error"):
            log.debug(f"Fetch error for {internal_id}: {result['error']}")
        return result.get("grantor","").strip(), result.get("address","").strip(), result
    except Exception as e:
        log.debug(f"execute_async_script error for {internal_id}: {e}")
        return "", "", {}

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

        log.info(f"doc_id_map: {len(doc_id_map)} entries | new records: {len(all_new_records)}")

    except Exception as e:
        log.error(f"Scraper error: {e}", exc_info=True)
    finally:
        if driver:
            try: driver.quit()
            except Exception: pass

    return all_new_records, doc_id_map

def enrich_records(records, doc_id_map):
    bad = {'Window.','Window','Search Results','Nueces County','Document Preview'}
    for r in records:
        if r.get("owner","") in bad:
            r["owner"] = ""
        if not r.get("internal_id") and r.get("doc_number") in doc_id_map:
            r["internal_id"] = doc_id_map[r["doc_number"]]

    needs = [r for r in records if not r.get("owner") and r.get("internal_id")][:ENRICH_LIMIT]
    log.info(f"Enriching {len(needs)} records via browser fetch()...")

    if not needs:
        log.info("No records to enrich")
        return 0

    driver = None
    enriched = 0
    try:
        driver = get_driver()
        # Navigate to base page first to establish session
        driver.get(f"{PUBLICSEARCH_BASE}/")
        time.sleep(2)

        # Test first record and log full response
        test_rec = needs[0]
        log.info(f"Testing API fetch for doc {test_rec['doc_number']} internal_id {test_rec['internal_id']}...")
        grantor, address, raw = fetch_doc_via_browser(driver, test_rec["internal_id"])
        log.info(f"Test result: grantor='{grantor}' | address='{address}'")
        log.info(f"API response keys: {raw.get('raw_keys','?')} | parties_count: {raw.get('parties_count','?')}")

        if grantor:
            test_rec["owner"] = grantor.title()
            enriched += 1

        # Process remaining
        for i, rec in enumerate(needs[1:], 1):
            grantor, address, _ = fetch_doc_via_browser(driver, rec["internal_id"])
            if grantor:
                rec["owner"] = grantor.title()
                enriched += 1
            if address and len(address) > 5:
                rec["address"] = address
            if (i+1) % 20 == 0:
                log.info(f"  {i+1}/{len(needs)} | {enriched} named")
            time.sleep(0.3)

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
    log.info("Nueces County Lead Scraper v2.1")
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
