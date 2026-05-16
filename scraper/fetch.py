"""
Nueces County Motivated Seller Lead Scraper v1.9
v1.9: Build doc_number -> internal_id map using doc number range search
      Load results page with doc range, wait for React, get all /doc/ links
      Then visit each detail page for party names
      Separate pass from known_docs check so existing records get enriched
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
ENRICH_LIMIT      = 50  # enrich 50 per run, builds up over time

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

def wait_for_react(driver, timeout=15):
    """Wait for React to fully render the page."""
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.common.by import By
    try:
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "table tr td, .no-results")))
        time.sleep(3)  # extra wait for React hydration and link rendering
    except Exception:
        time.sleep(5)

def get_doc_id_map_from_range(driver, doc_nums):
    """
    Load search results for a specific doc number range.
    Extract /doc/{id} links from the rendered React DOM.
    Returns dict: doc_number_str -> internal_id_str
    """
    if not doc_nums:
        return {}

    doc_nums_sorted = sorted(doc_nums)
    min_doc = doc_nums_sorted[0]
    max_doc = doc_nums_sorted[-1]

    url = (f"{PUBLICSEARCH_BASE}/results"
           f"?department=FC"
           f"&documentNumberRange[]={min_doc}"
           f"&documentNumberRange[]={max_doc}")

    log.info(f"Loading doc range search: {min_doc} to {max_doc}")
    driver.get(url)
    wait_for_react(driver, timeout=20)

    # Get all /doc/ links via JS
    links = driver.execute_script(
        "return Array.from(document.querySelectorAll('a[href*=\"/doc/\"]')).map(a => ({href: a.href, text: a.innerText}));"
    ) or []
    log.info(f"React DOM doc links found: {len(links)}")

    # Also check page source for doc numbers in table rows
    src = driver.page_source

    # Build map: for each result row, extract doc_num and internal_id
    result_map = {}

    # Try to pair internal IDs from links with doc numbers from table
    internal_ids = []
    for link in links:
        href = link.get("href","")
        m = re.search(r'/doc/(\d+)', href)
        if m:
            internal_ids.append(m.group(1))

    # Get doc numbers from table in order
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', src, re.DOTALL | re.IGNORECASE)
    table_doc_nums = []
    for row in rows:
        if re.search(r'<th|thead|DOC.TYPE|RECORDED.DATE', row, re.IGNORECASE):
            continue
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
        cells = [re.sub(r'<[^>]+>', '', c).strip() for c in cells if c.strip()]
        doc_num = next((c for c in cells if re.match(r'^\d{9,12}$', c.strip())), "")
        if doc_num:
            table_doc_nums.append(doc_num)

    log.info(f"Table doc nums: {len(table_doc_nums)} | Internal IDs: {len(internal_ids)}")

    # Pair them up
    for i, (doc_num, iid) in enumerate(zip(table_doc_nums, internal_ids)):
        result_map[doc_num] = iid

    log.info(f"doc_id_map from range search: {len(result_map)} entries")
    if result_map:
        sample = list(result_map.items())[:3]
        log.info(f"Sample: {sample}")

    return result_map

def get_party_from_detail_page(driver, internal_id):
    """Visit /doc/{id} and extract party names from rendered React DOM."""
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.common.by import By

    url = f"{PUBLICSEARCH_BASE}/doc/{internal_id}"
    try:
        driver.get(url)
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.CSS_SELECTOR,
                "[class*='summary'], table, h1, [class*='detail']")))
        time.sleep(2.5)

        # Try JS to get party from Redux store or window.__data
        grantor = driver.execute_script("""
            try {
                // Check window.__data which is set on page load
                if (window.__data) {
                    var dp = window.__data.docPreview;
                    if (dp && dp.document && dp.document.data) {
                        var parties = dp.document.data.parties || [];
                        for (var p of parties) {
                            if (p && p.name) return p.name;
                        }
                    }
                }
                // Check Redux store
                var reduxKey = Object.keys(window).find(k => window[k] && window[k].getState);
                if (reduxKey) {
                    var state = window[reduxKey].getState();
                    var dp2 = state.docPreview;
                    if (dp2 && dp2.document && dp2.document.data) {
                        var parties2 = dp2.document.data.parties || [];
                        for (var p2 of parties2) {
                            if (p2 && p2.name) return p2.name;
                        }
                    }
                }
                // Try rendered DOM text
                var partyEls = document.querySelectorAll('[class*="party"], [class*="grantor"], [class*="name"]');
                for (var el of partyEls) {
                    var txt = el.innerText && el.innerText.trim();
                    if (txt && txt.length > 5 && txt.length < 60 && /[A-Z]/.test(txt) && !/Parties|Grantor|Name/.test(txt)) {
                        return txt;
                    }
                }
                return '';
            } catch(e) { return 'ERR:'+e.message; }
        """)

        if grantor and not grantor.startswith('ERR:'):
            return grantor.strip()

        # Final fallback: parse rendered page source
        src = driver.page_source
        data_m = re.search(r'"parties"\s*:\s*\[([^\]]{0,500})\]', src)
        if data_m:
            name_m = re.search(r'"name"\s*:\s*"([^"]{3,60})"', data_m.group(1))
            if name_m:
                return name_m.group(1).strip()

    except Exception as e:
        log.debug(f"Detail error for {internal_id}: {e}")
    return ""

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
            url = (f"{PUBLICSEARCH_BASE}/results"
                   f"?department=FC"
                   f"&instrumentDateRange={cutoff_str}%2C{today_str}"
                   f"&keywordSearch=false&offset={offset}")
            log.info(f"Fetching offset={offset}")
            try:
                driver.get(url)
                wait_for_react(driver)
            except Exception as e:
                log.warning(f"Load issue at offset {offset}: {e}")

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

            log.info(f"offset={offset} | {len(page_records)} new extracted")
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

    log.info(f"Scrape complete: {len(new_records)} new records")
    return new_records

def build_id_map_and_enrich(records):
    """
    For records missing owner names:
    1. Use doc number range search to get internal IDs
    2. Visit each detail page to get party names
    """
    bad = {'Window.','Window','Search Results','Nueces County','Document Preview'}
    for r in records:
        if r.get("owner","") in bad:
            r["owner"] = ""

    needs = [r for r in records if not r.get("owner")]
    log.info(f"Records needing owner names: {len(needs)}")
    if not needs:
        return 0

    # Get doc numbers we need IDs for
    doc_nums = [r["doc_number"] for r in needs if r.get("doc_number")]
    log.info(f"Building internal ID map for {len(doc_nums)} records...")

    driver = None
    enriched = 0
    try:
        driver = get_driver()

        # Build the map using doc number range search
        doc_id_map = get_doc_id_map_from_range(driver, doc_nums)

        # Update records with internal IDs
        for r in needs:
            if r["doc_number"] in doc_id_map:
                r["internal_id"] = doc_id_map[r["doc_number"]]

        # Enrich records that now have internal IDs
        has_id = [r for r in needs if r.get("internal_id")][:ENRICH_LIMIT]
        log.info(f"Enriching {len(has_id)} records with internal IDs...")

        for i, rec in enumerate(has_id):
            grantor = get_party_from_detail_page(driver, rec["internal_id"])
            if grantor:
                rec["owner"] = grantor.title()
                enriched += 1
            if (i+1) % 10 == 0:
                log.info(f"  Enriched: {i+1}/{len(has_id)} | {enriched} named")
            time.sleep(0.5)

    except Exception as e:
        log.error(f"Enrichment error: {e}")
    finally:
        if driver:
            try: driver.quit()
            except Exception: pass

    log.info(f"Enrichment complete: {enriched} named")
    return enriched

def load_known_docs():
    try:
        existing = json.loads(RECORDS_PATH.read_text())
        log.info(f"Loaded {len(existing)} existing records")
        return {r["doc_number"] for r in existing if r.get("doc_number")}, existing
    except Exception:
        log.info("No existing records")
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
    log.info("Nueces County Lead Scraper v1.9")
    log.info(f"Cutoff: {CUTOFF} (21 days) | Today: {TODAY}")
    log.info("=" * 60)

    known_docs, prev_records = load_known_docs()
    new_records = scrape_publicsearch(known_docs)

    for r in prev_records:
        r["is_new"] = False

    seen = {}
    for r in new_records + prev_records:
        doc = r.get("doc_number","")
        if doc and doc not in seen:
            seen[doc] = r
    records = list(seen.values())
    log.info(f"After merge: {len(records)} total")

    build_id_map_and_enrich(records)

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
