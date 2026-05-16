"""
Nueces County Motivated Seller Lead Scraper v1.8
v1.8: Use Selenium execute_script to get /doc/{id} links from rendered React DOM
      Then visit each detail page with Selenium and extract party names
      from the rendered React component (not raw HTML)
"""

import json, logging, re, time, urllib.request
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
ENRICH_LIMIT      = 249

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

def get_doc_links_from_page(driver):
    """Use JS to get all /doc/{id} links from rendered React DOM."""
    try:
        links = driver.execute_script(
            "return Array.from(document.querySelectorAll('a[href*=\"/doc/\"]')).map(a => a.href);"
        )
        ids = []
        for link in (links or []):
            m = re.search(r'/doc/(\d+)', link)
            if m:
                ids.append(m.group(1))
        return ids
    except Exception as e:
        log.debug(f"execute_script error: {e}")
        return []

def get_party_from_detail(driver, internal_id, timeout=20):
    """
    Visit /doc/{id} and extract party names from rendered React DOM.
    The React app renders party info into the DOM — use JS to extract it.
    """
    url = f"{PUBLICSEARCH_BASE}/doc/{internal_id}"
    try:
        driver.get(url)
        # Wait for the summary panel to render
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.common.by import By
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR,
                "[class*='summary'], [class*='detail'], [class*='party'], table"))
        )
        time.sleep(2)  # let React fully hydrate

        # Try JS to extract from Redux store
        grantor = driver.execute_script("""
            try {
                // Try Redux store
                var stores = Object.values(window).filter(v => v && v.getState);
                for (var s of stores) {
                    var state = s.getState();
                    var parties = state && state.docPreview && state.docPreview.document &&
                                  state.docPreview.document.data && state.docPreview.document.data.parties;
                    if (parties && parties.length > 0) {
                        return parties[0].name || '';
                    }
                }
                // Try window.__data
                if (window.__data && window.__data.docPreview) {
                    var dp = window.__data.docPreview;
                    if (dp.document && dp.document.data && dp.document.data.parties) {
                        return dp.document.data.parties[0].name || '';
                    }
                }
                return '';
            } catch(e) { return ''; }
        """)

        if grantor:
            return grantor.strip()

        # Fallback: extract from rendered DOM text
        src = driver.page_source

        # Look for party section in rendered HTML
        party_m = re.search(
            r'(?:Parties?|Grantor|Mortgagor)[^<]*</[^>]+>\s*(?:<[^>]+>\s*)*([A-Z][A-Z ,&\.\'\-]{5,60})',
            src, re.IGNORECASE)
        if party_m:
            candidate = party_m.group(1).strip()
            bad = {'Window', 'Document', 'Preview', 'Nueces', 'Search', 'Summary'}
            if ' ' in candidate and not any(b in candidate for b in bad):
                return candidate

        # Try to find name in __data embedded in page
        data_m = re.search(r'window\.__data\s*=\s*(\{.*?\});\s*</script>', src, re.DOTALL)
        if data_m:
            try:
                data = json.loads(data_m.group(1))
                dp = data.get("docPreview", {})
                doc_data = dp.get("document", {}).get("data", {})
                parties = doc_data.get("parties", [])
                if parties:
                    return parties[0].get("name", "").strip()
            except Exception:
                pass

        return ""
    except Exception as e:
        log.debug(f"Detail fetch error for {internal_id}: {e}")
        return ""

def get_address_from_detail(driver):
    """Extract property address from already-loaded detail page."""
    try:
        src = driver.page_source
        # propAddress section
        addr_m = re.search(
            r'Property Address[^<]*</[^>]+>\s*(?:<[^>]+>\s*)*(\d+[^<]{5,60})',
            src, re.IGNORECASE)
        if addr_m:
            return re.sub(r'<[^>]+>', '', addr_m.group(1)).strip()

        # From __data
        data_m = re.search(r'window\.__data\s*=\s*(\{.*?\});\s*</script>', src, re.DOTALL)
        if data_m:
            try:
                data = json.loads(data_m.group(1))
                dp = data.get("docPreview", {})
                doc_data = dp.get("document", {}).get("data", {})
                addrs = doc_data.get("propAddress", [])
                if addrs:
                    a = addrs[0]
                    parts = [a.get("address1",""), a.get("city",""), a.get("state","")]
                    return " ".join(p for p in parts if p).strip()
            except Exception:
                pass
    except Exception:
        pass
    return ""

def scrape_publicsearch(known_docs):
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    new_records = []
    doc_id_map = {}  # doc_number -> internal_id
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
                # Wait for React to render the table rows
                WebDriverWait(driver, 30).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "table tr td")))
                time.sleep(3)  # extra wait for React hydration + doc links
            except Exception as e:
                log.warning(f"Timeout at offset {offset}: {e}")
                time.sleep(3)

            src = driver.page_source

            # Get doc detail links from rendered React DOM
            internal_ids = get_doc_links_from_page(driver)
            log.info(f"Doc links from React DOM: {len(internal_ids)}")

            count_m = re.search(r'(\d[\d,]*)\s*of\s*(\d[\d,]*)\s*results?', src, re.IGNORECASE)
            if count_m:
                log.info(f"Results: {count_m.group(0)}")

            page_records = []
            rows = re.findall(r'<tr[^>]*>(.*?)</tr>', src, re.DOTALL | re.IGNORECASE)
            row_data_idx = 0
            for row in rows:
                if re.search(r'<th|thead|DOC.TYPE|RECORDED.DATE', row, re.IGNORECASE):
                    continue
                cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
                cells = [re.sub(r'<[^>]+>', '', c).strip() for c in cells if c.strip()]
                if len(cells) < 3:
                    continue
                doc_num = next((c for c in cells if re.match(r'^\d{9,12}$', c.strip())), "")
                if not doc_num or doc_num in known_docs:
                    row_data_idx += 1
                    continue
                dates = [c for c in cells if re.match(r'^\d{1,2}/\d{1,2}/\d{4}$', c.strip())]
                address = next((c for c in cells
                    if len(c) > 8 and c not in dates
                    and not re.match(r'^\d{9,12}$', c)
                    and 'FORECLOSURE' not in c.upper()
                    and c.upper() not in ('N/A','')), "N/A")

                # Map internal ID to doc number
                iid = internal_ids[row_data_idx] if row_data_idx < len(internal_ids) else ""
                if iid and doc_num:
                    doc_id_map[doc_num] = iid

                page_records.append({
                    "doc_number": doc_num, "internal_id": iid,
                    "type": "NOF", "source": "publicsearch", "county": "nueces",
                    "address": address, "city": "CORPUS CHRISTI", "zip": "",
                    "owner": "", "date_filed": dates[0] if dates else "",
                    "sale_date": dates[1] if len(dates) > 1 else "",
                })
                row_data_idx += 1

            log.info(f"offset={offset} | {len(page_records)} extracted | {len(doc_id_map)} with internal IDs")

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
                break
            if 0 < len(page_records) < 50:
                break
            offset += 50
            time.sleep(1.5)

        # ── Enrich records with party names via detail pages ──────────────────
        # For existing records, restore their internal_ids from doc_id_map
        all_needs_enrichment = []  # will be filled after merge in main()

        log.info(f"doc_id_map built: {len(doc_id_map)} entries")

    except Exception as e:
        log.error(f"Scraper error: {e}", exc_info=True)
    finally:
        if driver:
            try: driver.quit()
            except Exception: pass

    return new_records, doc_id_map

def enrich_with_selenium(records, doc_id_map):
    """Visit detail pages for records missing owner names."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    # Update internal_ids from doc_id_map
    for r in records:
        if not r.get("internal_id") and r.get("doc_number") in doc_id_map:
            r["internal_id"] = doc_id_map[r["doc_number"]]

    bad = {'Window.','Window','Search Results','Nueces County','Document Preview',''}
    needs = [r for r in records if r.get("owner","") in bad or not r.get("owner")]
    needs_with_id = [r for r in needs if r.get("internal_id")][:ENRICH_LIMIT]

    log.info(f"Enrichment: {len(needs)} need names | {len(needs_with_id)} have internal IDs")

    if not needs_with_id:
        log.info("No records with internal IDs to enrich — skipping")
        return 0

    driver = None
    enriched = 0
    try:
        driver = get_driver()
        for i, rec in enumerate(needs_with_id):
            grantor = get_party_from_detail(driver, rec["internal_id"])
            if grantor:
                rec["owner"] = grantor.title()
                enriched += 1
            address = get_address_from_detail(driver)
            if address and len(address) > 5 and address != "N/A":
                rec["address"] = address
            if (i+1) % 10 == 0:
                log.info(f"  Enrichment: {i+1}/{len(needs_with_id)} | {enriched} named")
            time.sleep(0.5)
    except Exception as e:
        log.error(f"Enrichment error: {e}")
    finally:
        if driver:
            try: driver.quit()
            except Exception: pass

    log.info(f"Enrichment complete: {enriched}/{len(needs_with_id)} named")
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
    log.info("Nueces County Lead Scraper v1.8")
    log.info(f"Cutoff: {CUTOFF} (21 days) | Today: {TODAY}")
    log.info("=" * 60)

    known_docs, prev_records = load_known_docs()
    new_records, doc_id_map = scrape_publicsearch(known_docs)

    for r in prev_records:
        r["is_new"] = False

    seen = {}
    for r in new_records + prev_records:
        doc = r.get("doc_number","")
        if doc and doc not in seen:
            seen[doc] = r
    records = list(seen.values())
    log.info(f"After merge: {len(records)} total | doc_id_map: {len(doc_id_map)} entries")

    enrich_with_selenium(records, doc_id_map)

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
