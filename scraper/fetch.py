"""
Nueces County Motivated Seller Lead Scraper v1.10 - DIAGNOSTIC
Logs ALL links and clickable elements from rendered page to find doc ID pattern
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

def diagnostic_page(driver, url):
    """Load page and log everything useful for finding doc IDs."""
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.common.by import By

    driver.get(url)
    try:
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "table tr td")))
        time.sleep(4)
    except Exception:
        time.sleep(6)

    # 1. All anchor hrefs
    hrefs = driver.execute_script("return Array.from(document.querySelectorAll('a')).map(a => a.href).filter(h => h && !h.includes('fonts') && !h.includes('.css') && !h.includes('.js'));")
    log.info(f"All anchor hrefs: {hrefs[:20]}")

    # 2. All data attributes on table rows/cells
    data_attrs = driver.execute_script("""
        var els = document.querySelectorAll('tr, td, [data-id], [data-docid], [data-doc]');
        var attrs = [];
        for (var el of els) {
            var d = {};
            for (var a of el.attributes) {
                if (a.name.startsWith('data-') || a.name === 'id') {
                    d[a.name] = a.value;
                }
            }
            if (Object.keys(d).length > 0) attrs.push(d);
        }
        return attrs.slice(0, 20);
    """)
    log.info(f"Data attrs on rows/cells: {data_attrs}")

    # 3. Window __data docPreview
    doc_preview = driver.execute_script("try { return JSON.stringify(window.__data && window.__data.docPreview); } catch(e) { return null; }")
    log.info(f"window.__data.docPreview: {str(doc_preview)[:200] if doc_preview else 'none'}")

    # 4. Window __data workspaces
    workspaces = driver.execute_script("try { return JSON.stringify(window.__data && window.__data.workspaces); } catch(e) { return null; }")
    log.info(f"workspaces keys: {str(workspaces)[:300] if workspaces else 'none'}")

    # 5. Search state
    search_state = driver.execute_script("""
        try {
            var ws = window.__data && window.__data.workspaces;
            if (!ws) return null;
            var tabs = ws.tabs;
            if (!tabs) return null;
            var keys = Object.keys(tabs);
            for (var k of keys) {
                var tab = tabs[k];
                if (tab && tab.searchResults) return JSON.stringify(tab.searchResults).slice(0, 500);
                if (tab && tab.results) return JSON.stringify(tab.results).slice(0, 500);
            }
            return JSON.stringify(ws).slice(0, 300);
        } catch(e) { return 'err:'+e.message; }
    """)
    log.info(f"Search results in state: {search_state}")

    # 6. Check if Redux store is available
    redux = driver.execute_script("""
        try {
            var stores = Object.keys(window).filter(k => window[k] && typeof window[k].getState === 'function');
            if (stores.length === 0) return 'no stores';
            var state = window[stores[0]].getState();
            var keys = Object.keys(state);
            return 'store keys: ' + keys.join(', ');
        } catch(e) { return 'err:'+e.message; }
    """)
    log.info(f"Redux: {redux}")

    # 7. Look for document IDs in page source directly
    src = driver.page_source
    # Find any large numbers that could be internal IDs (7-9 digits)
    large_nums = re.findall(r'\b(3\d{8})\b', src)
    unique_large = list(dict.fromkeys(large_nums))[:10]
    log.info(f"Large nums (3xxxxxxxx pattern) in src: {unique_large}")

    # 8. Find instNum or documentId patterns in page source
    inst_nums = re.findall(r'"(?:id|instNum|documentId|docId)"\s*:\s*(\d{6,12})', src)
    log.info(f"id/instNum in JSON: {inst_nums[:10]}")

def scrape_publicsearch(known_docs):
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.common.by import By

    new_records = []
    driver = None
    cutoff_str = CUTOFF.strftime("%Y%m%d")
    today_str  = TODAY.strftime("%Y%m%d")

    try:
        driver = get_driver()
        offset = 0
        consecutive_empty = 0
        first_page = True

        while True:
            url = (f"{PUBLICSEARCH_BASE}/results"
                   f"?department=FC"
                   f"&instrumentDateRange={cutoff_str}%2C{today_str}"
                   f"&keywordSearch=false&offset={offset}")
            log.info(f"Fetching offset={offset}")

            driver.get(url)
            try:
                WebDriverWait(driver, 30).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "table tr td")))
                time.sleep(3)
            except Exception:
                time.sleep(5)

            # Run diagnostic on first page
            if first_page:
                log.info("=== DIAGNOSTIC PAGE 1 ===")
                diagnostic_page(driver, url)
                log.info("=== END DIAGNOSTIC ===")
                first_page = False
                # Re-navigate since diagnostic may have changed state
                driver.get(url)
                try:
                    WebDriverWait(driver, 30).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, "table tr td")))
                    time.sleep(3)
                except Exception:
                    time.sleep(5)

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
                    "doc_number": doc_num, "internal_id": "",
                    "type": "NOF", "source": "publicsearch", "county": "nueces",
                    "address": address, "city": "CORPUS CHRISTI", "zip": "",
                    "owner": "", "date_filed": dates[0] if dates else "",
                    "sale_date": dates[1] if len(dates) > 1 else "",
                })

            log.info(f"offset={offset} | {len(page_records)} new")
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

            if len(page_records) == 0:
                consecutive_empty += 1
            else:
                consecutive_empty = 0
            if consecutive_empty >= 2 or (0 < len(page_records) < 50):
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
    log.info("Nueces County Lead Scraper v1.10 DIAGNOSTIC")
    log.info(f"Cutoff: {CUTOFF} | Today: {TODAY}")
    log.info("=" * 60)

    known_docs, prev_records = load_known_docs()
    # Clear known_docs so we force a fresh page load with diagnostic
    known_docs_backup = set(known_docs)
    known_docs_empty = set()  # Use empty set so first page loads with content

    new_records = scrape_publicsearch(known_docs_empty)

    for r in prev_records:
        r["is_new"] = False

    seen = {}
    for r in new_records + prev_records:
        doc = r.get("doc_number","")
        if doc and doc not in seen:
            seen[doc] = r
    records = list(seen.values())

    named = sum(1 for r in records if r.get("owner"))
    urgent = sum(1 for r in records if "URGENT" in r.get("flags",[]))
    log.info(f"Final: {len(records)} total | {named} named | {urgent} URGENT")
    write_records(records)
    log.info("Done.")

if __name__ == "__main__":
    main()
