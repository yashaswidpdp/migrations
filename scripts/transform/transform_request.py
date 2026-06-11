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


def transform_request_data(input_filename: str, output_filename: str, default_request_type_id: int = 1):
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

            pam_field = parse_tuple_string(row.get("pAManager"))
            assigned_user_names = [pam_field[1]] if len(pam_field) > 1 and pam_field[1] else []

            record = {
                "odoo_source_id": row.get("id"),
                "name": str(row.get("name", "")).strip(),
                "email": str(row.get("eMail", "")).strip(),
                "phone": str(row.get("phone", "")).strip(),
                "request_type_id": default_request_type_id,
                "processing_activity_names": processing_activity_names,
                "assigned_user_names": assigned_user_names,
                "status": map_request_status(row.get("status", "Not Assigned")),
                "rag_status": map_rag_status(row.get("ragStatus", "Green")),
                "otp_required": False,
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
