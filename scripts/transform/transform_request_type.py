"""
Transform Odoo request types into Flask /request-types/create payloads.

Odoo (source) and Flask (dest) name the same fields differently — e.g. Odoo
`sla_days` is Flask `sla_expected_days`. This renames each field and drops the
Odoo-only ones (id, request_type, is_nominee, company_id, default_department_ids,
is_active).

Two backend constraints are enforced here, not at load time:
  * Flask allows only ONE request type with is_revoke=True per tenant. The first
    revoke type wins; every later one is forced to is_revoke=False (logged).
  * Odoo department ids are not Flask department ids and the source carries no
    names to cross-map, so `department` is always emitted empty. (Confirmed: the
    sampled source rows all had default_department_ids = [].)

Output is a JSON list of create-ready payloads, consumed by
load_flask.load_request_types().
"""
import json
import logging
import os
from dotenv import load_dotenv

load_dotenv("config/.env")
DATA_RAW_DIR = os.getenv("DATA_RAW_DIR", "data/raw")
DATA_PROCESSED_DIR = os.getenv("DATA_PROCESSED_DIR", "data/processed")

logger = logging.getLogger("transform_request_type")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")


# Odoo source field -> Flask create field. Booleans/ints copied as-is.
FIELD_MAP = {
    "name": "name",
    "description": "description",
    "sla_days": "sla_expected_days",
    "sla_amber_days": "sla_amber_notification_days",
    "sla_red_days": "sla_red_notification_days",
    "amber_days": "amber_alert_days",
    "red_days": "red_alert_days",
    "vendor_is_mandatory": "vendor_mandatory",
    "check_consent": "consent_withdrawal_check",
    "is_data_principal": "is_data_principal",
    "nominee_access": "nominee_access",
    "is_revoke": "is_revoke",
}


def _as_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    return str(v).strip().lower() in ("true", "1", "yes")


# SLA fields are resolved as one block (see _resolve_sla_block), not field by
# field, because the backend validates them against each other.
SLA_NUMERIC_FIELDS = {
    "sla_expected_days", "sla_amber_notification_days",
    "sla_red_notification_days", "amber_alert_days", "red_alert_days",
}

# Backend request_type_routes.validate_sla_thresholds coerces every missing SLA
# field to 0 and rejects 0 ("must be greater than zero"), then enforces ordering.
# The model is days-ELAPSED (days_open): each threshold is the day the stage
# starts, so amber comes before red, both within the SLA window:
#   amber_alert_days   <  red_alert_days  <=  sla_expected_days
#   sla_amber_notification_days  <=  amber_alert_days
#   sla_red_notification_days    <=  red_alert_days
# Odoo often ships 0/partial SLA, so when the source set is incomplete or breaks
# these rules we replace ALL five with this valid default block.
DEFAULT_SLA_BLOCK = {
    "sla_expected_days": 45,
    "amber_alert_days": 15,
    "red_alert_days": 30,
    "sla_amber_notification_days": 7,
    "sla_red_notification_days": 15,
}


def _as_positive_int(v):
    """Return int(v) when it parses to a positive number, else None."""
    try:
        iv = int(float(v))
    except (TypeError, ValueError):
        return None
    return iv if iv > 0 else None


def _sla_block_valid(b: dict) -> bool:
    """Mirror backend validate_sla_thresholds (days-elapsed model): all five
    present, positive, and amber_alert < red_alert <= sla_expected, with each
    notification day within its alert."""
    e = b["sla_expected_days"]
    a = b["amber_alert_days"]
    r = b["red_alert_days"]
    an = b["sla_amber_notification_days"]
    rn = b["sla_red_notification_days"]
    if None in (e, a, r, an, rn):
        return False
    return e >= r > a and a >= an and r >= rn


def _resolve_sla_block(rec: dict, name: str) -> dict:
    """Build the Flask SLA block from Odoo source values; fall back to the full
    default block when the source set is missing/invalid (so create never 400s)."""
    block = {
        "sla_expected_days": _as_positive_int(rec.get("sla_days")),
        "amber_alert_days": _as_positive_int(rec.get("amber_days")),
        "red_alert_days": _as_positive_int(rec.get("red_days")),
        "sla_amber_notification_days": _as_positive_int(rec.get("sla_amber_days")),
        "sla_red_notification_days": _as_positive_int(rec.get("sla_red_days")),
    }
    if _sla_block_valid(block):
        return block
    logger.warning(
        "Request type '%s' has missing/invalid SLA values %s; applying default "
        "SLA block %s.", name, block, DEFAULT_SLA_BLOCK
    )
    return dict(DEFAULT_SLA_BLOCK)


def transform_request_type_data(raw_file: str = "raw_request_types.json",
                                output_file: str = "processed_request_types.json"):
    in_path = os.path.join(DATA_RAW_DIR, raw_file)
    out_path = os.path.join(DATA_PROCESSED_DIR, output_file)

    if not os.path.exists(in_path):
        logger.error(f"Raw request-type file not found: {in_path}")
        return

    with open(in_path, encoding="utf-8") as f:
        records = json.load(f)
    if isinstance(records, dict):
        records = records.get("requestTypes", [])

    bool_fields = {
        "vendor_mandatory", "consent_withdrawal_check",
        "is_data_principal", "nominee_access", "is_revoke",
    }
    revoke_taken = False
    payloads = []

    for rec in records:
        name = str(rec.get("name", "")).strip()
        if not name:
            logger.warning("Skipping request type with no name: %r", rec)
            continue

        payload = {}
        for src, dst in FIELD_MAP.items():
            if src not in rec:
                continue
            if dst in SLA_NUMERIC_FIELDS:
                continue  # handled as one validated block below
            val = rec[src]
            payload[dst] = _as_bool(val) if dst in bool_fields else val

        # SLA fields validated as a set; source values used when valid, else the
        # full default block (backend rejects 0 and enforces ordering).
        payload.update(_resolve_sla_block(rec, name))

        # Enforce single-revoke-per-tenant: first revoke wins, rest demoted.
        if payload.get("is_revoke"):
            if revoke_taken:
                logger.warning(
                    "Request type '%s' had is_revoke=True but a revoke type already "
                    "exists; forcing is_revoke=False (backend allows only one).", name
                )
                payload["is_revoke"] = False
            else:
                revoke_taken = True

        # Odoo dept ids != Flask dept ids and source carries no names -> drop.
        payload["department"] = []
        payloads.append(payload)

    os.makedirs(DATA_PROCESSED_DIR, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payloads, f, ensure_ascii=False, indent=2)
    logger.info(f"Transformed {len(payloads)} request types -> {out_path}")
