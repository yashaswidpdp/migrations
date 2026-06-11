import pandas as pd
import logging
import os
import ast
from dotenv import load_dotenv

load_dotenv("config/.env")
DATA_RAW_DIR = os.getenv("DATA_RAW_DIR", "data/raw")
DATA_PROCESSED_DIR = os.getenv("DATA_PROCESSED_DIR", "data/processed")

logger = logging.getLogger("transform_consent")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

# Flask enum values (dpdp_python/models/consent.py)
# ConsentStatus:      Initiated | Deemed Consent | Consented | Rejected | Not Delivered | Withdrawn | Expired | Bounced | Delivered
# LegacyTypeEnum:     Legacy | Live
# ConsentTypeEnum:    Digital | Paper
# ProcessingTypeEnum: Mandatory/Regulatory | Promotional


def map_status(status_str: str) -> str:
    s = str(status_str).lower().strip()
    if "deemed" in s:
        return "Deemed Consent"
    if "consented" in s:
        return "Consented"
    if "reject" in s:
        return "Rejected"
    if "withdraw" in s:
        return "Withdrawn"
    if "expired" in s:
        return "Expired"
    if "bounced" in s:
        return "Bounced"
    if "delivered" in s:
        return "Delivered"
    if "initiated" in s:
        return "Initiated"
    return "Deemed Consent"


def map_processing_type(user_activity_type: str) -> str:
    if "mandatory" in str(user_activity_type).lower():
        return "Mandatory/Regulatory"
    return "Promotional"


def map_consent_type(paper_type: str) -> str:
    if "paper" in str(paper_type).lower():
        return "Paper"
    return "Digital"


def map_legacy_type(legacy_type: str) -> str:
    if "live" in str(legacy_type).lower():
        return "Live"
    return "Legacy"


def parse_tuple_string(tuple_str) -> list:
    if pd.isna(tuple_str):
        return [None, ""]
    if isinstance(tuple_str, list):
        return tuple_str
    try:
        return ast.literal_eval(str(tuple_str))
    except Exception:
        return [None, tuple_str]


def transform_consent_data(input_filename: str, output_filename: str):
    input_path = os.path.join(DATA_RAW_DIR, input_filename)
    if not os.path.exists(input_path):
        logger.error(f"Input file not found: {input_path}")
        return

    df = pd.read_csv(input_path)
    logger.info(f"Loaded {len(df)} records for transformation.")

    transformed_records = []

    for index, row in df.iterrows():
        try:
            name_field = parse_tuple_string(row.get("name"))
            name = name_field[1] if len(name_field) > 1 else str(name_field[0] or "")

            processing_field = parse_tuple_string(row.get("processingActivity"))
            processing_activity_id = processing_field[0] if len(processing_field) > 0 else None
            processing_activity_name = processing_field[1] if len(processing_field) > 1 else None

            manager_field = parse_tuple_string(row.get("pAManager"))
            manager_name = manager_field[1] if len(manager_field) > 1 else None

            record = {
                "odoo_source_id": row.get("id"),
                "name": name,
                "email": row.get("eMail", ""),
                "phone": str(row.get("phone", "")),
                "processing_activity_id": processing_activity_id,
                "processing_activity_name": processing_activity_name,
                "manager_name": manager_name,
                "status": map_status(row.get("status")),
                "legacyType": map_legacy_type(row.get("legacyType", "legacy")),
                "consentType": map_consent_type(row.get("paperType", "digital")),
                "processingType": map_processing_type(row.get("userActivityType", "mandatory")),
                "accept_terms": True,
            }

            transformed_records.append(record)

        except Exception as e:
            logger.error(f"Error transforming row {index} (ID: {row.get('id')}): {e}")

    deemed_records = [r for r in transformed_records if r["status"] == "Deemed Consent"]
    live_records = [r for r in transformed_records if r["status"] != "Deemed Consent"]

    os.makedirs(DATA_PROCESSED_DIR, exist_ok=True)

    pd.DataFrame(transformed_records).to_csv(
        os.path.join(DATA_PROCESSED_DIR, output_filename), index=False
    )

    base = output_filename.replace(".csv", "")

    if deemed_records:
        pd.DataFrame(deemed_records).to_csv(
            os.path.join(DATA_PROCESSED_DIR, f"{base}_deemed.csv"), index=False
        )

    if live_records:
        pd.DataFrame(live_records).to_csv(
            os.path.join(DATA_PROCESSED_DIR, f"{base}_live.csv"), index=False
        )

    logger.info(
        f"Transformation complete. Total: {len(transformed_records)} | "
        f"Deemed: {len(deemed_records)} | Live: {len(live_records)}"
    )


if __name__ == "__main__":
    pass
