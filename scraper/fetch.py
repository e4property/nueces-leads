"""
Nueces County Motivated Seller Lead Scraper v1.2
County: Nueces (Corpus Christi, TX)
Source 1: nueces.tx.publicsearch.us — FC dept (NOF/TAX foreclosures)
Source 2: nueces.tx.publicsearch.us — RP dept (Appointment of Substitute Trustee / Pre-Fore)
Source 3: Corpus Christi Code Compliance cases (open violations)
Enrichment: Nueces CAD appraisal roll lookup CSV (legal desc → address/owner/value)
GHL tags: nueces_lead, nueces_prefore, nueces_ce
Scrape schedule: Mon/Thu 9am + 3pm CST (14:00 + 20:00 UTC)

v1.2 changes (address fallback rewrite):
  - Confirmed via DOM inspection (Aug 3 session): PublicSearch's FC results
    table has NO href, NO data-id, NO static identifier anywhere in the row
    HTML. It's a pure React SPA — the internal doc ID only exists in JS
    state and appears in the URL after a client-side route change from a
    row click. The old /doc/(\\d+) href-scraping fallback (v1.1) could
    never have worked; it silently found nothing on every run.
  - REPLACED fetch_doc_address() (broken href-based) with
    fetch_address_by_click() — reloads the exact results page a row came
    from (stored per-record as _source_url during scraping), locates that
    row by its visible doc number, clicks it, waits for the /doc/ route
    change, then pulls the address out of the doc page header. Works
    regardless of how the site stores its internal IDs.
  - ps_doc_id field kept for backward JSON compatibility but no longer
    populated (the href it looked for doesn't exist) — _source_url is
    the new mechanism, stripped from records before saving to keep
    records.json clean.

v1.1 changes (address fix):
  - FC/NOF/TAX leads were getting 0/N addresses because PROPERTY ADDRESS
    column contains a legal description ("LAMAR PARK SECTION 1 LOT 2"),
    not a street address. Direct-address regex correctly skipped these
    (working as intended) — but roll enrichment was ALSO failing because
    it required an exact string match against nueces_lookup.csv.gz, and
    county rolls abbreviate differently than PublicSearch (SEC vs SECTION,
    BLK vs BLOCK, etc).
  - NEW: parse_legal_components() extracts (subdivision name signature,
    lot, block, section) independent of abbreviation/word order, and
    load_lookup()/enrich_from_lookup() match on that signature.
  - FIXED: NameError in scrape_code_enforcement (undefined `case_id` var).

v1.0 changes:
  - Fresh build, no dependency on Bexar scraper
  - Legal description matching against appraisal roll lookup CSV
  - Auto-purge past auction leads on every run
  - Code enforcement from CC open data
  - Score: base + auction urgency + absentee + LLC flag + coastal zip bonus
"""

import csv
import gzip
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Selenium ──────────────────────────────────────────────────────────────────
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
COUNTY            = "nueces"
PUBLICSEARCH_BASE = "https://nueces.tx.publicsearch.us"
RECORDS_PATH      = Path("dashboard/records.json")
LOOKUP_PATH       = Path("scraper/nueces_lookup.csv.gz")
SCRAPE_DAYS       = 365   # extended for initial Nueces catchup        # rolling window
AGED_DAYS         = 60        # leads older than this = aged
TODAY             = datetime.now(timezone.utc)
TODAY_NAIVE       = datetime.now()

# Cap on Selenium doc-page fetches per run (address fallback) — keeps runtime bounded
MAX_DOC_FETCH     = 60

# Coastal Corpus Christi ZIP codes — premium distress signal
COASTAL_ZIPS = {
    "78401","78402","78403","78404","78405","78406","78407","78408",
    "78409","78410","78411","78412","78413","78414","78415","78416",
    "78417","78418","78419"
}

# Entity keywords — LLC/Corp flag
ENTITY_KW = [
    "LLC","L.L.C","INC","CORP","LTD","TRUST","HOLDINGS","PARTNERS",
    "GROUP","COMPANY"," CO ","BANK","ASSOC","INVESTMENTS","PROPERTIES"
]

# CC Code Compliance open data endpoint
CC_CODE_URL = (
    "https://opendata.arcgis.com/datasets/"
    "b8a8b8f8b8f8b8f8b8f8b8f8b8f8b8f8_0.geojson"
)

# ── Helpers ───────────────────────────────────────────────────────────────────
def is_entity(name):
    if not name:
        return False
    u = name.upper()
    return any(k in u for k in ENTITY_KW)

def is_coastal(zip_code):
    return (zip_code or "")[:5] in COASTAL_ZIPS

def normalize_legal(s):
    if not s:
        return ""
    return re.sub(r"\s+", " ", s.upper().strip())

# ── Legal description signature matching (v1.1) ───────────────────────────────
LOT_RE       = re.compile(r"\bLOT[S]?\.?\s*([0-9A-Z\-]+)")
BLOCK_NUM_RE = re.compile(r"\bBL(?:OC)?K\.?\s*([0-9A-Z\-]+)")
SECTION_RE   = re.compile(r"\bSEC(?:TION)?\.?\s*([0-9A-Z\-]+)")

LEGAL_NOISE_WORDS = {
    "SUBDIVISION","SUBD","ADDITION","ADDN","ADD","UNIT","PHASE","PH",
    "REPLAT","AMENDED","AMD","PLAT","OF","THE","AN","A","AND","INST",
    "NO","NUMBER","RECORDED","VOL","VOLUME","PG","PAGE"
}

def parse_legal_components(s):
    """
    Extract (subdivision_name_signature, lot, block, section) from a legal
    description, independent of abbreviation style (SEC vs SECTION, BLK vs
    BLOCK), punctuation, or word order. Used to match PublicSearch's legal
    description text against the county roll's legal_desc field, which are
    almost never formatted identically even for the same parcel.
    """
    s = (s or "").upper()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()

    lot = None
    m = LOT_RE.search(s)
    if m:
        lot = m.group(1).strip("-")

    block = None
    m = BLOCK_NUM_RE.search(s)
    if m:
        block = m.group(1).strip("-")

    section = None
    m = SECTION_RE.search(s)
    if m:
        section = m.group(1).strip("-")

    # Strip the LOT/BLOCK/SECTION phrases out, whatever's left is the
    # subdivision name — turn it into an order-independent token set.
    name_part = s
    name_part = LOT_RE.sub(" ", name_part)
    name_part = BLOCK_NUM_RE.sub(" ", name_part)
    name_part = SECTION_RE.sub(" ", name_part)
    tokens = [t for t in name_part.split() if len(t) >= 3 and t not in LEGAL_NOISE_WORDS]
    name_sig = frozenset(tokens)

    return name_sig, lot, block, section

def parse_sale_date(raw):
    if not raw:
        return None, None
    raw = raw.strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%Y"):
        try:
            dt = datetime.strptime(raw, fmt)
            days = (dt - TODAY_NAIVE).days
            return raw, days
        except ValueError:
            continue
    return raw, None

def days_until_sale(sale_date_str):
    if not sale_date_str:
        return None
    _, days = parse_sale_date(sale_date_str)
    return days

def score_record(rec):
    s = 0
    if rec.get("address"):    s += 3
    if rec.get("owner"):      s += 3
    if rec.get("type") == "TAX": s += 2
    if rec.get("absentee"):   s += 2
    if rec.get("sale_date"):  s = min(s + 1, 10)
    if rec.get("is_coastal"): s = min(s + 1, 10)
    if rec.get("is_entity"):  s = min(s + 1, 10)
    if rec.get("source") == "code_enforcement":
        s += 1
        if rec.get("ce_status", "").upper() == "OPEN":
            s += 1
    s += rec.get("tenure_score_bonus", 0)
    return min(s, 10)

def years_owned_from_sale_date(raw):
    """
    Parse a roll sale_date (sl_dt) into years-owned. CAD exports vary in
    format (YYYYMMDD, MM/DD/YYYY, YYYY-MM-DD) so try the common ones.
    Returns None if unparseable/blank.
    """
    if not raw:
        return None
    raw = raw.strip()
    for fmt in ("%Y%m%d", "%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"):
        try:
            dt = datetime.strptime(raw, fmt)
            years = (TODAY_NAIVE - dt).days / 365.25
            return round(years, 1) if years >= 0 else None
        except ValueError:
            continue
    return None

def tenure_bonus(yrs):
    if yrs is None: return 0
    if yrs >= 15:   return 15
    if yrs >= 10:   return 10
    if yrs >= 5:    return 5
    return 0

def filed_within_window(date_str, days=SCRAPE_DAYS):
    if not date_str:
        return True
    try:
        parts = date_str.strip().split("/")
        if len(parts) == 3:
            dt = datetime(int(parts[2]), int(parts[0]), int(parts[1]))
        elif len(parts) == 2:
            dt = datetime(int(parts[1]), int(parts[0]), 1)
        else:
            return True
        return (TODAY_NAIVE - dt).days <= days
    except Exception:
        return True

def auction_passed(sale_date_str):
    if not sale_date_str:
        return False
    try:
        parts = sale_date_str.strip().split("/")
        if len(parts) == 3:
            dt = datetime(int(parts[2]), int(parts[0]), int(parts[1]))
            return dt < TODAY_NAIVE
    except Exception:
        pass
    return False

def get_driver():
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1280,900")
    opts.add_argument("user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36")
    svc = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=svc, options=opts)

# ── Doc-page address fallback (v1.2 — click-through) ──────────────────────────
# Matches a header line like "2522 WIDGEON DR CORPUS CHRISTI TX 78410"
DOC_ADDR_LABEL_RE = re.compile(r"Property\s*Address[:\s]*([0-9][^<\n]{5,90})", re.IGNORECASE)
DOC_ADDR_GENERIC_RE = re.compile(
    r"(\d{1,6}\s+[A-Z0-9.\-\/ ]{3,40}?)\s+([A-Z][A-Z .]{2,25}?)\s+TX\s+(\d{5})"
)

def fetch_address_by_click(driver, source_url, doc_number, timeout=20):
    """
    Fallback for FC leads that don't get a roll match. PublicSearch's results
    table is a pure React SPA with NO href/data-id anywhere in the row HTML
    (confirmed via DOM inspection) — the internal doc ID only exists in JS
    state and shows up in the URL after a client-side route change from a
    row click. So instead of scraping a link that doesn't exist, this reloads
    the exact results page the row came from, finds that row by its visible
    doc number, clicks it, waits for the route to land on /doc/<id>, then
    pulls the address out of the doc page header.
    Returns (street, city, zip) or (None, None, None).
    """
    if not source_url or not doc_number:
        return None, None, None
    try:
        driver.get(source_url)
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "table tr td"))
        )
        time.sleep(1)

        row_xpath = f"//tr[.//span[normalize-space(text())='{doc_number}']]"
        row = WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.XPATH, row_xpath))
        )
        clickable = row.find_element(By.CSS_SELECTOR, "td.col-3")
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", clickable)
        clickable.click()

        WebDriverWait(driver, timeout).until(EC.url_contains("/doc/"))
        time.sleep(1.5)

        landed_url = driver.current_url
        text_plain = re.sub(r"<[^>]+>", " ", driver.page_source)
        text_plain = re.sub(r"\s+", " ", text_plain)

        # Prefer a labeled "Property Address" block if the page has one
        m = DOC_ADDR_LABEL_RE.search(text_plain)
        if m:
            m2 = DOC_ADDR_GENERIC_RE.search(m.group(1) + " TX 00000")
            if m2:
                return m2.group(1).strip(), m2.group(2).strip(), m2.group(3).strip()

        # Otherwise scan the whole page for a street/city/TX/zip pattern
        for m in DOC_ADDR_GENERIC_RE.finditer(text_plain):
            street, city, zipc = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
            if zipc.startswith("78"):  # sanity check — South Texas zip
                return street, city, zipc

        # Nothing matched — log exactly what we landed on so this is
        # debuggable from the Action log instead of a silent 0/N.
        snippet = text_plain[:400]
        log.warning(
            f"  click-fallback: doc {doc_number} landed on {landed_url} "
            f"but no address pattern matched. Page text starts: {snippet!r}"
        )

    except Exception as e:
        log.warning(f"  click-fallback failed for doc {doc_number}: {e}")

    return None, None, None

# ── Appraisal Roll Lookup ─────────────────────────────────────────────────────
def normalize_owner(s):
    """Normalize owner name for lookup — last name only (first word)."""
    if not s:
        return ""
    return s.upper().strip().split()[0] if s.strip() else ""

def load_lookup():
    """
    Load Nueces CAD appraisal roll lookup CSV into memory.
    Indexed by FOUR keys for maximum match rate:
      1. legal_desc — exact normalized string (fast path, rarely hits)
      2. legal_desc — (subdivision-name signature, lot) — the real fix
      3. legal_desc — subdivision-name signature alone (fallback)
      4. situs address / owner last name (unchanged from v1.0)
    """
    if not LOOKUP_PATH.exists():
        log.warning(f"Lookup CSV not found at {LOOKUP_PATH} — enrichment disabled")
        return {}, {}, {}, {}

    log.info(f"Loading lookup from {LOOKUP_PATH}...")
    lookup      = {}   # exact normalized legal_desc / situs → row
    by_sig_lot  = {}   # (name_sig, lot) → [rows]
    by_sig      = {}   # name_sig → [rows]
    by_owner    = {}   # last_name → [rows]
    count = 0
    try:
        with gzip.open(LOOKUP_PATH, "rt", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                raw_legal = row.get("legal_desc", "")

                # 1. exact normalized string
                legal = normalize_legal(raw_legal)
                if legal:
                    lookup[legal] = row
                    count += 1

                # 2 & 3. signature-based index
                name_sig, lot, block, section = parse_legal_components(raw_legal)
                if name_sig:
                    if lot:
                        by_sig_lot.setdefault((name_sig, lot), []).append(row)
                    by_sig.setdefault(name_sig, []).append(row)

                # 4. situs address
                situs = (row.get("situs_addr", "") or "").strip().upper()
                if situs and situs not in lookup:
                    lookup[situs] = row

                # 4. owner last name
                owner = (row.get("owner", "") or "").strip().upper()
                last = owner.split()[0] if owner.strip() else ""
                if last and len(last) >= 3:
                    by_owner.setdefault(last, []).append(row)
        log.info(
            f"Loaded {count} lookup records, {len(by_sig_lot)} sig+lot keys, "
            f"{len(by_sig)} sig-only keys, {len(by_owner)} owner name keys"
        )
    except Exception as e:
        log.warning(f"Lookup load error: {e}")
    return lookup, by_sig_lot, by_sig, by_owner

def _resolve_sig_candidates(candidates, block):
    """Pick the best row out of multiple signature-match candidates."""
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    if block:
        for cand in candidates:
            _, _, cand_block, _ = parse_legal_components(cand.get("legal_desc", ""))
            if cand_block and cand_block == block:
                return cand
    # No confident tie-break — take the first as a best-effort guess
    return candidates[0]

def enrich_from_lookup(rec, lookup_tuple):
    """
    Enrich a record using the roll lookup. Tries, in order:
    1. Legal description — exact normalized string match
    2. Legal description — signature match (subdivision name + lot number)
    3. Legal description — signature match (subdivision name only)
    4. Situs address exact match
    5. Owner last name cross-match
    """
    if not lookup_tuple:
        return rec
    lookup, by_sig_lot, by_sig, by_owner = lookup_tuple

    result = None
    raw_legal = rec.get("legal_desc", "") or rec.get("remarks", "")

    # Strategy 1: exact legal description string match
    legal = normalize_legal(raw_legal)
    if legal and len(legal) > 5:
        result = lookup.get(legal)

    # Strategy 2/3: signature match (this is what actually fixes FC leads)
    if not result:
        name_sig, lot, block, section = parse_legal_components(raw_legal)
        if name_sig:
            candidates = by_sig_lot.get((name_sig, lot), []) if lot else []
            if not candidates:
                candidates = by_sig.get(name_sig, [])
            result = _resolve_sig_candidates(candidates, block)

    # Strategy 4: situs address exact match
    if not result:
        addr = (rec.get("address", "") or "").strip().upper()
        if addr and len(addr) > 5:
            result = lookup.get(addr)

    # Strategy 5: owner last name cross-match with address number
    if not result:
        owner = (rec.get("owner", "") or "").strip().upper()
        last = owner.split()[0] if owner.strip() else ""
        if last and len(last) >= 3 and last in by_owner:
            candidates = by_owner[last]
            if len(candidates) == 1:
                result = candidates[0]
            elif len(candidates) <= 10:
                owner_parts = owner.split()
                for cand in candidates:
                    cand_owner = (cand.get("owner", "") or "").upper()
                    cand_parts = cand_owner.split()
                    matches = sum(1 for p in owner_parts[:2] if p in cand_parts)
                    if matches >= 2:
                        result = cand
                        break

    if not result:
        return rec

    # Apply enrichment — only fill blanks
    if not rec.get("owner") and result.get("owner"):
        rec["owner"] = result["owner"].title()

    if not rec.get("address") and result.get("situs_addr"):
        rec["address"] = result["situs_addr"].upper()

    if not rec.get("zip") and result.get("situs_zip"):
        rec["zip"] = result["situs_zip"]

    if not rec.get("city") and result.get("situs_city"):
        rec["city"] = result["situs_city"].title()

    if not rec.get("appraised_value") and result.get("appraised_value"):
        try:
            val = float(result["appraised_value"])
            if val > 0:
                rec["appraised_value"] = val
        except Exception:
            pass

    if not rec.get("absentee"):
        rec["absentee"] = result.get("absentee", "0") == "1"

    if not rec.get("is_entity"):
        rec["is_entity"] = result.get("is_entity", "0") == "1"

    # Tenure — years owned from the roll's last-sale date (free, no live API)
    if rec.get("tenure_years") is None:
        yrs = years_owned_from_sale_date(result.get("sale_date", ""))
        if yrs is not None:
            rec["tenure_years"] = yrs
            rec["tenure_score_bonus"] = tenure_bonus(yrs)
            rec["deed_date"] = result.get("sale_date", "")

    # Equity estimate — current appraised value minus what they paid.
    # Rough signal only (doesn't account for improvements/market swings since
    # purchase), but a fast way to flag high-equity motivated-seller leads.
    if not rec.get("equity_est"):
        try:
            sale_price = float(result.get("sale_price", "") or 0)
            appraised = float(rec.get("appraised_value", "") or result.get("appraised_value", "") or 0)
            if sale_price > 0 and appraised > 0:
                rec["last_sale_price"] = sale_price
                rec["equity_est"] = round(appraised - sale_price, 0)
        except Exception:
            pass

    # Coastal flag
    rec["is_coastal"] = is_coastal(rec.get("zip", ""))

    return rec

# ── PublicSearch Scraper ───────────────────────────────────────────────────────
ENTITY_FILTER_KW = [
    "LLC","L.L.C","INC","CORP","BANK","N.A.","TRUST","TRUSTEE",
    "MORTGAGE","LOAN SERVICING","SERVICER","FEDERAL","SAVINGS",
    "ASSOCIATION","DEPARTMENT","AGENCY","COMMISSIONER","JPMORGAN",
    "CHASE","WELLS FARGO","NATIONSTAR","MR COOPER","PENNYMAC",
    "NEWREZ","CALIBER","SELENE","PHH","OCWEN","SPS","BSI",
    "BARRETT DAFFIN","SUBSTITUTE TRUSTEE","AUCTION.COM",
]

def is_entity_name(name):
    if not name:
        return True
    upper = name.upper()
    return any(kw in upper for kw in ENTITY_FILTER_KW)

def new_record(doc_number, lead_type, source="publicsearch", run_ts=None):
    return {
        "doc_number":       doc_number,
        "county":           COUNTY,
        "type":             lead_type,
        "source":           source,
        "owner":            "",
        "address":          "",
        "city":             "Corpus Christi",
        "zip":              "",
        "date_filed":       "",
        "sale_date":        "",
        "days_until_sale":  None,
        "lender":           "",
        "loan_amount":      "",
        "loan_date":        "",
        "trustee":          "",
        "appraised_value":  "",
        "legal_desc":       "",
        "remarks":          "",
        "absentee":         False,
        "is_entity":        False,
        "is_coastal":       False,
        "duplicate":        False,
        "is_new":           True,
        "score":            0,
        "flags":            [],
        "run_ts":           run_ts or TODAY.isoformat(),
        "tenure_years":     None,
        "tenure_score_bonus": 0,
        "deed_date":        "",
        "last_sale_price":  "",
        "equity_est":       "",
        "prop_id":          "",
        "ps_doc_id":        "",
        # Code enforcement fields
        "ce_case_id":       "",
        "ce_reason":        "",
        "ce_status":        "",
        "ce_category":      "",
        "opened_date":      "",
        # Dashboard fields
        "dash_phone":       "",
        "dash_dispo":       "new",
        "dash_notes":       "",
        "ghl_pushed":       False,
        "ghl_id":           "",
    }

def scrape_publicsearch(department, search_term, lead_type, known_docs, driver, run_ts, days=None):
    """
    Generic PublicSearch scraper for Nueces County.
    department: 'FC' (foreclosures) or 'RP' (real property)
    search_term: e.g. 'NOTICE' or 'APPOINTMENT'
    days: optional lookback window override (defaults to SCRAPE_DAYS).
          Used by backfill_30d.py to re-pull a shorter recent window.
    """
    new_records = []
    window = days if days is not None else SCRAPE_DAYS
    cutoff = (TODAY - timedelta(days=window)).strftime("%Y%m%d")
    today_str = TODAY.strftime("%Y%m%d")
    offset = 0
    consecutive_empty = 0

    log.info(f"Scraping {department}/{search_term} ({lead_type})...")

    while True:
        url = (
            f"{PUBLICSEARCH_BASE}/results"
            f"?department={department}"
            f"&keywordSearch=false"
            f"&limit=50"
            f"&offset={offset}"
            f"&recordedDateRange={cutoff}%2C{today_str}"
            f"&searchOcrText=false"
            f"&searchType=quickSearch"
            f"&searchValue={urllib.parse.quote(search_term)}"
            f"&sort=desc"
            f"&sortBy=recordedDate"
        )
        log.info(f"  offset={offset}")

        try:
            driver.get(url)
            WebDriverWait(driver, 30).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "table tr td, .no-results, [class*='no-result']")
                )
            )
            time.sleep(2)
        except Exception as e:
            log.warning(f"  Timeout offset={offset}: {e}")
            consecutive_empty += 1
            if consecutive_empty >= 3:
                break
            time.sleep(5)
            continue

        src = driver.page_source

        if "no results" in src.lower() or "0 of 0" in src:
            log.info(f"  No results — stopping")
            break

        m = re.search(r"(\d[\d,]*)\s*of\s*(\d[\d,]*)\s*results?", src, re.IGNORECASE)
        if m:
            log.info(f"  Results: {m.group(0)}")

        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", src, re.DOTALL | re.IGNORECASE)
        page_recs = []

        for row in rows:
            if re.search(r"<th|thead|DOC.TYPE|RECORDED|GRANTOR|GRANTEE|PROPERTY", row, re.IGNORECASE):
                continue

            cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)
            cells = [re.sub(r"<[^>]+>", "", c).strip() for c in cells if c.strip()]
            if len(cells) < 3:
                continue

            doc_num = next((c for c in cells if re.match(r"^\d{7,10}$", c.strip())), "")
            if not doc_num or doc_num in known_docs:
                continue

            # Nueces FC results include PROPERTY ADDRESS column
            # Format: DOC TYPE | RECORDED | SALE DATE | DOC# | PROPERTY ADDRESS | GRANTOR | GRANTEE
            property_addr = ""
            addr_candidates = [
                c for c in cells
                if re.match(r"^\d+\s+[A-Z]", c.upper())
                and len(c) > 8
                and "N/A" not in c.upper()
                and not re.match(r"^\d{7,10}$", c)
            ]
            if addr_candidates:
                raw_addr = addr_candidates[0]
                # May contain subdivision name like "LAMAR PARK SECTION 1 LOT 2"
                # Try to extract just the street address if it has a number prefix
                addr_m = re.match(r"^(\d+\s+[A-Z0-9 ]+?)(?:\s+(?:LOT|BLOCK|SECTION|UNIT|APT|#).*)?$", raw_addr.upper())
                if addr_m:
                    property_addr = addr_m.group(1).strip()
                else:
                    property_addr = raw_addr.upper()[:80]

            dates = [c for c in cells if re.match(r"^\d{1,2}/\d{1,2}/\d{4}$", c.strip())]
            recorded_date = dates[0] if dates else ""

            # Extract sale date from remarks column
            sale_date = ""
            remarks_raw = next(
                (c for c in cells if re.search(r"(10AM|1PM|2PM|10:00|AUCTION)", c, re.IGNORECASE)),
                ""
            )
            sale_m = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", remarks_raw)
            if sale_m:
                sale_date = sale_m.group(1)
            elif len(dates) >= 2:
                # No keyword-flagged remarks cell — Nueces FC table may just list
                # the trustee sale date as a plain second date column with no
                # time/AUCTION text. Recorded date is dates[0], so take the next
                # distinct date as the sale date fallback.
                sale_date = next((d for d in dates[1:] if d != recorded_date), "")

            # Month/year from recorded date
            month, year = "", ""
            if recorded_date:
                parts = recorded_date.split("/")
                if len(parts) == 3:
                    month, year = parts[0], parts[2]

            # Grantor = owner (first non-entity name candidate)
            name_candidates = [
                c for c in cells
                if len(c) > 4
                and c not in dates
                and not re.match(r"^\d{9,12}$", c)
                and not re.match(r"^\d{1,2}/\d{1,2}/\d{4}$", c)
                and re.search(r"[A-Za-z]{2,}", c)
                and "N/A" not in c.upper()
                and "NOTICE" not in c.upper()
                and "APPOINTMENT" not in c.upper()
                and "FORECLOSURE" not in c.upper()
            ]

            grantor = ""
            for cand in name_candidates:
                if not is_entity_name(cand):
                    grantor = cand
                    break
            if not grantor and name_candidates:
                grantor = name_candidates[0]

            # Lender = first entity name
            lender = ""
            for cand in name_candidates:
                if is_entity_name(cand) and cand != grantor:
                    lender = cand
                    break

            # Legal description from remaining cells
            legal = next(
                (c for c in cells
                 if re.search(r"\b(LOT|BLOCK|SECTION|SUBDIVISION|SUBD|TRACT|ABSTRACT)\b", c, re.IGNORECASE)
                 and len(c) > 10),
                ""
            )

            rec = new_record(doc_num, lead_type, run_ts=run_ts)
            # Stored so the click-fallback knows which results page to reload
            # to find this row again — stripped from records before saving.
            rec["_source_url"] = url
            rec["owner"]       = grantor.title() if grantor else ""
            rec["lender"]      = lender
            rec["date_filed"]  = f"{month}/{year}".strip("/")
            rec["sale_date"]   = sale_date
            rec["legal_desc"]  = legal.upper() if legal else ""
            rec["remarks"]     = remarks_raw[:200] if remarks_raw else ""
            # Direct address from PublicSearch results table
            if property_addr:
                rec["address"] = property_addr
                rec["city"]    = "Corpus Christi"

            page_recs.append(rec)

        log.info(f"  offset={offset} | {len(page_recs)} new on page")

        for rec in page_recs:
            known_docs.add(rec["doc_number"])
            new_records.append(rec)

        if len(page_recs) == 0:
            consecutive_empty += 1
        else:
            consecutive_empty = 0

        if consecutive_empty >= 2 or 0 < len(page_recs) < 50:
            break

        offset += 50
        time.sleep(1.5)

    log.info(f"{department}/{search_term}: {len(new_records)} new records")
    return new_records

# ── Code Enforcement Scraper ──────────────────────────────────────────────────
def scrape_code_enforcement(known_docs, run_ts):
    """
    Scrape Corpus Christi code compliance cases from open data.
    Uses the city's public ArcGIS feature service.
    """
    new_records = []
    log.info("Scraping Corpus Christi code enforcement...")

    # CC open data — no public CE case endpoint exists yet (program est. Sept 2025)
    # Using NCad_Parcels (layer 43) from CC open data to find distressed properties
    # This gives us parcels with code violations flagged in the CAD data
    endpoints = [
        "https://services.arcgis.com/0J4ZNc4NaTguvRy0/ArcGIS/rest/services/OpenData/FeatureServer/43/query",
    ]

    cutoff_date = (TODAY - timedelta(days=SCRAPE_DAYS)).strftime("%Y-%m-%d")

    # Try ArcGIS FeatureServer first
    # Query NCad_Parcels for properties with code-related state codes
    # state_cd A1=residential, filter for distressed indicators
    params = urllib.parse.urlencode({
        "where":             "state_cd IN ('A1','A2') AND (imprv_type = 'DILAPIDATED' OR land_type_ = 'VACANT')",
        "outFields":         "PROP_ID,situs_disp,file_as_na,addr_zip,addr_city,appraised_,state_cd",
        "returnGeometry":    "false",
        "resultRecordCount": 2000,
        "f":                 "json",
    })

    found = False
    for endpoint in endpoints:
        try:
            if "FeatureServer" in endpoint:
                url = f"{endpoint}?{params}"
            else:
                url = endpoint

            req = urllib.request.Request(
                url,
                headers={"User-Agent": "NuecesLeads/1.0", "Accept": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode("utf-8", errors="replace"))

            features = data.get("features", [])
            log.info(f"Code enforcement: {len(features)} cases from {endpoint}")

            for feat in features:
                attrs = feat.get("attributes", feat.get("properties", {}))
                prop_id = str(attrs.get("PROP_ID", "") or "")
                if not prop_id:
                    continue

                doc_key = f"CE-{prop_id}"
                if doc_key in known_docs:
                    continue

                address = (attrs.get("situs_disp", "") or "").strip().upper()
                owner   = (attrs.get("file_as_na", "") or "").strip()
                status  = "OPEN"
                vtype   = attrs.get("state_cd", "")
                open_dt = ""
                zip_c   = str(attrs.get("addr_zip", "") or "")[:5]

                # Parse open date
                date_filed = ""
                try:
                    if open_dt and open_dt.isdigit():
                        dt = datetime.fromtimestamp(int(open_dt)/1000)
                        date_filed = dt.strftime("%m/%Y")
                    elif re.match(r"\d{4}-\d{2}-\d{2}", open_dt):
                        date_filed = datetime.strptime(open_dt[:10], "%Y-%m-%d").strftime("%m/%Y")
                except Exception:
                    pass

                rec = new_record(doc_key, "CE", source="code_enforcement", run_ts=run_ts)
                rec["ce_case_id"]  = prop_id  # was: undefined `case_id` — fixed v1.1
                rec["address"]     = address
                rec["owner"]       = owner.title() if owner else ""
                rec["zip"]         = zip_c
                rec["city"]        = "Corpus Christi"
                rec["ce_status"]   = status
                rec["ce_reason"]   = vtype
                rec["ce_category"] = vtype
                rec["opened_date"] = date_filed
                rec["date_filed"]  = date_filed
                rec["is_coastal"]  = is_coastal(zip_c)

                known_docs.add(doc_key)
                new_records.append(rec)

            found = True
            break

        except Exception as e:
            log.warning(f"Code enforcement endpoint failed ({endpoint}): {e}")
            continue

    if not found:
        log.warning("Code enforcement: no data retrieved — both endpoints failed")

    log.info(f"Code enforcement: {len(new_records)} new cases")
    return new_records

# ── Purge Past Auctions ───────────────────────────────────────────────────────
def purge_past_auctions(records):
    """Remove leads where auction date has passed. Preserve GHL-worked leads."""
    before = len(records)
    kept = []
    for rec in records:
        lead_type = rec.get("type", "")
        # Never purge CE, APPT, or GHL-worked leads
        if lead_type in ("CE", "APPT") or rec.get("ghl_pushed") or rec.get("dash_phone"):
            kept.append(rec)
            continue
        # NOF/TAX: purge if auction passed
        if lead_type in ("NOF", "TAX"):
            sd = rec.get("sale_date", "")
            if sd and auction_passed(sd):
                continue
            # Also purge stale leads with no sale date > 180 days
            if not sd and not filed_within_window(rec.get("date_filed", ""), 180):
                continue
        kept.append(rec)
    removed = before - len(kept)
    if removed:
        log.info(f"Purged {removed} past-auction leads")
    return kept

# ── Dedup ─────────────────────────────────────────────────────────────────────
def dedup(existing, new_recs):
    """Merge new records into existing, dedup by doc_number."""
    seen = {r["doc_number"]: r for r in existing}
    added = 0
    for rec in new_recs:
        doc = rec["doc_number"]
        if doc not in seen:
            seen[doc] = rec
            added += 1
        else:
            # Preserve existing GHL data, update enrichment fields
            existing_rec = seen[doc]
            for field in ["owner", "address", "zip", "city", "appraised_value",
                          "legal_desc", "sale_date", "lender", "is_coastal",
                          "is_entity", "absentee"]:
                if rec.get(field) and not existing_rec.get(field):
                    existing_rec[field] = rec[field]
            existing_rec["is_new"] = False
    log.info(f"Dedup: {added} new, {len(seen)-added} existing")
    return list(seen.values())

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    run_ts = TODAY.isoformat()
    log.info("=" * 60)
    log.info(f"Nueces County Lead Scraper v1.1")
    log.info(f"Run: {run_ts}")
    log.info("=" * 60)

    # Load existing records
    existing = []
    if RECORDS_PATH.exists():
        try:
            existing = json.loads(RECORDS_PATH.read_text(encoding="utf-8"))
            log.info(f"Loaded {len(existing)} existing records")
        except Exception as e:
            log.warning(f"Could not load records: {e}")
            existing = []

    # Mark all existing as not-new
    for rec in existing:
        rec["is_new"] = False

    known_docs = {r["doc_number"] for r in existing}

    # Load appraisal roll lookup
    lookup = load_lookup()  # returns (lookup, by_sig_lot, by_sig, by_owner)

    # ── Selenium driver ───────────────────────────────────────────────────────
    log.info("Starting WebDriver...")
    driver = get_driver()

    all_new = []

    try:
        # Source 1: FC department — ALL foreclosure notices (blank search = all docs)
        # Nueces uses "FORECLOSURE NOTICE" doc type — blank search catches everything
        nof_recs = scrape_publicsearch(
            department="FC",
            search_term="",
            lead_type="NOF",
            known_docs=known_docs,
            driver=driver,
            run_ts=run_ts,
        )
        all_new.extend(nof_recs)

        # Source 2: FC department — TAX foreclosures (separate search)
        tax_recs = scrape_publicsearch(
            department="FC",
            search_term="TAX",
            lead_type="TAX",
            known_docs=known_docs,
            driver=driver,
            run_ts=run_ts,
        )
        all_new.extend(tax_recs)

        # Source 3: RP department — Appointment of Substitute Trustee (Pre-Fore)
        appt_recs = scrape_publicsearch(
            department="RP",
            search_term="APPOINTMENT",
            lead_type="APPT",
            known_docs=known_docs,
            driver=driver,
            run_ts=run_ts,
        )
        all_new.extend(appt_recs)

        # ── Enrich ALL records missing address — new + existing ──────────────
        # (runs here, while the driver is still open, so the Selenium fallback
        #  right after it can reuse the same session)
        needs_enrich = [
            r for r in existing
            if not r.get("address") or not r.get("appraised_value") or r.get("tenure_years") is None
        ]
        enrich_targets = all_new + needs_enrich
        log.info(f"Enriching {len(all_new)} new + {len(needs_enrich)} existing records from appraisal roll...")
        enriched = 0
        for rec in enrich_targets:
            before_addr = rec.get("address", "")
            rec = enrich_from_lookup(rec, lookup)
            rec["score"] = score_record(rec)
            d = days_until_sale(rec.get("sale_date", ""))
            rec["days_until_sale"] = d
            if d is not None and d <= 14:
                rec["flags"] = list(set(rec.get("flags", []) + ["URGENT", "AUCTION SOON"]))
            elif d is not None and d <= 30:
                rec["flags"] = list(set(rec.get("flags", []) + ["AUCTION SOON"]))
            if rec.get("type") == "APPT":
                rec["ghl_tag"] = "nueces_prefore"
            elif rec.get("source") == "code_enforcement":
                rec["ghl_tag"] = "nueces_ce"
            else:
                rec["ghl_tag"] = "nueces_lead"
            if rec.get("address") and not before_addr:
                enriched += 1
        log.info(f"Roll enrichment: {enriched}/{len(all_new)} new-lead addresses filled")

        # ── Selenium fallback: FC leads still missing address after roll match ──
        still_missing = [
            r for r in all_new
            if r.get("type") in ("NOF", "TAX") and not r.get("address") and r.get("_source_url")
        ]
        log.info(f"Selenium fallback: {len(still_missing)} FC leads still missing address")
        fetched = 0
        for rec in still_missing[:MAX_DOC_FETCH]:
            street, city, zipc = fetch_address_by_click(driver, rec["_source_url"], rec["doc_number"])
            if street:
                rec["address"] = street.upper()
                if city:
                    rec["city"] = city
                if zipc:
                    rec["zip"] = zipc
                rec["is_coastal"] = is_coastal(rec.get("zip", ""))
                rec["score"] = score_record(rec)
                fetched += 1
            time.sleep(1)
        skipped = max(0, len(still_missing) - MAX_DOC_FETCH)
        log.info(
            f"Selenium fallback: {fetched}/{min(len(still_missing), MAX_DOC_FETCH)} "
            f"addresses recovered from doc pages"
            + (f" ({skipped} deferred to next run — MAX_DOC_FETCH cap)" if skipped else "")
        )

    finally:
        try:
            driver.quit()
        except Exception:
            pass

    # Source 4: Code Enforcement (no Selenium needed)
    ce_recs = scrape_code_enforcement(known_docs, run_ts)
    all_new.extend(ce_recs)

    # Score/tag the code-enforcement batch too (it wasn't in enrich_targets above)
    for rec in ce_recs:
        rec = enrich_from_lookup(rec, lookup)
        rec["score"] = score_record(rec)
        rec["ghl_tag"] = "nueces_ce"

    # ── Merge + dedup ─────────────────────────────────────────────────────────
    all_records = dedup(existing, all_new)

    # ── Purge past auctions ───────────────────────────────────────────────────
    all_records = purge_past_auctions(all_records)

    # ── 90-day rolling filter ─────────────────────────────────────────────────
    before_filter = len(all_records)
    all_records = [
        r for r in all_records
        if r.get("type") in ("CE", "APPT")  # keep CE and Pre-Fore always
        or r.get("ghl_pushed") or r.get("dash_phone")  # keep worked leads
        or filed_within_window(r.get("date_filed", "") or r.get("opened_date", ""), SCRAPE_DAYS)
    ]
    log.info(f"90-day filter: {before_filter} → {len(all_records)}")

    # ── Rescore all ───────────────────────────────────────────────────────────
    for rec in all_records:
        rec["score"] = score_record(rec)
        d = days_until_sale(rec.get("sale_date", ""))
        rec["days_until_sale"] = d

    # ── Stats ─────────────────────────────────────────────────────────────────
    total    = len(all_records)
    new_ct   = sum(1 for r in all_records if r.get("is_new"))
    nof_ct   = sum(1 for r in all_records if r.get("type") == "NOF")
    tax_ct   = sum(1 for r in all_records if r.get("type") == "TAX")
    appt_ct  = sum(1 for r in all_records if r.get("type") == "APPT")
    ce_ct    = sum(1 for r in all_records if r.get("type") == "CE")
    urgent   = sum(1 for r in all_records if r.get("days_until_sale") is not None and r.get("days_until_sale", 999) <= 14)
    with_addr= sum(1 for r in all_records if r.get("address"))
    coastal  = sum(1 for r in all_records if r.get("is_coastal"))
    named    = sum(1 for r in all_records if r.get("owner"))

    log.info(f"Final: {total} total | {new_ct} new | {named} named")
    log.info(f"  NOF={nof_ct} | TAX={tax_ct} | APPT={appt_ct} | CE={ce_ct}")
    log.info(f"  URGENT≤14d={urgent} | with_addr={with_addr} | coastal={coastal}")

    # ── Save ──────────────────────────────────────────────────────────────────
    # _source_url is only needed during this run's click-fallback — strip it
    # so it doesn't bloat records.json or leak into the dashboard.
    for rec in all_records:
        rec.pop("_source_url", None)

    RECORDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RECORDS_PATH.write_text(
        json.dumps(all_records, ensure_ascii=False),
        encoding="utf-8"
    )
    size_kb = RECORDS_PATH.stat().st_size / 1024
    log.info(f"Dashboard: {total} records, {size_kb:.0f} KB")
    log.info("Done.")

if __name__ == "__main__":
    main()
