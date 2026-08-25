"""
Nueces County Motivated Seller Lead Scraper v1.2
County: Nueces (Corpus Christi, TX)
Source 1: nueces.tx.publicsearch.us — FC dept (NOF/TAX foreclosures)
Source 2: nueces.tx.publicsearch.us — RP dept (Appointment of Substitute Trustee / Pre-Fore)
Source 3: Corpus Christi Code Compliance cases (open violations)
Enrichment: Nueces CAD appraisal roll lookup CSV (legal desc → address/owner/value)
GHL tags: nueces_lead, nueces_prefore, nueces_ce
Scrape schedule: Mon/Thu 9am + 3pm CST (14:00 + 20:00 UTC)

v1.7 changes (Deed of Trust hop for APPT address gap, closes the v1.4 TODO):
  - Live-verified 2026-08-23: an APPT doc's "Marginal References" section
    lists the Deed of Trust it's tied to (e.g. "Instrument Number: 2024012324
    DEED OF TRUST 4/15/2024"), filed in the SAME department (RP) as the
    APPT itself. Hopping to that doc number via the same
    advancedSearch/documentNumberRange mechanism and re-parsing the page
    for a Property Address closes the gap -- same pattern as Bexar's
    Notice -> Deed of Trust OCR chain, but no OCR needed here since this
    site's doc pages expose real DOM text.
  - Also found live 2026-08-23, separate from the above: recordedDateRange's
    end date must not exceed the site's own "Certified through" date (runs
    ~2 days behind real today) or the search returns "No Results Found"
    even for a document that unambiguously exists and falls well inside the
    range -- confirmed by bisecting a known-good doc (2024012324) between
    end=today (fails) and end=today-2d (succeeds), identical query
    otherwise. fetch_address_by_docnumber's single-doc lookups now use a
    3-day safety buffer on the end date instead of TODAY exactly.

v1.4 changes (fixed the actual search mechanism, not just its scope):
  - v1.3's fetch_address_by_docnumber() still had a 100% failure rate even
    after covering the full backlog -- confirmed live 2026-08-20 via the
    site's own Advanced Search UI that searchType=quickSearch is a
    keyword/party-name search, not an exact document-number lookup. It
    returns "No Results" for every doc number regardless of validity.
    doc_number itself was never wrong (unlike the analogous Bexar bug) --
    only the URL was. Real mechanism: searchType=advancedSearch with
    documentNumberRange as a JSON array (found in the "Single Document
    Numbers" Advanced Search field). Verified end-to-end against a real
    doc (2026026641) -- exact match. Also fixed row.click() on the <tr>
    not triggering navigation -- needs a click on td.col-3 specifically.
  - Real, structural limit found in the same investigation: APPT
    (Appointment of Substitute Trustee) documents genuinely have no
    Property Address field on the source page itself ("No property
    address found") -- this isn't a bug, TX law doesn't require this
    document type to state the property location. A Marginal Reference
    on the doc links to the actual Deed of Trust, which likely does have
    it -- a same-pattern second hop (like Bexar's Notice -> Deed of Trust
    OCR chain) would close this, not yet built.

v1.3 changes (address fallback now covers the backlog, not just new leads):
  - Confirmed via data audit 2026-08-20: 100/278 records missing address,
    100% of them type=APPT. Root cause: the v1.2 fallback only ever ran
    against all_new (this run's brand-new leads) and only for type
    NOF/TAX -- APPT was never included, and source_url (which the old
    fallback depends on) is stripped from every record before saving, so
    it can't be used on anything from a previous run either. Existing
    backlog leads were structurally unreachable no matter how many runs
    passed. Same bug shape as the Bexar scraper's loan_amount/_source_url
    issue fixed the same day.
  - REPLACED fetch_address_by_click() (source_url + same-run only) with
    fetch_address_by_docnumber() -- looks up any doc number directly via
    quickSearch (scoped to its filing department, NOF/TAX=FC, APPT=RP),
    so it works on ANY record regardless of type or age. The Selenium
    fallback loop now runs against the full backlog (existing + new),
    not just this run's new leads.

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
ON_MARKET_STATUSES      = {"FOR_SALE", "PENDING", "FOR_RENT"}
# 2026-08-21: bexar-leads hit a hard Realtor.com AuthenticationError wall
# after ~27 consecutive homeharvest requests in one run that never
# recovered. Keeping the combined per-run total (fetch + refresh) well
# under that.
ON_MARKET_FETCH_LIMIT   = 20   # max never-checked leads to look up per run
ON_MARKET_REFRESH_DAYS  = 7    # re-check a lead's market status at most this often
ON_MARKET_REFRESH_LIMIT = 10   # max already-checked leads to re-check per run
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
    # 2026-08-20: the old UA string was truncated -- "...AppleWebKit/537.36"
    # with nothing after it, which no real browser ever sends. Confirmed
    # live: fetch_address_by_docnumber()'s exact URL+click flow works
    # perfectly in an interactive browser but fails 100% of the time in
    # this headless session (same doc, same URL, same click target) --
    # strong sign the site is treating this session differently, and a
    # UA that's an obvious automation tell is the most likely reason.
    # Using a complete, current, realistic Chrome UA plus the standard
    # navigator.webdriver-hiding flags instead.
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    )
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    svc = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=svc, options=opts)
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"},
    )
    return driver

# ── Doc-page address fallback (v1.2 — click-through) ──────────────────────────
# Matches a header line like "2522 WIDGEON DR CORPUS CHRISTI TX 78410"
DOC_ADDR_LABEL_RE = re.compile(r"Property\s*Address[:\s]*([0-9][^<\n]{5,90})", re.IGNORECASE)
DOC_ADDR_GENERIC_RE = re.compile(
    r"(\d{1,6}\s+[A-Z0-9.\-\/ ]{3,40}?)\s+([A-Z][A-Z .]{2,25}?)\s+TX\s+(\d{5})"
)
# Matches a Marginal References entry like "Instrument Number: 2024012324
# DEED OF TRUST 4/15/2024" (whitespace-normalized page text) -- used to hop
# from an APPT doc (no address field) to the Deed of Trust it references,
# which does have one.
DEED_OF_TRUST_REF_RE = re.compile(
    r"Instrument\s+Number:\s*(\d+)\s+DEED\s+OF\s+TRUST", re.IGNORECASE
)

def _parse_address_from_current_page(driver, doc_number):
    """
    Shared by both address-fallback paths below -- assumes the driver has
    already landed on a /doc/<id> page and just pulls the address out of it.
    Returns (street, city, zip) or (None, None, None).
    """
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
        f"  address-fallback: doc {doc_number} landed on {landed_url} "
        f"but no address pattern matched. Page text starts: {snippet!r}"
    )
    return None, None, None


# department a doc number was originally filed under -- quickSearch results
# seem to be scoped by department, so an APPT (RP) doc number won't show up
# in an FC-scoped search and vice versa.
DEPT_BY_TYPE = {"NOF": "FC", "TAX": "FC", "APPT": "RP"}


def fetch_address_by_docnumber(driver, doc_number, department, timeout=40, _hop=False):
    """
    v1.3: address fallback for the BACKLOG, not just this run's new leads.
    fetch_address_by_click() only works when source_url is still in memory
    (same-run leads only) -- confirmed via the address-fill audit 2026-08-20:
    100/278 Nueces records missing address, ALL type=APPT, because the old
    fallback loop only ever considered NOF/TAX and only ever ran against
    all_new, never the existing backlog. This looks the doc number up
    directly via quickSearch instead of replaying a stored results-page URL,
    so it works for ANY doc number regardless of type or when it was first
    scraped -- same fix pattern already proven on the Bexar scraper's
    goto_doc_by_docnumber() for the analogous loan_amount backlog problem.

    v1.7: _hop=True marks this call as the recursive Deed-of-Trust follow-up
    (see below) so it can't chain a second time even if that doc also has a
    Marginal Reference of its own -- one hop only.
    Returns (street, city, zip) or (None, None, None).
    """
    if not doc_number:
        return None, None, None
    # v1.7: end date capped 3 days behind real today -- confirmed live
    # 2026-08-23 that recordedDateRange's end date must not exceed the
    # site's own "Certified through" date (runs ~1-2 days behind real
    # today) or the search returns "No Results Found" even for a document
    # that unambiguously exists inside the range. Costs nothing here since
    # every doc being looked up this way was already recorded in the past.
    today_str = (TODAY - timedelta(days=3)).strftime("%Y%m%d")
    # v1.4: was searchType=quickSearch&searchValue={doc_number} -- confirmed
    # live 2026-08-20 via the site's own Advanced Search UI that quickSearch
    # is a keyword/party-name search, NOT an exact document-number lookup;
    # it returned "No Results" for every single doc number regardless of
    # type or age, which is why this fallback had a 100% failure rate. The
    # real mechanism (found in the "Single Document Numbers" Advanced
    # Search field) is searchType=advancedSearch with documentNumberRange
    # as a JSON array. Verified working end-to-end against a real doc
    # (2026026641, DFLC INC -> BLACK ROBERT E) -- exact 1-result match,
    # doc_number itself was never wrong here (unlike the analogous bug on
    # Bexar), only the search URL was.
    doc_json = urllib.parse.quote(f'["{doc_number}"]')
    url = (
        f"{PUBLICSEARCH_BASE}/results"
        f"?department={department}"
        f"&documentNumberRange={doc_json}"
        f"&recordedDateRange=18000101%2C{today_str}"
        f"&searchType=advancedSearch"
    )
    # v1.5 diagnostics: the v1.4 fix was verified end-to-end in an
    # interactive browser but still failed 100% of the time in the actual
    # headless GitHub Actions run -- same URL, same doc, same click target.
    # Splitting this into separately-caught stages so the next real run's
    # log says exactly which step failed (page load? table never appeared?
    # click didn't navigate?) instead of one blank "Message: " for the
    # whole function, which told us nothing about where it was actually
    # breaking.
    try:
        driver.get(url)
    except Exception as e:
        log.warning(f"  docnumber-fallback [{doc_number}]: page load failed: {e}")
        return None, None, None

    # v1.6: the v1.5 title-based gate was wrong. The page_source dumped on
    # that failure was a fully hydrated 100KB+ React page with GTM/analytics
    # scripts loaded -- not a stuck loading shell. document.title just never
    # updates in headless mode (likely a visibility/focus-gated title effect
    # in the SPA), so gating on it timed out 100% of the time even when the
    # real results were already in the DOM. Wait on the table directly
    # instead -- that's the thing we actually need.
    # v1.9: that still didn't explain persistent timeouts even after v1.7's
    # date fix -- live-verified 2026-08-23 in an interactive (non-headless)
    # session that a genuine "No Results Found" page renders its own <h1>
    # with NO stable class (React CSS-in-JS hash like "css-z524vz", changes
    # per build), so it never matched a class-based no-results selector and
    # the wait spun for the FULL timeout on every zero-match lookup instead
    # of failing fast. XPath text match catches it regardless of class.
    try:
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located(
                (By.XPATH, "//table//tr/td | //h1[contains(text(),'No Results')]")
            )
        )
        if driver.find_elements(By.XPATH, "//h1[contains(text(),'No Results')]"):
            log.info(f"  docnumber-fallback [{doc_number}]: no results for this doc number")
            return None, None, None
    except Exception as e:
        log.warning(f"  docnumber-fallback [{doc_number}]: results table never appeared "
                    f"(url={driver.current_url!r}, title={driver.title!r}): {e}")
        try:
            src = driver.page_source
            log.warning(f"  docnumber-fallback [{doc_number}]: page_source len={len(src)} "
                        f"snippet={src[:800]!r}")
        except Exception as e2:
            log.warning(f"  docnumber-fallback [{doc_number}]: could not read page_source: {e2}")
        return None, None, None
    time.sleep(1)

    try:
        # v1.4: row.click() on the <tr> itself doesn't trigger the site's
        # row-navigation handler -- confirmed the row only navigates when a
        # data cell (td.col-3, first real column after the checkbox/icon
        # columns) is clicked directly, same pattern already used elsewhere
        # in this file for the main chunk-scrape table.
        cell = driver.find_element(By.CSS_SELECTOR, "td.col-3")
        cell.click()
        WebDriverWait(driver, timeout).until(EC.url_contains("/doc/"))
    except Exception as e:
        log.warning(f"  docnumber-fallback [{doc_number}]: click-through to /doc/ failed "
                    f"(url={driver.current_url!r}): {e}")
        return None, None, None
    time.sleep(1.5)

    result = _parse_address_from_current_page(driver, doc_number)
    if result != (None, None, None) or _hop:
        return result

    # v1.7: APPT docs genuinely have no Property Address field of their own
    # (TX law doesn't require it for this doc type) -- fall back to the
    # Deed of Trust it references via Marginal References, which does.
    # Confirmed live 2026-08-23 the referenced doc sits in the same
    # department as the APPT itself.
    text_plain = re.sub(r"<[^>]+>", " ", driver.page_source)
    text_plain = re.sub(r"\s+", " ", text_plain)
    m = DEED_OF_TRUST_REF_RE.search(text_plain)
    if not m:
        return None, None, None
    dot_doc_number = m.group(1)
    log.info(f"  docnumber-fallback [{doc_number}]: no address on APPT page — "
             f"hopping to referenced Deed of Trust {dot_doc_number}")
    return fetch_address_by_docnumber(driver, dot_doc_number, department, timeout, _hop=True)

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


# ── Address sanity check ────────────────────────────────────────────────────
# 2026-08-07: same bug found and fixed in bexar-leads applies here -- the old
# addr_candidates regex only checked "starts with digits then a letter", so
# page footer/copyright text ("2026 Nueces County... All Rights Reserved.")
# can slip through as a real address since the year reads as a street number.
# Require an actual street-suffix word and reject known boilerplate phrases.
STREET_SUFFIXES = {
    "ST","AVE","DR","RD","LN","CT","CIR","BLVD","WAY","PL","TRL","PKWY",
    "HWY","LOOP","PASS","CV","PT","HLS","TRAIL","GROVE","RIDGE","CREEK",
    "LAKE","PARK","GLEN","RUN","XING","STREET","AVENUE","DRIVE","ROAD",
    "LANE","COURT","CIRCLE","BOULEVARD","PLACE","TERRACE","TER","WALK",
    "ROW","BND","BEND","VW","VIEW","COVE","MNR","MANOR","SQ","SQUARE",
}
GARBAGE_ADDRESS_KEYWORDS = [
    "RIGHTS RESERVE", "COPYRIGHT", "ALL RIGHTS", "NUECES COUNTY,",
    "CLERK OF", "GOVOS", "ACCESSIBILITY",
]

def _looks_like_address(s):
    if not s:
        return False
    upper = s.upper()
    if any(kw in upper for kw in GARBAGE_ADDRESS_KEYWORDS):
        return False
    words = re.split(r"[\s,]+", upper)
    return any(w.rstrip(".") in STREET_SUFFIXES for w in words)

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

def scrape_publicsearch(department, lead_type, known_docs, driver, run_ts, days=None, doc_types=None):
    """
    Generic PublicSearch scraper for Nueces County.
    department: 'FC' (foreclosures) or 'RP' (real property)
    doc_types: docTypes code (e.g. 'APPNMT' for Appointment of Substitute
               Trustee) -- required for RP, ignored for FC.
    days: optional lookback window override (defaults to SCRAPE_DAYS).
          Used by backfill_30d.py to re-pull a shorter recent window.

    v2.0: replaced quickSearch+searchValue entirely. Live-verified
    2026-08-25 via the site's own Advanced Search UI that quickSearch's
    free-text searchValue never matches anything under a document-type-
    scoped department -- confirmed for "TAX", "APPOINTMENT", and even a
    generic keyword like "SMITH" under department=FC (quickSearch only
    works against the broad, unfiltered department listing, e.g. plain
    department=RP with no other filters). This is the actual reason
    NOF/TAX/APPT all showed "0 new records" every run since at least
    2026-08-21 even after the v1.7 date-range fix and v1.9 wait-selector
    fix landed -- both were real bugs, but the query mechanism itself was
    also wrong underneath them.

    Real mechanism per department, confirmed via the Advanced Search UI:
      - FC (Foreclosures): plain listing, searchType=advancedSearch, NO
        searchValue, instrumentDateRange (not recordedDateRange -- this
        field has no "Certified through" lag; a working query used an end
        date a month past today). Table has no GRANTOR/GRANTEE at all
        (Doc Type / Recorded / Sale Date / Doc# / legal description
        mislabeled "Property Address" in the header) -- owner comes from
        enrich_from_lookup() by legal description below, not this listing.
      - RP (Land Records) needs docTypes=<code> as its own param (e.g.
        APPNMT for Appointment of Substitute Trustee) plus
        searchType=advancedSearch and recordedDateRange (3-day-buffered,
        same v1.7 fix). This table has the full GRANTOR/GRANTEE/LEGAL
        DESCRIPTION/LOT/BLOCK structure the parsing below already expects.
    """
    new_records = []
    window = days if days is not None else SCRAPE_DAYS
    cutoff = (TODAY - timedelta(days=window)).strftime("%Y%m%d")
    offset = 0
    consecutive_empty = 0

    log.info(f"Scraping {department}/{doc_types or lead_type} ({lead_type})...")

    while True:
        if department == "FC":
            end_str = (TODAY + timedelta(days=30)).strftime("%Y%m%d")
            url = (
                f"{PUBLICSEARCH_BASE}/results"
                f"?department=FC"
                f"&instrumentDateRange={cutoff}%2C{end_str}"
                f"&keywordSearch=false"
                f"&limit=50"
                f"&offset={offset}"
                f"&sort=desc"
                f"&sortBy=recordedDate"
                f"&searchType=advancedSearch"
            )
        else:
            end_str = (TODAY - timedelta(days=3)).strftime("%Y%m%d")
            url = (
                f"{PUBLICSEARCH_BASE}/results"
                f"?department={department}"
                f"&docTypes={doc_types}"
                f"&recordedDateRange={cutoff}%2C{end_str}"
                f"&keywordSearch=false"
                f"&limit=50"
                f"&offset={offset}"
                f"&sort=desc"
                f"&sortBy=recordedDate"
                f"&searchType=advancedSearch"
            )
        log.info(f"  offset={offset}")

        try:
            driver.get(url)
            # v1.9: the .no-results/[class*='no-result'] selector never
            # matched anything -- live-verified 2026-08-23 that a genuine
            # "No Results Found" page renders an <h1> with a React CSS-in-JS
            # hashed class (e.g. "css-z524vz"), not a stable "no-result"
            # class name. That's why this wait timed out on EVERY zero-match
            # search instead of the "no results" check below ever running --
            # not a bot-detection or headless-only issue as it first looked.
            WebDriverWait(driver, 30).until(
                EC.presence_of_element_located(
                    (By.XPATH, "//table//tr/td | //h1[contains(text(),'No Results')]")
                )
            )
            time.sleep(2)
        except Exception as e:
            # v1.8: dump what's actually on the page when this times out --
            # every prior "Timeout offset=X" log has been a blank "Message: "
            # with no way to tell whether it's a slow real page, a genuine
            # "No Results" state that just doesn't match either selector, or
            # a bot-challenge interstitial (Cloudflare etc.) that headless
            # Chrome hits but an interactive session doesn't. Same diagnostic
            # pattern already proven useful on fetch_address_by_docnumber.
            try:
                src = driver.page_source
                log.warning(f"  Timeout offset={offset} (url={driver.current_url!r}, "
                            f"title={driver.title!r}): {e} | page_source len={len(src)} "
                            f"snippet={src[:600]!r}")
            except Exception as e2:
                log.warning(f"  Timeout offset={offset}: {e} | could not read page_source: {e2}")
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
                and _looks_like_address(c)
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
            # 2026-08-21: added the LOT/BLOCK/SECTION/etc exclusion -- legal
            # description cells ("Padre Island Corpus Christi Ports O Call-
            # Lot 4, Block 10") weren't excluded here, so one could slip
            # through and get assigned as "owner" since it doesn't match any
            # ENTITY_FILTER_KW keyword either. Same regex already used below
            # to capture the `legal` variable.
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
                and not re.search(r"\b(LOT|BLOCK|SECTION|SUBDIVISION|SUBD|TRACT|ABSTRACT)\b", c, re.IGNORECASE)
            ]

            grantor = ""
            for cand in name_candidates:
                if not is_entity_name(cand):
                    grantor = cand
                    break
            # 2026-08-21: removed the "first candidate as last resort" fallback
            # that used to run here. On APPT docs (Appointment of Substitute
            # Trustee), grantor/grantee are the lender and the substitute
            # trustee company -- there's no borrower name on the document at
            # all -- so when every name candidate is an entity, that fallback
            # was silently assigning the lender's name (e.g. "Rocket Mortgage
            # Llc", "Lakeview Loan Servicing Llc") as the owner. Leaving owner
            # blank here lets enrich_from_lookup() below fill it in from the
            # county tax roll instead, which has the real owner.

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

    log.info(f"{department}/{doc_types or lead_type}: {len(new_records)} new records")
    return new_records

# ── Code Enforcement Scraper ──────────────────────────────────────────────────
def scrape_code_enforcement(known_docs, run_ts):
    """
    DISABLED 2026-08-10 — call site above now short-circuits to [] before this
    ever runs. Left in place in case a real endpoint shows up later.

    Confirmed dead end, checked 2026-08-10:
    - Layer 43 on this FeatureServer is named "NCad_Parcels" but its actual
      schema (fetched live) is only OBJECTID/AREA/PERIMETER/COUNTIES_/CODE/
      NAME/ZONE/Shape__Area/Shape__Length — a boundary/zone layer, not
      property-level parcel data. None of the outFields this function asks
      for (PROP_ID, situs_disp, file_as_na, addr_zip, addr_city, appraised_,
      state_cd) exist on it, hence the permanent 400.
    - CC_CODE_URL above (the geojson constant) is a dead placeholder — that
      hash isn't a real ArcGIS dataset id and nothing in this file uses it.
    - Searched ArcGIS Online's public item index directly for Corpus
      Christi code-enforcement/compliance/violation datasets — nothing
      exists under this org or any other.
    - The only real code-compliance data the city publishes is monthly PDF
      "Citation Activity" reports (corpuschristitx.gov) — aggregate stats
      only (e.g. citation counts), no individual case/address/owner data,
      so not usable for lead generation even as a slower fallback.
    - Conclusion: there is no public, case-level code-enforcement data
      source for Corpus Christi/Nueces right now. Not a quick fix — would
      need the city to actually publish one, or a different discovery pass
      if their data posture changes.
    """
    new_records = []
    log.info("Scraping Corpus Christi code enforcement...")

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


# ── On-market status (HomeHarvest / Realtor.com) ──────────────────────────────
def fetch_on_market_status(records):
    """
    Flags leads that are already listed for sale/rent elsewhere, using
    homeharvest (pip, MIT license) against Realtor.com's public page data —
    no API key, no cost. Same approach as bexar-leads' ARV lookup, but only
    pulls the `status` field here since Nueces doesn't have an ARV feature
    to piggyback on.

    Soft dependency: any failure (network, no match, library error) just
    leaves on_market unset for that lead rather than breaking the run.
    Two passes: never-checked leads first (ON_MARKET_FETCH_LIMIT), then a
    refresh of already-checked leads older than ON_MARKET_REFRESH_DAYS
    (ON_MARKET_REFRESH_LIMIT) — a lead can get listed by someone else weeks
    after we first looked, so a one-time check isn't enough.
    """
    import pandas as pd
    from homeharvest import scrape_property

    def clean(val):
        if val is None or pd.isna(val):
            return None
        s = str(val).strip()
        return None if s in ("", "nan", "<NA>", "None") else val

    cutoff = datetime.now() - timedelta(days=ON_MARKET_REFRESH_DAYS)

    def needs_check(r):
        if not r.get("address"):
            return False
        checked_at = r.get("on_market_checked_at")
        if not checked_at:
            return True
        try:
            return datetime.strptime(checked_at, "%Y-%m-%dT%H:%M:%SZ") < cutoff
        except Exception:
            return True

    never_checked = [r for r in records if r.get("address") and not r.get("on_market_checked_at")]
    stale_checked = [r for r in records if r.get("address") and r.get("on_market_checked_at") and needs_check(r)]

    candidates = never_checked[:ON_MARKET_FETCH_LIMIT] + stale_checked[:ON_MARKET_REFRESH_LIMIT]

    if not candidates:
        log.info("On-market: no eligible leads — skipping")
        return records

    log.info(f"On-market: {len(never_checked[:ON_MARKET_FETCH_LIMIT])} new + "
             f"{len(stale_checked[:ON_MARKET_REFRESH_LIMIT])} refresh "
             f"(caps={ON_MARKET_FETCH_LIMIT}/{ON_MARKET_REFRESH_LIMIT})")
    changed = 0
    errors  = 0

    for rec in candidates:
        full_addr = f"{rec['address']}, {rec.get('city', '')}, TX {rec.get('zip', '')}".strip(", ")
        try:
            df = scrape_property(location=full_addr)
            now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            was_on_market = bool(rec.get("on_market"))

            if df is None or len(df) == 0:
                rec["on_market_checked_at"] = now_iso
                continue

            status = clean(df.iloc[0].get("status")) or ""
            rec["on_market"]            = status in ON_MARKET_STATUSES
            rec["on_market_status"]     = status
            rec["on_market_checked_at"] = now_iso

            if rec["on_market"] != was_on_market:
                changed += 1
                log.info(f"  On-market [{rec.get('doc_number')}] {full_addr}: "
                         f"{was_on_market} -> {rec['on_market']} (status={status})")
        except Exception as e:
            log.warning(f"  On-market [{rec.get('doc_number')}] {full_addr}: error: {e}")
            errors += 1
        finally:
            time.sleep(1)

    log.info(f"On-market: {changed} status changes, {errors} errors out of {len(candidates)} candidates")
    return records


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
        # Source 1: FC department — all foreclosure notices. v2.0: dropped the
        # separate TAX-specific call -- it used quickSearch&searchValue=TAX,
        # which never matched anything under department=FC (see
        # scrape_publicsearch's v2.0 note) and every doc type seen here so
        # far is "FORECLOSURE NOTICE" anyway (no evidence Nueces files a
        # distinct TAX deed type through this department). If a doc's own
        # "Doc Type" text ever contains TAX, classify it from within this
        # single pass instead of a second full scrape for a type that may
        # not exist.
        nof_recs = scrape_publicsearch(
            department="FC",
            lead_type="NOF",
            known_docs=known_docs,
            driver=driver,
            run_ts=run_ts,
        )
        all_new.extend(nof_recs)

        # Source 3: RP department — Appointment of Substitute Trustee (Pre-Fore).
        # v2.0: docTypes=APPNMT is the real filter mechanism (live-verified via
        # the Advanced Search UI) -- searchType=quickSearch&searchValue=
        # APPOINTMENT never matched anything.
        appt_recs = scrape_publicsearch(
            department="RP",
            doc_types="APPNMT",
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

        # ── Selenium fallback: any lead still missing address after roll match ──
        # v1.3: covers the full backlog (enrich_targets = all_new + existing),
        # not just this run's new NOF/TAX leads -- see fetch_address_by_docnumber
        # docstring for why the old all_new-only, NOF/TAX-only, source_url-gated
        # version left the entire APPT backlog permanently unaddressed.
        still_missing = [
            r for r in enrich_targets
            if r.get("type") in ("NOF", "TAX", "APPT") and not r.get("address") and r.get("doc_number")
        ]
        log.info(f"Selenium fallback: {len(still_missing)} leads still missing address (backlog + new)")
        fetched = 0
        for rec in still_missing[:MAX_DOC_FETCH]:
            department = DEPT_BY_TYPE.get(rec.get("type"), "RP")
            street, city, zipc = fetch_address_by_docnumber(driver, rec["doc_number"], department)
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

    # Source 4: Code Enforcement — DISABLED 2026-08-10, see scrape_code_enforcement()
    # docstring for why. Re-enable once a real case-level endpoint is confirmed.
    ce_recs = []
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

    # ── On-market status ──────────────────────────────────────────────────────
    try:
        all_records = fetch_on_market_status(all_records)
    except Exception as e:
        log.warning(f"On-market status error: {e}")

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
