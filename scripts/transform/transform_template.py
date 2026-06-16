"""
Transform Odoo Templates into Flask-compatible records.

Odoo source: GET /api/v2/get/templates
Flask target: POST /api/templates/create

Key mapping:
  Odoo templateType  → Flask TemplateTypeEnum
  Odoo subType       → Flask SubTypeEnum
  Odoo language.name → Flask LanguageEnum
  Odoo state=accept  → Flask status=Active
"""
import json
import logging
import os
from datetime import datetime
from dotenv import load_dotenv

load_dotenv("config/.env")
DATA_RAW_DIR = os.getenv("DATA_RAW_DIR", "data/raw")
DATA_PROCESSED_DIR = os.getenv("DATA_PROCESSED_DIR", "data/processed")

logger = logging.getLogger("transform_template")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")


def _to_iso(value) -> str:
    """Normalise an Odoo effectiveDate to ISO 8601 so the Flask
    /notice-templates approve route can parse it with datetime.fromisoformat.
    Returns "" for empty/unparseable values."""
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


FLASK_LANGUAGES = {
    "Assamese", "Bengali", "Bodo", "Dogri", "English", "Gujarati",
    "Hindi", "Kannada", "Kashmiri", "Konkani", "Maithili", "Malayalam",
    "Manipuri", "Marathi", "Nepali", "Odia", "Punjabi", "Sanskrit",
    "Santhali", "Sindhi", "Tamil", "Telugu", "Urdu",
}


def _map_template_type(odoo_type: str, odoo_sub: str) -> str:
    t_type = str(odoo_type).strip().lower()
    s_type = str(odoo_sub).strip().lower() if odoo_sub else ""

    if "breach" in t_type:
        if "sms" in s_type:
            return "Breach Notice SMS Template"
        return "Breach Notice Email Template"
    elif "privacy" in t_type:
        if "sms" in s_type:
            return "Privacy Notice SMS Template"
        return "Privacy Notice Email Template"
    elif "online" in t_type or "live" in t_type:
        return "Live Consent Template"
    elif "otp" in t_type:
        return "SMS OTP Template"
    elif "nominee" in t_type:
        return "Nominee Templates"
    else:
        if "sms" in s_type or "sms" in t_type:
            return "Consent SMS Template"
        return "Legacy Consent Email Template"


def _map_sub_type(odoo_sub: str, template_type: str) -> str:
    """Infer sub_type from odoo subType or template_type when subType is missing."""
    s_type = str(odoo_sub).strip().lower() if odoo_sub else ""
    if "sms" in s_type or "msg91" in s_type:
        return "SMS"
    if "email" in s_type:
        return "Email"
    if "online" in s_type or "live" in s_type:
        return "Online"

    # Infer from template_type
    if "SMS" in template_type:
        return "SMS"
    if "Live Consent" in template_type:
        return "Online"
    return "Email"


def _map_language(language_obj) -> str:
    if isinstance(language_obj, dict):
        name = str(language_obj.get("name", "English")).strip()
        if name in FLASK_LANGUAGES:
            return name
        logger.warning(f"Unknown language '{name}', defaulting to 'English'")
    return "English"


def transform_template_data(input_filename: str, output_filename: str):
    """
    Read raw JSON from data/raw/<input_filename>, transform each template,
    write CSV to data/processed/<output_filename>.

    The raw file must be the JSON response body or the templates array.
    """
    input_path = os.path.join(DATA_RAW_DIR, input_filename)
    if not os.path.exists(input_path):
        logger.error(f"Input file not found: {input_path}")
        return

    with open(input_path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    # Accept full response or just the templates array
    if isinstance(raw, dict):
        templates = (
            raw.get("data", {}).get("templates", [])
            if isinstance(raw.get("data"), dict)
            else raw.get("templates", [])
        )
    else:
        templates = raw

    logger.info(f"Loaded {len(templates)} templates from {input_filename}")

    records = []
    for tmpl in templates:
        if not isinstance(tmpl, dict):
            continue

        odoo_type = tmpl.get("templateType", "")
        odoo_sub = tmpl.get("subType", "")
        flask_type = _map_template_type(odoo_type, odoo_sub)
        flask_sub = _map_sub_type(odoo_sub, flask_type)
        flask_lang = _map_language(tmpl.get("language"))

        body = str(tmpl.get("templateBody", "") or "").strip()
        if not body:
            body = str(tmpl.get("templateSmsBody", "") or "").strip()
        if not body:
            body = "(no content)"

        # Extract linked PA names for association
        pa_names = []
        pas = tmpl.get("processingActivities", [])
        if isinstance(pas, list):
            for pa in pas:
                if isinstance(pa, dict) and pa.get("name"):
                    pa_names.append(str(pa.get("name")).strip())

        record = {
            "odoo_id": tmpl.get("id"),
            "name": str(tmpl.get("name", "")).strip(),
            "template_type": flask_type,
            "sub_type": flask_sub,
            "language": flask_lang,
            "email_body": body,
            "subject": str(tmpl.get("name", "") or "").strip() or None,
            "is_default": bool(tmpl.get("isDefault", False)),
            "is_granular": bool(tmpl.get("is_granular_consent", False)),
            "status": "Active",
            "processing_activity_names": pa_names,
            "effective_from": _to_iso(tmpl.get("effectiveDate")) or None,
        }
        records.append(record)

    os.makedirs(DATA_PROCESSED_DIR, exist_ok=True)
    output_path = os.path.join(DATA_PROCESSED_DIR, output_filename)

    import pandas as pd
    pd.DataFrame(records).to_csv(output_path, index=False)
    logger.info(f"Saved {len(records)} transformed templates to {output_path}")


if __name__ == "__main__":
    pass
