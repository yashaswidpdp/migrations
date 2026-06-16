"""
Transform Odoo Processing Activities tree into a flat list ready for Flask.

Odoo returns a nested tree (children arrays). Flask's create endpoint accepts
flat records with an optional parent_id. This transform flattens the tree
depth-first, recording each node's parent name so the loader can resolve
Flask parent IDs at load time.
"""
import json
import logging
import os
from datetime import datetime
from dotenv import load_dotenv

load_dotenv("config/.env")
DATA_RAW_DIR = os.getenv("DATA_RAW_DIR", "data/raw")
DATA_PROCESSED_DIR = os.getenv("DATA_PROCESSED_DIR", "data/processed")

logger = logging.getLogger("transform_processing_activity")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")


ODOO_TO_FLASK_ACTIVITY_TYPE = {
    "Mandatory/Regulatory": "Mandatory/Regulatory",
    "mandatory": "Mandatory/Regulatory",
    "Promotional": "Promotional",
    "promotional": "Promotional",
}


def _to_iso(value) -> str:
    """Normalise an Odoo effective-from value (e.g. '2025-12-12' or
    '2025-12-12 10:30:00') to ISO 8601 so the Flask /processing/create route can
    store it. Returns "" for empty/false/unparseable so the loader omits it."""
    if not value or value is False:
        return ""
    s = str(value).strip()
    if not s:
        return ""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%dT%H:%M:%S")
        except ValueError:
            continue
    return ""


def _yes_no(value, default: bool = False) -> bool:
    """Odoo sends boolean-ish flags as the strings 'yes'/'no' (and sometimes
    true/false). bool('no') is True in Python, so parse explicitly."""
    if value is None or value == "":
        return default
    return str(value).strip().lower() in ("yes", "true", "1", "t")


def _otp_minutes(value):
    """Odoo otpExpiryConsent is a duration like '24h' / '30m' / '1d'. Flask
    stores otp_validity_minutes as an int. Returns None for empty/unparseable
    so the loader omits it (Flask then keeps its company default)."""
    if value is None:
        return None
    s = str(value).strip().lower()
    if not s:
        return None
    units = {"d": 1440, "h": 60, "m": 1, "s": 0}
    if s[-1] in units:
        try:
            return int(float(s[:-1]) * units[s[-1]])
        except ValueError:
            return None
    try:
        return int(float(s))  # bare number -> assume minutes
    except ValueError:
        return None


def _int_or_none(value):
    """Parse Odoo consentValidity ('' or '12') to int months, else None."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _odoo_ref_id(value):
    """Odoo M2O fields are [id, "Name"] (or "" / false). Return the numeric id,
    kept for traceability only — Flask resolves links by name, not Odoo id."""
    if isinstance(value, (list, tuple)) and len(value) > 0:
        try:
            return int(value[0])
        except (TypeError, ValueError):
            return None
    return None


def _template_name(value) -> str:
    """Odoo template M2O fields arrive as [id, "Name"] or "" / false / None.

    The Odoo id is useless to Flask (ids differ post-migration), so we carry the
    NAME and let the loader resolve it against Flask's template name->id map.
    Returns None when no template is linked."""
    if isinstance(value, (list, tuple)) and len(value) > 1:
        name = str(value[1]).strip()
        return name or None
    return None


def _parse_array_or_false(value) -> list:
    """Odoo returns [] or false for empty M2M fields."""
    if not value or value is False:
        return []
    if isinstance(value, list):
        return value
    return []


def _flatten_tree(nodes: list, parent_name: str = None) -> list:
    """Depth-first flatten of the Odoo PA tree."""
    flat = []
    for node in nodes:
        if not isinstance(node, dict):
            continue

        name = str(node.get("name", "")).strip()
        if not name:
            continue

        activity_type_raw = node.get("processingActivityType", "Mandatory/Regulatory")
        activity_type = ODOO_TO_FLASK_ACTIVITY_TYPE.get(
            str(activity_type_raw).strip(), "Mandatory/Regulatory"
        )

        manager_val = node.get("managerId")
        manager_name = None
        if manager_val and manager_val is not False:
            if isinstance(manager_val, list) and len(manager_val) > 1:
                manager_name = str(manager_val[1]).strip()
            elif isinstance(manager_val, dict):
                manager_name = str(manager_val.get("name", "")).strip() or None
            elif isinstance(manager_val, str):
                manager_name = manager_val.strip()

        record = {
            "odoo_id": node.get("id"),
            "odoo_level": node.get("level"),  # tree depth, traceability only
            "name": name,
            "parent_name": parent_name,
            "description": str(node.get("description", "") or "").strip() or None,
            "activity_type": activity_type,
            "is_active": _yes_no(node.get("isActive"), default=True),
            "is_otp": _yes_no(node.get("isOtpMandatory")),
            "show_on_dpgr": _yes_no(node.get("showOnDpgr")),
            "show_on_privacy": _yes_no(node.get("showOnDpia")),
            "manager_name": manager_name,
            "manager_odoo_id": _odoo_ref_id(node.get("managerId")),
            # Odoo validity settings -> Flask columns (set via PUT; create
            # ignores these and would otherwise apply company defaults).
            "consent_validity_months": _int_or_none(node.get("consentValidity")),
            "otp_validity_minutes": _otp_minutes(node.get("otpExpiryConsent")),
            # Odoo effective-from dates -> Flask PA effective_from_* columns.
            "effective_from_email": _to_iso(node.get("consentEmailEffectiveFrom")),
            "effective_from_sms": _to_iso(node.get("consentSmsEffectiveFrom")),
            "effective_from_privacy": _to_iso(node.get("privacyEffectiveFrom")),
            # Odoo template links (name only; loader resolves to Flask id).
            "consent_email_template_name": _template_name(node.get("consentEmailTemplateId")),
            "consent_sms_template_name": _template_name(node.get("consentSmsTemplateId")),
            "privacy_template_name": _template_name(node.get("privacyTemplateId")),
            # Odoo template ids — traceability only; Flask links by name.
            "consent_email_template_odoo_id": _odoo_ref_id(node.get("consentEmailTemplateId")),
            "consent_sms_template_odoo_id": _odoo_ref_id(node.get("consentSmsTemplateId")),
            "privacy_template_odoo_id": _odoo_ref_id(node.get("privacyTemplateId")),
        }
        flat.append(record)

        children = node.get("children", [])
        if children:
            flat.extend(_flatten_tree(children, parent_name=name))

    return flat


def transform_processing_activity_data(input_filename: str, output_filename: str):
    """
    Read raw JSON from data/raw/<input_filename>, flatten the PA tree,
    write flat CSV to data/processed/<output_filename>.

    The raw file must be a JSON array (the processingActivities array from Odoo).
    """
    input_path = os.path.join(DATA_RAW_DIR, input_filename)
    if not os.path.exists(input_path):
        logger.error(f"Input file not found: {input_path}")
        return

    with open(input_path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    # raw may be the full response or just the processingActivities array
    if isinstance(raw, dict):
        nodes = raw.get("processingActivities", raw.get("data", []))
    else:
        nodes = raw

    logger.info(f"Loaded {len(nodes)} root-level PA nodes from {input_filename}")

    flat = _flatten_tree(nodes)
    logger.info(f"Flattened to {len(flat)} total PA records (including children)")

    os.makedirs(DATA_PROCESSED_DIR, exist_ok=True)
    output_path = os.path.join(DATA_PROCESSED_DIR, output_filename)

    import pandas as pd
    pd.DataFrame(flat).to_csv(output_path, index=False)
    logger.info(f"Saved {len(flat)} records to {output_path}")


if __name__ == "__main__":
    pass
