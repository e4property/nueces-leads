"""
Nueces County Motivated Seller Lead Scraper v1.0
County: Nueces (Corpus Christi, TX)
Source 1: nueces.tx.publicsearch.us — FC dept (NOF/TAX foreclosures)
Source 2: nueces.tx.publicsearch.us — RP dept (Appointment of Substitute Trustee / Pre-Fore)
Source 3: Corpus Christi Code Compliance cases (open violations)
Enrichment: Nueces CAD appraisal roll lookup CSV (legal desc → address/owner/value)
GHL tags: nueces_lead, nueces_prefore, nueces_ce
Scrape schedule: Mon/Thu 9am + 3pm CST (14:00 + 20:00 UTC)

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

# ── Appraisal Roll Lookup ─────────────────────────────────────────────────────
def normalize_owner(s):
    """Normalize owner name for lookup — last name only (first word)."""
    if not s:
        return ""
    return s.upper().strip().split()[0] if s.strip() else ""

def load_lookup():
    """
    Load Nueces CAD appraisal roll lookup CSV into memory.
    Indexed by THREE keys for maximum match rate:
      1. legal_desc (exact + prefix)
      2. situs address
      3. owner last name (first word) → list of records
    """
    if not LOOKUP_PATH.exists():
        log.warning(f"Lookup CSV not found at {LOOKUP_PATH} — enrichment disabled")
        return {}, {}

    log.info(f"Loading lookup from {LOOKUP_PATH}...")
    lookup   = {}   # legal_desc / situs → row
    by_owner = {}   # last_name → [rows]
    count = 0
    try:
        with gzip.open(LOOKUP_PATH, "rt", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Index by legal description
                legal = normalize_legal(row.get("legal_desc", ""))
                if legal:
                    lookup[legal] = row
                    count += 1
                # Index by situs address
                situs = (row.get("situs_addr", "") or "").strip().upper()
                if situs and situs not in lookup:
                    lookup[situs] = row
                # Index by owner last name
                owner = (row.get("owner", "") or "").strip().upper()
                last = owner.split()[0] if owner.strip() else ""
                if last and len(last) >= 3:
                    by_owner.setdefault(last, []).append(row)
        log.info(f"Loaded {count} lookup records, {len(by_owner)} owner name keys")
    except Exception as e:
        log.warning(f"Lookup load error: {e}")
    return lookup, by_owner

def enrich_from_lookup(rec, lookup_tuple):
    """
    Enrich a record using three-strategy lookup:
    1. Legal description (exact)
    2. Situs address (exact)
    3. Owner last name + address number cross-match
    """
    if not lookup_tuple:
        return rec
    lookup, by_owner = lookup_tuple

    result = None

    # Strategy 1: legal description exact match
    legal = normalize_legal(rec.get("legal_desc", "") or rec.get("remarks", ""))
    if legal and len(legal) > 5:
        result = lookup.get(legal)

    # Strategy 2: situs address exact match
    if not result:
        addr = (rec.get("address", "") or "").strip().upper()
        if addr and len(addr) > 5:
            result = lookup.get(addr)

    # Strategy 3: owner last name cross-match with address number
    if not result:
        owner = (rec.get("owner", "") or "").strip().upper()
        last = owner.split()[0] if owner.strip() else ""
        if last and len(last) >= 3 and last in by_owner:
            candidates = by_owner[last]
            # If only one candidate for this last name, use it
            if len(candidates) == 1:
                result = candidates[0]
            elif len(candidates) <= 10:
                # Try to match full name — check if second word matches
                owner_parts = owner.split()
                for cand in candidates:
                    cand_owner = (cand.get("owner", "") or "").upper()
                    cand_parts = cand_owner.split()
                    # Match first 2 words of owner name
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
        "prop_id":          "",
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

def scrape_publicsearch(department, search_term, lead_type, known_docs, driver, run_ts):
    """
    Generic PublicSearch scraper for Nueces County.
    department: 'FC' (foreclosures) or 'RP' (real property)
    search_term: e.g. 'NOTICE' or 'APPOINTMENT'
    """
    new_records = []
    cutoff = (TODAY - timedelta(days=SCRAPE_DAYS)).strftime("%Y%m%d")
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

            ps_doc_id = ""
            href = re.findall(r"/doc/(\d+)", row)
            if href:
                ps_doc_id = href[0]

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
            rec["ps_doc_id"]   = ps_doc_id
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
                rec["ce_case_id"]  = case_id
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
    log.info(f"Nueces County Lead Scraper v1.0")
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
    lookup = load_lookup()  # returns (lookup_dict, by_owner_dict)

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

    finally:
        try:
            driver.quit()
        except Exception:
            pass

    # Source 4: Code Enforcement (no Selenium needed)
    ce_recs = scrape_code_enforcement(known_docs, run_ts)
    all_new.extend(ce_recs)

    # ── Enrich ALL records missing address — new + existing ──────────────────
    needs_enrich = [r for r in existing if not r.get("address") or not r.get("appraised_value")]
    enrich_targets = all_new + needs_enrich
    log.info(f"Enriching {len(all_new)} new + {len(needs_enrich)} existing records from appraisal roll...")
    enriched = 0
    for rec in enrich_targets:
        before_addr = rec.get("address", "")
        rec = enrich_from_lookup(rec, lookup)
        # Score
        rec["score"] = score_record(rec)
        # Auction urgency flags
        d = days_until_sale(rec.get("sale_date", ""))
        rec["days_until_sale"] = d
        if d is not None and d <= 14:
            rec["flags"] = list(set(rec.get("flags", []) + ["URGENT", "AUCTION SOON"]))
        elif d is not None and d <= 30:
            rec["flags"] = list(set(rec.get("flags", []) + ["AUCTION SOON"]))
        # GHL tag
        if rec.get("type") == "APPT":
            rec["ghl_tag"] = "nueces_prefore"
        elif rec.get("source") == "code_enforcement":
            rec["ghl_tag"] = "nueces_ce"
        else:
            rec["ghl_tag"] = "nueces_lead"

        if rec.get("address") and not before_addr:
            enriched += 1

    log.info(f"Enrichment: {enriched}/{len(all_new)} addresses filled from roll")

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
