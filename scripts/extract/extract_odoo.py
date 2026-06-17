import requests
import pandas as pd
import os
import logging
from typing import List, Dict, Any
from dotenv import load_dotenv

load_dotenv("config/.env")

ODOO_BASE_URL = os.getenv("ODOO_BASE_URL", "https://tool.dpdp-portal.dpdpconsultants.com/api")
ODOO_JWT_TOKEN = os.getenv("ODOO_JWT_TOKEN")
ODOO_SESSION_ID = os.getenv("ODOO_SESSION_ID")
BATCH_SIZE = int(os.getenv("BATCH_SIZE", 50))
MAX_RECORDS = int(os.getenv("MAX_RECORDS", 0))  # 0 means no limit
DATA_RAW_DIR = os.getenv("DATA_RAW_DIR", "data/raw")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("extract_odoo")

class OdooExtractor:
    def __init__(self, base_url: str, jwt_token: str, session_id: str):
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {jwt_token}",
            "Content-Type": "application/json"
        }
        self.cookies = {
            "session_id": session_id
        }

    def fetch_records(self, endpoint: str, filters: Dict[str, Any] = None) -> List[Dict[str, Any]]:
        """
        Fetch records from Odoo API using POST requests for dashboard/list endpoints.
        Handles offset pagination using page_no and rec_limit.
        """
        all_records = []
        page = 1
        
        while True:
            logger.info(f"Fetching page {page} for {endpoint}...")
            
            params = {
                "page_no": page,
                "rec_limit": BATCH_SIZE
            }
            if filters:
                params.update(filters)

            try:
                response = requests.get(
                    f"{self.base_url}{endpoint}",
                    headers=self.headers,
                    cookies=self.cookies,
                    params=params,
                    timeout=30
                )
                
                if response.status_code != 200:
                    logger.error(f"Failed to fetch page {page}: {response.status_code} - {response.text}")
                    break
                
                data = response.json()

                # Odoo wraps auth errors in an HTTP-200 envelope, e.g.
                # {"message": "Token Expired", "status_code": 401}. Abort loudly
                # instead of treating it as an empty page.
                if isinstance(data, dict) and data.get("status_code", 200) != 200:
                    logger.error(
                        f"API error envelope on page {page}: {data.get('status_code')} - "
                        f"{data.get('message')}. Refresh ODOO_JWT_TOKEN in config/.env."
                    )
                    break

                # Check where records are nested in the response
                if isinstance(data, list):
                    records = data
                else:
                    # Look for known keys or the first list found in the dict
                    records = data.get("records", data.get("data", data.get("dpcmData", data.get("dpgrData", []))))
                    if not records:
                        # Fallback: find any key that contains a list
                        for key, value in data.items():
                            if isinstance(value, list):
                                records = value
                                break
                
                if not records:
                    logger.info("No more records found.")
                    break
                
                all_records.extend(records)
                logger.info(f"Successfully fetched {len(records)} records from page {page}.")
                
                # Check for max records limit
                if MAX_RECORDS > 0 and len(all_records) >= MAX_RECORDS:
                    logger.info(f"Reached MAX_RECORDS limit of {MAX_RECORDS}. Stopping extraction.")
                    all_records = all_records[:MAX_RECORDS]
                    break

                # Use total_page from the response pagination object when available;
                # fall back to record-count heuristic for endpoints that omit it.
                pagination = data.get("pagination", {}) if isinstance(data, dict) else {}
                total_pages = pagination.get("total_page")
                if total_pages is not None:
                    if page >= total_pages:
                        break
                elif len(records) < BATCH_SIZE:
                    break

                page += 1
                
            except Exception as e:
                logger.exception(f"Exception occurred during extraction: {e}")
                break
                
        return all_records

    def fetch_simple(self, endpoint: str) -> dict:
        """
        Fetch a single non-paginated response from Odoo and return the raw dict.

        Used for endpoints like /processing_activities and /v2/get/templates
        which return the full dataset in one shot (no page_no / rec_limit).
        The caller is responsible for extracting the relevant array from the dict.
        """
        url = f"{self.base_url}{endpoint}"
        logger.info(f"Fetching (non-paginated): {url}")
        try:
            response = requests.get(
                url,
                headers=self.headers,
                cookies=self.cookies,
                timeout=60,
            )
            if response.status_code != 200:
                logger.error(f"Failed: {response.status_code} - {response.text[:300]}")
                return {}
            data = response.json()
            # Odoo wraps auth errors in an HTTP-200 envelope, e.g.
            # {"message": "Token Expired", "status_code": 401}. Catch those so
            # we don't save a bogus "empty" dataset and silently transform 0 rows.
            if isinstance(data, dict) and data.get("status_code", 200) != 200:
                logger.error(
                    f"API error envelope: {data.get('status_code')} - "
                    f"{data.get('message')}. Refresh ODOO_JWT_TOKEN in config/.env."
                )
                return {}
            return data
        except Exception as e:
            logger.exception(f"Exception fetching {url}: {e}")
            return {}

    def fetch_by_id(self, endpoint: str, record_id, result_key: str = None) -> dict:
        """Fetch a single record from a by-id endpoint (e.g. /dpgr/id?id=66,
        /dpcm/id?id=300). Returns the inner record dict, or {} on failure.

        The response shape is {"status": "success", "<result_key>": [ {...} ]}.
        When result_key is None the first list-valued key is used.
        """
        url = f"{self.base_url}{endpoint}"
        try:
            response = requests.get(
                url,
                headers=self.headers,
                cookies=self.cookies,
                params={"id": record_id},
                timeout=30,
            )
            if response.status_code != 200:
                logger.error(f"by-id fetch failed (id={record_id}): {response.status_code} - {response.text[:200]}")
                return {}
            data = response.json()
            # Same HTTP-200 auth-error envelope guard as the other fetchers.
            if isinstance(data, dict) and data.get("status_code", 200) != 200:
                logger.error(
                    f"API error envelope on by-id (id={record_id}): {data.get('status_code')} - "
                    f"{data.get('message')}. Refresh ODOO_JWT_TOKEN in config/.env."
                )
                return {}
            records = data.get(result_key) if result_key else None
            if records is None:
                for value in data.values():
                    if isinstance(value, list):
                        records = value
                        break
            if isinstance(records, list) and records:
                return records[0]
            return {}
        except Exception as e:
            logger.exception(f"Exception on by-id fetch {url} (id={record_id}): {e}")
            return {}

    def save_to_csv(self, records: List[Dict[str, Any]], filename: str):
        """Save records to a human-readable CSV."""
        if not records:
            logger.warning("No records to save.")
            return

        os.makedirs(DATA_RAW_DIR, exist_ok=True)
        filepath = os.path.join(DATA_RAW_DIR, filename)

        df = pd.DataFrame(records)
        df.to_csv(filepath, index=False)
        logger.info(f"Saved {len(records)} records to {filepath}")

    def save_to_json(self, data: object, filename: str):
        """Save raw response (dict or list) as JSON — used for tree-structured data."""
        import json
        os.makedirs(DATA_RAW_DIR, exist_ok=True)
        filepath = os.path.join(DATA_RAW_DIR, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info(f"Saved JSON to {filepath}")


def run_extraction(entity_endpoint: str, output_file: str, filters: dict = None):
    extractor = OdooExtractor(ODOO_BASE_URL, ODOO_JWT_TOKEN, ODOO_SESSION_ID)
    records = extractor.fetch_records(entity_endpoint, filters=filters)
    extractor.save_to_csv(records, output_file)


def run_pa_extraction(output_file: str):
    """Extract all Processing Activities (tree) from Odoo and save as JSON."""
    extractor = OdooExtractor(ODOO_BASE_URL, ODOO_JWT_TOKEN, ODOO_SESSION_ID)
    raw = extractor.fetch_simple("/processing_activities")
    if raw:
        extractor.save_to_json(raw, output_file)
    else:
        logger.error("Processing Activity extraction returned empty response.")


def run_template_extraction(output_file: str, template_type: str = None):
    """Extract all Templates from Odoo and save as JSON."""
    extractor = OdooExtractor(ODOO_BASE_URL, ODOO_JWT_TOKEN, ODOO_SESSION_ID)
    endpoint = "/v2/get/templates"
    if template_type:
        endpoint += f"?template_type={template_type}"
    raw = extractor.fetch_simple(endpoint)
    if raw:
        extractor.save_to_json(raw, output_file)
    else:
        logger.error("Template extraction returned empty response.")


def run_vendor_extraction(output_file: str = "raw_vendors.json"):
    """Extract all vendors from Odoo (/vendors_details) and save as JSON."""
    extractor = OdooExtractor(ODOO_BASE_URL, ODOO_JWT_TOKEN, ODOO_SESSION_ID)
    raw = extractor.fetch_simple("/vendors_details")
    if raw:
        extractor.save_to_json(raw, output_file)
    else:
        logger.error("Vendor extraction returned empty response.")

def run_request_enrichment(raw_file: str = "raw_requests.csv"):
    """Enrich the dashboard request CSV with the per-record by-id fields the
    dashboard omits — chiefly `requestType [id, name]`, plus the assignee
    email/phone carried in `trackAssigneeStatus`. Adds columns in place.

    `/dpgr/dashboard` has no request type at all; `GET /dpgr/id?id=<N>` does.
    """
    import json
    path = os.path.join(DATA_RAW_DIR, raw_file)
    if not os.path.exists(path):
        logger.error(f"Cannot enrich, raw file not found: {path}")
        return
    df = pd.read_csv(path)
    if "id" not in df.columns:
        logger.error("Request CSV has no 'id' column; cannot enrich by id.")
        return

    # by-id-only fields the dashboard omits. Collected per-record and written
    # as new columns the transform reads.
    byid_cols = ("dpComment", "escalatedComment", "escalatedDate",
                 "closingComment", "withdrawalComment", "iPAddress",
                 "deviceType", "resolutionDate", "closedOn")

    extractor = OdooExtractor(ODOO_BASE_URL, ODOO_JWT_TOKEN, ODOO_SESSION_ID)
    request_types, assignee_emails, consents = [], [], []
    extra = {col: [] for col in byid_cols}
    for i, rec_id in enumerate(df["id"].tolist(), 1):
        rec = extractor.fetch_by_id("/dpgr/id", rec_id, result_key="dpgr")
        request_types.append(json.dumps(rec.get("requestType")) if rec.get("requestType") else "")
        track = rec.get("trackAssigneeStatus") or []
        email = track[0].get("email") if track and isinstance(track[0], dict) else ""
        assignee_emails.append(email or "")
        # Linked consent(s) for revoke requests -> the transform extracts the ids.
        consents.append(json.dumps(rec.get("consent")) if rec.get("consent") else "")
        for col in byid_cols:
            extra[col].append(rec.get(col) or "")
        if i % 25 == 0:
            logger.info(f"Enriched {i}/{len(df)} requests...")

    df["requestType"] = request_types
    df["assignee_email"] = assignee_emails
    df["consent"] = consents
    for col in byid_cols:
        df[col] = extra[col]
    df.to_csv(path, index=False)
    filled = sum(1 for x in request_types if x)
    logger.info(f"Request enrichment done: {filled}/{len(df)} rows now carry requestType. Updated {path}")


def run_consent_enrichment(raw_file: str = "raw_consents.csv"):
    """Backfill consent type fields from `GET /dpcm/id?id=<N>` where the
    dashboard left them blank (notably `userActivityType`). The by-id endpoint
    uses different key names (`digitalPaper`/`legacyLive`) — mapped back onto the
    dashboard column names the transform already reads."""
    import json
    path = os.path.join(DATA_RAW_DIR, raw_file)
    if not os.path.exists(path):
        logger.error(f"Cannot enrich, raw file not found: {path}")
        return
    df = pd.read_csv(path)
    if "id" not in df.columns:
        logger.error("Consent CSV has no 'id' column; cannot enrich by id.")
        return

    for col in ("userActivityType", "paperType", "legacyType", "template",
                "artifactId", "iPAddress", "deviceType", "dpgrRequestNo",
                "closedOn", "createdOn", "lastUpdatedOn"):
        if col not in df.columns:
            df[col] = ""

    extractor = OdooExtractor(ODOO_BASE_URL, ODOO_JWT_TOKEN, ODOO_SESSION_ID)
    filled = 0
    for i, rec_id in enumerate(df["id"].tolist(), 1):
        rec = extractor.fetch_by_id("/dpcm/id", rec_id, result_key="dpcm")
        if not rec:
            continue
        idx = df.index[df["id"] == rec_id]

        def _blank(v):
            return v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip() == ""

        # by-id `userActivityType` fills the dashboard blank; `digitalPaper`/
        # `legacyLive` are the by-id names for `paperType`/`legacyType`.
        for csv_col, byid_key in (("userActivityType", "userActivityType"),
                                  ("paperType", "digitalPaper"),
                                  ("legacyType", "legacyLive")):
            byid_val = rec.get(byid_key)
            if byid_val and df.loc[idx, csv_col].apply(_blank).all():
                df.loc[idx, csv_col] = byid_val
                if csv_col == "userActivityType":
                    filled += 1
        if rec.get("template"):
            df.loc[idx, "template"] = json.dumps(rec.get("template"))

        # Consent-proof + audit fields only the by-id endpoint returns.
        # Overwrite-blank only, so a populated dashboard value wins. Coerce
        # list/dict values to a JSON string (some fields, e.g. dpgrRequestNo,
        # can arrive as an array) — assigning a list to df.loc[idx, col] raises
        # "Must have equal len keys and value when setting with an iterable".
        for csv_col in ("artifactId", "iPAddress", "deviceType",
                        "dpgrRequestNo", "closedOn", "createdOn",
                        "lastUpdatedOn"):
            byid_val = rec.get(csv_col)
            if isinstance(byid_val, (list, dict)):
                byid_val = json.dumps(byid_val) if byid_val else ""
            if byid_val and df.loc[idx, csv_col].apply(_blank).all():
                df.loc[idx, csv_col] = byid_val
        if i % 25 == 0:
            logger.info(f"Enriched {i}/{len(df)} consents...")

    df.to_csv(path, index=False)
    logger.info(f"Consent enrichment done: backfilled userActivityType on {filled} rows. Updated {path}")


if __name__ == "__main__":
    pass
