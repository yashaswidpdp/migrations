import pandas as pd
import logging
import os
import ast
from dotenv import load_dotenv

load_dotenv("config/.env")
DATA_RAW_DIR = os.getenv("DATA_RAW_DIR", "data/raw")
DATA_PROCESSED_DIR = os.getenv("DATA_PROCESSED_DIR", "data/processed")

logger = logging.getLogger("transform_request")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

# Flask enum values (dpdp_python/models/request.py)
# Request.status:     Completed | Initiated | Assign to PA Manager | Assign to DPO
# Request.rag_status: Red | Amber | Green | Completed
# Request.risk:       High | Medium | Low


def parse_tuple_string(tuple_str) -> list:
    if pd.isna(tuple_str) or not tuple_str:
        return []
    if isinstance(tuple_str, list):
        return tuple_str
    try:
        return ast.literal_eval(str(tuple_str))
    except Exception:
        return []


def parse_dict_list_string(list_str) -> list:
    if pd.isna(list_str) or not list_str:
        return []
    if isinstance(list_str, list):
        return list_str
    try:
        return ast.literal_eval(str(list_str))
    except Exception:
        return []


def map_request_status(odoo_status: str) -> str:
    status_map = {
        "Not Assigned": "Initiated",
        "Assign to PA Manager": "Assign to PA Manager",
        "Assigned To DPO": "Assign to DPO",
        "Completed": "Completed",
    }
    return status_map.get(str(odoo_status).strip(), "Initiated")


def map_rag_status(odoo_rag: str) -> str:
    rag = str(odoo_rag).strip().title()
    return rag if rag in {"Red", "Amber", "Green", "Completed"} else "Green"


def to_iso_datetime(raw) -> str:
    """Normalise an Odoo timestamp to ISO 8601 ('YYYY-MM-DDTHH:MM:SS') so the
    Flask backend can parse it with datetime.fromisoformat. Returns "" when
    empty/unparseable so the backend falls back to its own default (now)."""
    if raw is None or pd.isna(raw):
        return ""
    s = str(raw).strip()
    if not s:
        return ""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return pd.to_datetime(s, format=fmt).strftime("%Y-%m-%dT%H:%M:%S")
        except (ValueError, TypeError):
            continue
    try:
        return pd.to_datetime(s).strftime("%Y-%m-%dT%H:%M:%S")
    except Exception:
        return ""


def transform_request_data(input_filename: str, output_filename: str, assigned_user_id: int = None):
    input_path = os.path.join(DATA_RAW_DIR, input_filename)
    if not os.path.exists(input_path):
        logger.error(f"Input file not found: {input_path}")
        return

    df = pd.read_csv(input_path)
    logger.info(f"Loaded {len(df)} records for transformation.")

    transformed_records = []

    for index, row in df.iterrows():
        try:
            pa_list_raw = parse_dict_list_string(row.get("processingActivity"))
            processing_activity_names = [
                item.get("name") for item in pa_list_raw
                if isinstance(item, dict) and "name" in item
            ]

            # Assignee is a Flask user_id supplied at transform time (the same
            # backend user runs the migration / owns these requests). Emitted as
            # an ID list so the loader can pass it straight to /request/create's
            # `assigned_users` field.
            assigned_users = [int(assigned_user_id)] if assigned_user_id else []

            record = {
                "odoo_source_id": row.get("id"),
                "name": str(row.get("name", "")).strip(),
                "email": str(row.get("eMail", "")).strip(),
                "phone": str(row.get("phone", "")).strip(),
                # Odoo requestNo -> Flask request_no, carried through verbatim.
                "request_no": str(row.get("requestNo", "")).strip(),
                "request_type_id": map_request_status(row.get("request_type")),
                "processing_activity_names": processing_activity_names,
                "assigned_users": assigned_users,
                "status": map_request_status(row.get("status", "Not Assigned")),
                "rag_status": map_rag_status(row.get("ragStatus", "Green")),
                "otp_required": False,
                # Odoo source dates -> Flask Request columns. Emitted as ISO so
                # the backend parses them with datetime.fromisoformat; "" lets
                # the backend keep its own default (now).
                "raised_on": to_iso_datetime(row.get("createOn")),
                "action_date": to_iso_datetime(row.get("actionDate")),
                "resolution_date": to_iso_datetime(row.get("resolutionDate")),
                "closed_on": to_iso_datetime(row.get("closedOn")),
            }

            transformed_records.append(record)

        except Exception as e:
            logger.error(f"Error transforming row {index} (ID: {row.get('id')}): {e}")

    os.makedirs(DATA_PROCESSED_DIR, exist_ok=True)
    output_path = os.path.join(DATA_PROCESSED_DIR, output_filename)
    pd.DataFrame(transformed_records).to_csv(output_path, index=False)
    logger.info(f"Transformation complete. Saved {len(transformed_records)} records to {output_path}")


if __name__ == "__main__":
    pass
