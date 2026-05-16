"""
Nueces County Motivated Seller Lead Scraper v1.2
v1.2: Remove false-positive no-results detection
      Better row extraction for Nueces PublicSearch HTML
      Log page source snippet for debugging
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
    """Parse M/D/YYYY or YYYY-MM-DD to date object, return None on fail"""
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
                    EC.presence_of_element_located((By.CSS_SELECTOR, "table tr, .result-document, [data-testid]"))
                )
                time.sleep(2)
            except Exception as e:
                log.warning(f"Page load issue at offset {offset}: {e}")
                # Don't break — try to parse whatever loaded
                time.sleep(3)

            src = driver.page_source

            # Log snippet for debugging first page
            if offset == 0:
                # Find a chunk around "result" or table tags
                snippet_match = re.search(r'<(?:table|tbody|tr)[^>]*>(.{0,500})', src, re.DOTALL)
                if snippet_match:
                    log.info(f"Page snippet: {snippet_match.group(0)[:300].replace(chr(10),' ')}")
                else:
                    log.info(f"No table found. Body snippet: {src[2000:2500].replace(chr(10),' ')}")

            # Log result count hint
            count_match = re.search(r'(\d[\d,]*)\s*(?:of\s*\d[\d,]*)?\s*results?', src, re.IGNORECASE)
            if count_match:
                log.info(f"Result count: {count_match.group(0)}")

            # ── Extract records ──────────────────────────────────────────────
            page_records = []

            # Method 1: JSON from __NEXT_DATA__
            json_match = re.search(
                r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
                src, re.DOTALL)
            if json_match:
                try:
                    nd = json.loads(json_match.group(1))
                    # Walk down props → pageProps → results/documents
                    pp = nd.get("props",{}).get("pageProps",{})
                    results = (pp.get("results") or pp.get("documents") or
                               pp.get("searchResults") or
                               nd.get("props",{}).get("initialState",{}).get("results",[]) or [])
                    log.info(f"__NEXT_DATA__ results: {len(results)}")
                    for r in results:
                        doc_num = str(r.get("documentNumber") or r.get("docNumber") or r.get("id") or "")
                        if not doc_num or doc_num in known_docs:
                            continue
                        page_records.append({
                            "doc_number":  doc_num,
                            "type":        "NOF",
                            "source":      "publicsearch",
                            "county":      "nueces",
                            "address":     r.get("propertyAddress") or r.get("address") or "N/A",
                            "city":        "CORPUS CHRISTI",
                            "zip":         r.get("zip") or "",
                            "owner":       r.get("grantorName") or r.get("grantor") or "",
                            "date_filed":  (r.get("instrumentDate") or r.get("recordDate") or "")[:10],
                            "sale_date":   r.get("saleDate") or "",
                        })
                except Exception as e:
                    log.debug(f"__NEXT_DATA__ parse error: {e}")

            # Method 2: __REDUX_STATE__ or similar
            if not page_records:
                for state_var in ["__REDUX_STATE__","__APP_STATE__","__INITIAL_STATE__"]:
                    sm = re.search(rf'window\.{state_var}\s*=\s*(\{{.*?\}})\s*;', src, re.DOTALL)
                    if sm:
                        try:
                            state = json.loads(sm.group(1))
                            results = (state.get("search",{}).get("results",[]) or
                                       state.get("results",{}).get("data",[]) or [])
                            log.info(f"{state_var} results: {len(results)}")
                            for r in results:
                                doc_num = str(r.get("documentNumber") or r.get("docNumber") or "")
                                if doc_num and doc_num not in known_docs:
                                    page_records.append({
                                        "doc_number": doc_num, "type": "NOF",
                                        "source": "publicsearch", "county": "nueces",
                                        "address": r.get("propertyAddress") or "N/A",
                                        "city": "CORPUS CHRISTI", "zip": r.get("zip",""),
                                        "owner": r.get("grantorName",""),
                                        "date_filed": (r.get("instrumentDate") or r.get("recordDate",""))[:10],
                                        "sale_date": r.get("saleDate",""),
                                    })
                        except Exception:
                            pass
                        break

            # Method 3: HTML table rows
            if not page_records:
                rows = re.findall(r'<tr[^>]*>(.*?)</tr>', src, re.DOTALL | re.IGNORECASE)
                log.info(f"Table rows found: {len(rows)}")
                for row in rows:
                    if re.search(r'<th|thead|DOC.TYPE|RECORDED.DATE', row, re.IGNORECASE):
                        continue
                    cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
                    cells = [re.sub(r'<[^>]+>', '', c).strip() for c in cells if c.strip()]
                    if len(cells) < 3:
                        continue
                    # Find doc number — 10-digit numeric
                    doc_num = next((c for c in cells if re.match(r'^\d{9,12}$', c)), "")
                    if not doc_num or doc_num in known_docs:
                        continue
                    dates = [c for c in cells if re.match(r'^\d{1,2}/\d{1,2}/\d{4}$', c)]
                    page_records.append({
                        "doc_number":  doc_num, "type": "NOF",
                        "source":      "publicsearch", "county": "nueces",
                        "address":     next((c for c in cells if re.search(r'\d+\s+[A-Z]', c) and len(c) > 8), "N/A"),
                        "city":        "CORPUS CHRISTI", "zip": "",
                        "owner":       "",
                        "date_filed":  dates[0] if dates else "",
                        "sale_date":   dates[1] if len(dates) > 1 else "",
                    })

            # Method 4: data-docid or href links containing doc numbers
            if not page_records:
                doc_links = re.findall(r'(?:data-docid|/results/(\d{8,12})|href="[^"]*?(\d{8,12})[^"]*")', src)
                log.info(f"Doc links found: {len(doc_links)}")

            log.info(f"offset={offset} | {len(page_records)} records extracted")

            page_new = 0
            for rec in page_records:
                doc = rec["doc_number"]
                if doc not in known_docs:
                    known_docs.add(doc)
                    flags = []
                    days = None
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

            log.info(f"offset={offset} | {page_new} new added | {len(new_records)} total")

            if len(page_records) == 0:
                consecutive_empty += 1
            else:
                consecutive_empty = 0

            if consecutive_empty >= 2:
                log.info("2 empty pages — stopping")
                break
            if len(page_records) > 0 and len(page_records) < 50:
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
    log.info("Nueces County Lead Scraper v1.2")
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

    for r in records:
        s = 5
        if r.get("days_until_sale") is not None:
            if r["days_until_sale"] <= 14: s += 2
            elif r["days_until_sale"] <= 30: s += 1
        r["score"] = min(s, 10)

    new_ct  = sum(1 for r in records if r.get("is_new"))
    urgent  = sum(1 for r in records if "URGENT" in r.get("flags",[]))
    log.info(f"Final: {len(records)} total | {new_ct} new | {urgent} URGENT")
    write_records(records)
    log.info(f"Dashboard: {len(records)} records, {RECORDS_PATH.stat().st_size:,} bytes")
    log.info("Done.")

if __name__ == "__main__":
    main()
