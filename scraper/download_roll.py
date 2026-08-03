"""
download_roll.py — Nueces County CAD GIS/Roll Downloader
Downloads the Nueces CAD GIS Shapefile (owner + address + appraised value)
and converts it to a compressed CSV lookup table used by fetch.py.

Runs twice per year via GitHub Actions:
  - April 5  (after Preliminary roll drops ~April 1)
  - September 5 (after Certified roll drops ~September 1)

Output: scraper/nueces_lookup.csv.gz
Columns: legal_desc, owner, situs_addr, situs_city, situs_zip, appraised_value, prop_type, absentee

The GIS shapefile contains parcel polygons with appraisal data joined.
We extract attributes only (no geometry needed).
"""

import io
import csv
import gzip
import json
import logging
import os
import re
import shutil
import struct
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ── URLs ─────────────────────────────────────────────────────────────────────
# GIS Shapefile — updated ~monthly, much smaller than full appraisal roll
GIS_URL = "https://nuecescad.net/wp-content/uploads/2026/06/Parcels_260529.zip"

# Appraisal Roll Flat File — fallback if GIS doesn't have what we need
ROLL_URL = "https://nuecescad.net/wp-content/uploads/2026/04/2026-Preliminary-Public-Export-20260402.zip"

OUTPUT_PATH = Path("scraper/nueces_lookup.csv.gz")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; NuecesLeads/1.0)"}

def download_file(url, dest_path, label="file"):
    log.info(f"Downloading {label} from {url}")
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=300) as r:
        size = int(r.headers.get("Content-Length", 0))
        log.info(f"Size: {size/1024/1024:.1f} MB")
        with open(dest_path, "wb") as f:
            downloaded = 0
            while True:
                chunk = r.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                if size:
                    pct = downloaded / size * 100
                    if downloaded % (10*1024*1024) < 1024*1024:
                        log.info(f"  {pct:.0f}% ({downloaded/1024/1024:.0f} MB)")
    log.info(f"Downloaded to {dest_path}")

def read_dbf(dbf_path):
    """
    Read a dBASE III (.dbf) file without external dependencies.
    Returns list of dicts.
    """
    records = []
    with open(dbf_path, "rb") as f:
        # Header
        f.read(4)  # version, year, month, day
        num_records = struct.unpack("<I", f.read(4))[0]
        header_size = struct.unpack("<H", f.read(2))[0]
        record_size = struct.unpack("<H", f.read(2))[0]
        f.read(20)  # reserved

        # Field descriptors
        fields = []
        while True:
            field_desc = f.read(32)
            if field_desc[0] == 0x0D:  # terminator
                break
            name = field_desc[:11].rstrip(b"\x00").decode("latin-1")
            ftype = chr(field_desc[11])
            length = field_desc[16]
            fields.append((name, ftype, length))

        # Seek to data
        f.seek(header_size)

        for _ in range(num_records):
            deletion_flag = f.read(1)
            if deletion_flag == b"*":
                f.read(record_size - 1)
                continue
            row = {}
            for name, ftype, length in fields:
                raw = f.read(length).decode("latin-1", errors="replace").strip()
                row[name] = raw
            records.append(row)

    log.info(f"DBF: read {len(records)} records, {len(fields)} fields")
    if records:
        log.info(f"DBF fields: {list(records[0].keys())}")
    return records, [f[0] for f in fields]

def normalize_legal(s):
    """Normalize legal description for fuzzy matching."""
    if not s:
        return ""
    s = s.upper().strip()
    s = re.sub(r"\s+", " ", s)
    return s

def is_absentee(situs_addr, mail_addr):
    """Check if mailing address differs from situs (property) address."""
    if not situs_addr or not mail_addr:
        return False
    s1 = re.sub(r"\s+", "", situs_addr.upper())
    s2 = re.sub(r"\s+", "", mail_addr.upper())
    return s1 not in s2 and s2 not in s1

def is_entity(owner):
    """Check if owner is LLC/Corp/Trust."""
    if not owner:
        return False
    upper = owner.upper()
    keywords = ["LLC", "L.L.C", "INC", "CORP", "LTD", "TRUST", "HOLDINGS",
                "PARTNERS", "GROUP", "COMPANY", " CO ", "BANK", "ASSOC"]
    return any(k in upper for k in keywords)

def process_gis_shapefile(zip_path, tmpdir):
    """Extract GIS shapefile and read the DBF for attributes."""
    log.info("Processing GIS shapefile...")
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        log.info(f"ZIP contents: {names}")
        dbf_files = [n for n in names if n.lower().endswith(".dbf")]
        if not dbf_files:
            log.error("No .dbf file found in GIS shapefile ZIP")
            return None
        dbf_name = dbf_files[0]
        log.info(f"Extracting {dbf_name}")
        z.extract(dbf_name, tmpdir)
        dbf_path = os.path.join(tmpdir, dbf_name)

    records, fields = read_dbf(dbf_path)
    return records, fields

def process_appraisal_roll(zip_path, tmpdir):
    """
    Extract and parse Nueces CAD flat file appraisal roll.
    The roll ZIP contains multiple fixed-width text files.
    We look for the main property file (REAL.txt or similar).
    """
    log.info("Processing appraisal roll flat files...")
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        log.info(f"Roll ZIP contents: {names[:20]}")
        # Extract all text files
        txt_files = [n for n in names if n.lower().endswith((".txt", ".csv"))]
        log.info(f"Text files: {txt_files}")
        for tf in txt_files:
            z.extract(tf, tmpdir)
    return txt_files

def build_lookup_from_gis(records, fields):
    """Build lookup dict from GIS DBF records."""
    # Try to identify key field names (vary by shapefile version)
    field_map = {}
    field_lower = {f.lower(): f for f in fields}

    # Owner name field
    for candidate in ["owner", "owner1", "ownername", "owner_name", "own_name"]:
        if candidate in field_lower:
            field_map["owner"] = field_lower[candidate]
            break

    # Situs (property) address
    for candidate in ["situs", "situsaddr", "situs_addr", "prop_addr", "address",
                      "site_addr", "str_addr"]:
        if candidate in field_lower:
            field_map["situs"] = field_lower[candidate]
            break

    # Situs street number
    for candidate in ["situsnum", "situs_num", "str_num", "addr_num", "hse_num"]:
        if candidate in field_lower:
            field_map["situs_num"] = field_lower[candidate]
            break

    # Situs street name
    for candidate in ["situsstr", "situs_str", "str_name", "addr_str", "str_dir"]:
        if candidate in field_lower:
            field_map["situs_str"] = field_lower[candidate]
            break

    # City
    for candidate in ["city", "situs_city", "prop_city", "situscity"]:
        if candidate in field_lower:
            field_map["city"] = field_lower[candidate]
            break

    # ZIP
    for candidate in ["zip", "zipcode", "zip_code", "situszip", "situs_zip"]:
        if candidate in field_lower:
            field_map["zip"] = field_lower[candidate]
            break

    # Appraised value
    for candidate in ["tot_val", "totval", "appraised", "mkt_val", "total_val",
                      "land_val", "impr_val", "tot_appr"]:
        if candidate in field_lower:
            field_map["value"] = field_lower[candidate]
            break

    # Legal description
    for candidate in ["legal", "legal_desc", "legal_des", "legaldesc", "descript"]:
        if candidate in field_lower:
            field_map["legal"] = field_lower[candidate]
            break

    # Property type
    for candidate in ["prop_type", "state_cd", "class_cd", "use_code", "prop_cd"]:
        if candidate in field_lower:
            field_map["prop_type"] = field_lower[candidate]
            break

    # Mail address (for absentee detection)
    for candidate in ["mail_addr", "mailaddr", "mail_str", "mailing"]:
        if candidate in field_lower:
            field_map["mail"] = field_lower[candidate]
            break

    log.info(f"Field mapping: {field_map}")
    log.info(f"All available fields: {fields}")

    lookup = []
    for rec in records:
        owner = rec.get(field_map.get("owner", ""), "").strip()
        if not owner:
            continue

        # Build situs address
        situs = rec.get(field_map.get("situs", ""), "").strip()
        if not situs:
            num = rec.get(field_map.get("situs_num", ""), "").strip()
            st = rec.get(field_map.get("situs_str", ""), "").strip()
            if num and st:
                situs = f"{num} {st}"

        city = rec.get(field_map.get("city", ""), "CORPUS CHRISTI").strip() or "CORPUS CHRISTI"
        zip_code = rec.get(field_map.get("zip", ""), "").strip()
        value = rec.get(field_map.get("value", ""), "").strip()
        legal = rec.get(field_map.get("legal", ""), "").strip()
        prop_type = rec.get(field_map.get("prop_type", ""), "").strip()
        mail = rec.get(field_map.get("mail", ""), "").strip()

        lookup.append({
            "legal_desc":      normalize_legal(legal),
            "owner":           owner.upper(),
            "situs_addr":      situs.upper(),
            "situs_city":      city.upper(),
            "situs_zip":       zip_code,
            "appraised_value": value,
            "prop_type":       prop_type,
            "absentee":        "1" if is_absentee(situs, mail) else "0",
            "is_entity":       "1" if is_entity(owner) else "0",
        })

    log.info(f"Built {len(lookup)} lookup records")
    return lookup

def save_lookup(lookup, output_path):
    """Save lookup to gzipped CSV."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cols = ["legal_desc", "owner", "situs_addr", "situs_city",
            "situs_zip", "appraised_value", "prop_type", "absentee", "is_entity"]
    with gzip.open(output_path, "wt", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=cols)
        writer.writeheader()
        writer.writerows(lookup)
    size = output_path.stat().st_size / 1024 / 1024
    log.info(f"Saved {len(lookup)} records to {output_path} ({size:.1f} MB)")

def main():
    with tempfile.TemporaryDirectory() as tmpdir:
        # Try GIS shapefile first (smaller, more current)
        gis_zip = os.path.join(tmpdir, "gis.zip")
        records = None
        fields = None

        try:
            download_file(GIS_URL, gis_zip, "GIS Shapefile")
            result = process_gis_shapefile(gis_zip, tmpdir)
            if result:
                records, fields = result
                log.info(f"GIS shapefile: {len(records)} parcels")
        except Exception as e:
            log.warning(f"GIS download failed: {e} — will try appraisal roll")

        # Fallback to appraisal roll if GIS failed
        if not records:
            log.info("Falling back to appraisal roll...")
            roll_zip = os.path.join(tmpdir, "roll.zip")
            try:
                download_file(ROLL_URL, roll_zip, "Appraisal Roll")
                txt_files = process_appraisal_roll(roll_zip, tmpdir)
                log.info(f"Roll extracted: {txt_files}")
                log.warning("Roll flat file parsing requires format guide — manual step needed")
                sys.exit(1)
            except Exception as e:
                log.error(f"Both downloads failed: {e}")
                sys.exit(1)

        # Build and save lookup
        if records and fields:
            lookup = build_lookup_from_gis(records, fields)
            if lookup:
                save_lookup(lookup, OUTPUT_PATH)
                log.info("Done — lookup table ready for fetch.py")
            else:
                log.error("No lookup records built — check field mapping")
                sys.exit(1)

if __name__ == "__main__":
    main()
