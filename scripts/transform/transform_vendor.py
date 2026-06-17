"""Transform Odoo vendors (/api/vendors_details) into the flat CSV the Flask
loader posts to /api/migration/vendor.

Odoo source fields -> Flask:
    vendor_name         -> company_name
    email               -> contact_email (vendor contact User)
    contact_person      -> contact_person (User.name)
    vendor_contact      -> contact_phone (User.phone)
    vendor_website_url  -> website
    date_of_rollout     -> contract_start
    last_overall_risk   -> risk_level   (see map_vendor_risk)
    state               -> vra_status   (see map_vendor_vra)
    department_ids[].name -> processing_activities (name -> Flask PA id in loader)
    id                  -> odoo_source_id (idempotency source-map)

`status` (Active/Inactive) has no Odoo equivalent -> default "Active".
"""

import base64
import binascii
import json
import logging
import os
import re

import pandas as pd
from dotenv import load_dotenv

load_dotenv("config/.env")
DATA_RAW_DIR = os.getenv("DATA_RAW_DIR", "data/raw")
DATA_PROCESSED_DIR = os.getenv("DATA_PROCESSED_DIR", "data/processed")
# Decoded attachment bytes live here as sidecar files (kept out of the CSV so it
# stays small/clean); the loader re-encodes them at POST time. See manifest.
DATA_ATTACH_DIR = os.getenv("DATA_ATTACH_DIR", "data/attachments")
VENDOR_ATTACH_MANIFEST = "vendor_attachments_manifest.json"

# Odoo vendor attachment fields carrying inline Base64 ({fileName, fileContent}).
# The backend maps nda_attachment->nda_document, vra_attachment->dpa_document.
VENDOR_ATTACHMENT_FIELDS = ("nda_attachment", "vra_attachment")

logger = logging.getLogger("transform_vendor")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")


def _safe_name(name: str) -> str:
    """Filesystem-safe sidecar filename (the real secure_filename runs again in
    the backend upload service; this only protects the local sidecar path)."""
    name = os.path.basename(str(name or "").strip())
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    return name or "attachment"


def _extract_attachments(vendor: dict, odoo_id) -> dict:
    """Decode any inline Base64 vendor attachments to sidecar files under
    data/attachments/vendor/<odoo_id>/ and return a manifest entry:

        {field: {"fileName": <name>, "path": <relative sidecar path>}}

    Tolerant: a field that is absent, not an object, or missing fileContent is
    skipped (so the list endpoint without attachments still transforms cleanly).
    """
    entry = {}
    for field in VENDOR_ATTACHMENT_FIELDS:
        obj = vendor.get(field)
        if not isinstance(obj, dict):
            continue
        content = obj.get("fileContent")
        file_name = obj.get("fileName")
        if not content or not file_name:
            continue
        try:
            raw = base64.b64decode(content, validate=True)
        except (binascii.Error, ValueError):
            try:
                raw = base64.b64decode(content)
            except (binascii.Error, ValueError):
                logger.warning(f"Vendor {odoo_id} {field}: invalid base64, skipping attachment.")
                continue
        if not raw:
            logger.warning(f"Vendor {odoo_id} {field}: empty decoded content, skipping.")
            continue

        rel_dir = os.path.join(DATA_ATTACH_DIR, "vendor", str(odoo_id))
        os.makedirs(rel_dir, exist_ok=True)
        rel_path = os.path.join(rel_dir, f"{field}__{_safe_name(file_name)}")
        with open(rel_path, "wb") as f:
            f.write(raw)
        entry[field] = {"fileName": str(file_name), "path": rel_path}
        logger.info(f"Vendor {odoo_id} {field}: wrote {len(raw)} B sidecar -> {rel_path}")
    return entry


def _clean(value) -> str:
    """Odoo emits `false` (bool) for empty fields; coerce those + None to ''."""
    if value is None or value is False:
        return ""
    s = str(value).strip()
    return "" if s.lower() in ("false", "nan", "none") else s


def map_vendor_risk(value) -> str:
    """Odoo last_overall_risk -> Flask risk_level enum (Low|Medium|High).
    Real values are digit-prefixed colours: '1green', '2amber', (red), or
    `false`. Strip digits, match colour. Empty/false -> '' => loader leaves NULL.
    """
    s = _clean(value).lower()
    s = "".join(c for c in s if not c.isdigit()).strip()
    return {"green": "Low", "amber": "Medium", "red": "High"}.get(s, "")


def map_vendor_vra(state) -> str:
    """Odoo `state` -> Flask vra_status enum (Completed|In Progress|Pending).
    not_started -> Pending, submitted -> In Progress, approval -> Completed.
    Unknown -> Pending (the Flask default)."""
    s = _clean(state).lower()
    return {
        "not_started": "Pending",
        "submitted": "In Progress",
        "approval": "Completed",
    }.get(s, "Pending")


def to_iso_datetime(raw) -> str:
    """Odoo timestamp -> ISO 'YYYY-MM-DDTHH:MM:SS' (or '' when empty)."""
    s = _clean(raw)
    if not s:
        return ""
    try:
        return pd.to_datetime(s).strftime("%Y-%m-%dT%H:%M:%S")
    except Exception:
        return ""


def _extract_records(raw):
    """vendors_details may be a list or wrapped in a dict key."""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for key in ("data", "vendors", "vendors_details", "result"):
            if isinstance(raw.get(key), list):
                return raw[key]
    return []


def transform_vendor_data(input_filename: str = "raw_vendors.json",
                          output_filename: str = "processed_vendors.csv"):
    input_path = os.path.join(DATA_RAW_DIR, input_filename)
    if not os.path.exists(input_path):
        logger.error(f"Input file not found: {input_path}")
        return

    with open(input_path, encoding="utf-8") as f:
        raw = json.load(f)

    records = _extract_records(raw)
    logger.info(f"Loaded {len(records)} vendors from {input_filename}")

    rows = []
    manifest = {}
    for v in records:
        if not isinstance(v, dict):
            continue
        depts = v.get("department_ids") or []
        pa_names = [d.get("name") for d in depts if isinstance(d, dict) and d.get("name")]

        # Decode inline Base64 documents to sidecar files; record per-vendor in
        # the manifest the loader reads (keyed by Odoo id). Never embed Base64 in
        # the CSV.
        attach_entry = _extract_attachments(v, v.get("id"))
        if attach_entry:
            manifest[str(v.get("id"))] = attach_entry

        rows.append({
            "odoo_source_id": v.get("id"),
            "company_name": _clean(v.get("vendor_name")),
            "email": _clean(v.get("email")),
            "contact_person": _clean(v.get("contact_person")),
            "phone": _clean(v.get("vendor_contact")),
            "website": _clean(v.get("vendor_website_url")),
            "contract_start": to_iso_datetime(v.get("date_of_rollout")),
            # Odoo last_overall_risk -> risk_level; '' => NULL in Flask.
            "risk_level": map_vendor_risk(v.get("last_overall_risk")),
            # Odoo state -> vra_status. status has no Odoo source -> Active.
            "vra_status": map_vendor_vra(v.get("state")),
            "status": "Active",
            "processing_activity_names": pa_names,
        })

    os.makedirs(DATA_PROCESSED_DIR, exist_ok=True)
    output_path = os.path.join(DATA_PROCESSED_DIR, output_filename)
    pd.DataFrame(rows).to_csv(output_path, index=False)
    logger.info(f"Transformation complete. Saved {len(rows)} vendors to {output_path}")

    # Manifest sits next to the CSV; loader joins on odoo_source_id. Always
    # (re)write it — even empty — so a stale manifest from a prior run can't
    # attach files to the wrong vendors.
    manifest_path = os.path.join(DATA_PROCESSED_DIR, VENDOR_ATTACH_MANIFEST)
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    logger.info(f"Wrote vendor attachment manifest ({len(manifest)} vendors) -> {manifest_path}")


if __name__ == "__main__":
    pass
