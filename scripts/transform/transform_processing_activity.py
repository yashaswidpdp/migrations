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
            "name": name,
            "parent_name": parent_name,
            "description": str(node.get("description", "") or "").strip() or None,
            "activity_type": activity_type,
            "is_active": bool(node.get("isActive", True)),
            "is_otp": bool(node.get("isOtpMandatory", False)),
            "show_on_dpgr": bool(node.get("showOnDpgr", False)),
            "show_on_privacy": bool(node.get("showOnDpia", False)),
            "manager_name": manager_name,
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
