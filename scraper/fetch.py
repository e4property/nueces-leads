"""
Nueces County Motivated Seller Lead Scraper v1.7
v1.7: Use PublicSearch search API to get internal doc IDs by doc number
      Then hit /api/documents/{id} for party names
      GET /api/search?department=FC&documentNumbers[]=2026000292
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

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": f"{PUBLICSEARCH_BASE}/",
}

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

def api_get(path, params=None):
    """Simple GET to PublicSearch API."""
    url = f"{PUBLICSEARCH_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except Exception as e:
        log.debug(f"API GET {path} error: {e}")
        return None

def api_post(path, payload):
    """POST to PublicSearch API."""
    url = f"{PUBLICSEARCH_BASE}{path}"
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={
            **HEADERS,
            "Content-Type": "application/json",
        })
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except Exception as e:
        log.debug(f"API POST {path} error: {e}")
        return None

def get_internal_id_for_doc(doc_number):
    """
    Try multiple API patterns to get internal ID for a doc number.
    Returns internal_id string or empty string.
    """
    # Pattern 1: search by document number
    endpoints = [
        f"/api/search?department=FC&documentNumbers[]={doc_number}&take=1",
        f"/api/search?department=FC&instNum={doc_number}&take=1",
        f"/api/documents?department=FC&instNum={doc_number}",
        f"/api/search?instNum={doc_number}&take=1",
    ]
    for ep in endpoints:
        result = api_get(ep)
        if result:
            # Try to find ID in response
            docs = (result.get("hits") or result.get("documents") or
                    result.get("results") or result.get("data") or [])
            if isinstance(docs, list) and docs:
                doc = docs[0]
                iid = str(doc.get("id") or doc.get("_id") or doc.get("docId") or "")
                if iid and iid.isdigit():
                    return iid
            elif isinstance(docs, dict):
                iid = str(docs.get("id") or docs.get("_id") or "")
                if iid and iid.isdigit():
                    return iid
            log.debug(f"Pattern {ep}: got result but no ID. Keys: {list(result.keys()) if isinstance(result, dict) else type(result)}")
    return ""

def fetch_doc_data(internal_id):
    """Fetch full doc data from /api/documents/{id}."""
    result = api_get(f"/api/documents/{internal_id}")
    if not result:
        return {}, ""
    # Try to extract grantor
    grantor = ""
    parties = (result.get("parties") or
               result.get("data", {}).get("parties") or [])
    for p in parties:
        role = (p.get("type") or p.get("role") or "").lower()
        name = (p.get("name") or p.get("fullName") or "").strip()
        if name and (not role or role in ("grantor","mortgagor","trustor","borrower","debtor")):
            grantor = name
            if role:  # prefer typed role
                break
    # Address
    address = ""
    addrs = result.get("propAddress") or result.get("propertyAddresses") or []
    if isinstance(addrs, list) and addrs:
        a = addrs[0]
        parts = [a.get("address1",""), a.get("city",""), a.get("state",""), a.get("zip","")]
        address = " ".join(p for p in parts if p).strip()
    return result, grantor, address

def scrape_publicsearch_selenium(known_docs):
    """Scrape search results page for doc numbers, dates, addresses."""
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

            # Log ALL href patterns on first page for debugging
            if offset == 0:
                all_hrefs = re.findall(r'href=["\']([^"\']{3,50})["\']', src)
                doc_hrefs = [h for h in all_hrefs if re.search(r'\d{5,}', h)]
                log.info(f"Sample hrefs with numbers: {doc_hrefs[:10]}")

                # Also check for data-id, data-docid attributes
                data_attrs = re.findall(r'data-(?:id|docid|document-id|inst-num)=["\']([^"\']+)["\']', src, re.IGNORECASE)
                log.info(f"data-id attrs: {data_attrs[:10]}")

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
                    "internal_id": "",
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

    return new_records

def enrich_records(records):
    """
    For records missing owner names, try API endpoints to get internal ID
    then fetch party data. Tests multiple endpoint patterns.
    """
    bad = {'Window.', 'Window', 'Search Results', 'Nueces County', 'Document Preview'}
    for r in records:
        if r.get("owner","") in bad:
            r["owner"] = ""

    needs = [r for r in records if not r.get("owner")][:30]  # test first 30
    log.info(f"Testing API enrichment on {len(needs)} records...")

    if not needs:
        log.info("All records already have owner names")
        return 0

    # First — test what API endpoints are available
    test_doc = needs[0]["doc_number"]
    log.info(f"Testing API patterns for doc {test_doc}...")

    test_patterns = [
        f"/api/search?department=FC&documentNumbers[]={test_doc}&take=1",
        f"/api/search?department=FC&instNum={test_doc}&take=1",
        f"/api/search?instNum={test_doc}&take=1",
        f"/api/search?documentNumber={test_doc}",
        f"/api/documents?instNum={test_doc}",
        f"/api/fc/search?instNum={test_doc}",
    ]
    working_pattern = None
    for pat in test_patterns:
        result = api_get(pat)
        if result is not None:
            log.info(f"  FOUND endpoint: {pat} → keys: {list(result.keys()) if isinstance(result,dict) else type(result).__name__}")
            working_pattern = pat
            break
        else:
            log.info(f"  MISS: {pat}")

    if not working_pattern:
        log.info("No working API pattern found — skipping enrichment")
        return 0

    enriched = 0
    for i, rec in enumerate(needs):
        doc_num = rec["doc_number"]
        iid = get_internal_id_for_doc(doc_num)
        if iid:
            _, grantor, address = fetch_doc_data(iid)
            if grantor:
                rec["owner"] = grantor.title()
                rec["internal_id"] = iid
                enriched += 1
            if address and address != "N/A":
                rec["address"] = address
        time.sleep(0.4)

    log.info(f"API enrichment: {enriched}/{len(needs)} named")
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
    log.info("Nueces County Lead Scraper v1.7")
    log.info(f"Cutoff: {CUTOFF} (21 days) | Today: {TODAY}")
    log.info("=" * 60)

    known_docs, prev_records = load_known_docs()
    new_records = scrape_publicsearch_selenium(known_docs)

    for r in prev_records:
        r["is_new"] = False

    seen = {}
    for r in new_records + prev_records:
        doc = r.get("doc_number","")
        if doc and doc not in seen:
            seen[doc] = r
    records = list(seen.values())
    log.info(f"After merge: {len(records)} total")

    enrich_records(records)

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
