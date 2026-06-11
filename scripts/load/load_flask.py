import requests
import pandas as pd
import os
import sys
import logging
import ast
from io import BytesIO
from dotenv import load_dotenv
import openpyxl
from typing import Optional

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../dpdp_python")))

load_dotenv("config/.env")

FLASK_API_BASE_URL = os.getenv("FLASK_API_BASE_URL")
FLASK_API_KEY = os.getenv("FLASK_API_KEY")
DATA_PROCESSED_DIR = os.getenv("DATA_PROCESSED_DIR", "data/processed")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("load_flask")


class FlaskLoader:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Host": os.getenv("FLASK_TENANT_DOMAIN")
    }

    def load_from_csv(self, csv_filename: str, endpoint: str):
        input_path = os.path.join(DATA_PROCESSED_DIR, csv_filename)
        if not os.path.exists(input_path):
            logger.error(f"Input CSV not found: {input_path}")
            return

        df = pd.read_csv(input_path)
        logger.info(f"Loaded {len(df)} records from {csv_filename}")

        success_count = 0
        failure_rows = []
        error_file = os.path.join(DATA_PROCESSED_DIR, f"errors_{csv_filename}")

        pa_map = self._resolve_pa_ids()
        request_type_id = self._resolve_request_type_id()

        for index, row in df.iterrows():
            record_data = {k: (None if isinstance(v, float) and pd.isna(v) else v) for k, v in row.to_dict().items()}

            pa_name = record_data.get("processing_activity_name")
            if pa_name:
                record_data["processing_activity_id"] = pa_map.get(str(pa_name).strip())

            pa_names_str = record_data.get("processing_activity_names")
            if pa_names_str:
                try:
                    pa_names = ast.literal_eval(str(pa_names_str))
                    record_data["processing_activity"] = [pa_map[n] for n in pa_names if n in pa_map]
                except Exception:
                    pass

            if request_type_id:
                record_data["request_type_id"] = request_type_id

            if not record_data.get("phone"):
                record_data["phone"] = "0000000000"

            if not record_data.get("email"):
                phone_val = str(record_data.get("phone", "")).strip()
                record_data["email"] = f"{phone_val}@migration.local"

            record_data.pop("processing_activity_name", None)
            record_data.pop("processing_activity_names", None)
            record_data.pop("assigned_user_names", None)
            record_data.pop("manager_name", None)

            try:
                response = requests.post(
                    f"{self.base_url}{endpoint}",
                    headers=self.headers,
                    json=record_data,
                    timeout=30,
                )

                if response.status_code in [200, 201]:
                    logger.info(f"Loaded record {index + 1}/{len(df)}")
                    success_count += 1
                elif response.status_code == 409:
                    logger.info(f"Record {index + 1} already exists. Skipping.")
                    success_count += 1
                else:
                    logger.error(f"Failed record {index + 1}: {response.status_code} - {response.text}")
                    failure_rows.append({**record_data, "error_message": response.text, "status_code": response.status_code})

            except Exception as e:
                logger.error(f"Exception loading record {index + 1}: {e}")
                failure_rows.append({**record_data, "error_message": str(e)})

        if failure_rows:
            pd.DataFrame(failure_rows).to_csv(error_file, index=False)
            logger.error(f"{len(failure_rows)} failures written to {error_file}")

        logger.info(f"Summary: {success_count} succeeded, {len(failure_rows)} failed.")

    def _resolve_request_type_id(self) -> int | None:
        try:
            response = requests.get(
                f"{self.base_url}/request-types/",
                headers=self.headers,
                timeout=30,
            )
            if response.status_code == 200:
                body = response.json()
                data = body.get("data", {})
                records = data.get("records", []) if isinstance(data, dict) else data
                if isinstance(records, list) and records:
                    return records[0]["id"]
        except Exception as e:
            logger.warning(f"Could not fetch request types from Flask API: {e}")
        return None

    def _resolve_user_ids(self) -> dict:
        """Fetch all backend users from Flask and map their names to user IDs."""
        name_to_id = {}
        try:
            page = 1
            while True:
                response = requests.get(
                    f"{self.base_url}/auth/backend-users",
                    headers=self.headers,
                    params={"page": page, "per_page": 100},
                    timeout=30,
                )
                if response.status_code == 200:
                    body = response.json()
                    data = body.get("data", {})
                    users = data.get("users", [])
                    if not users:
                        break
                    for u in users:
                        if isinstance(u, dict) and "name" in u and "id" in u:
                            name_to_id[str(u["name"]).strip().lower()] = u["id"]
                    
                    pagination = data.get("pagination", {})
                    total_pages = pagination.get("total_page", 1)
                    if page >= total_pages:
                        break
                    page += 1
                else:
                    logger.warning(f"Could not fetch backend users: {response.status_code} - {response.text}")
                    break
        except Exception as e:
            logger.warning(f"Error fetching backend users: {e}")
        return name_to_id

    def _resolve_current_user_id(self) -> int | None:
        """Fetch the current authenticated user's profile ID."""
        try:
            response = requests.get(
                f"{self.base_url}/auth/profile",
                headers=self.headers,
                timeout=30,
            )
            if response.status_code == 200:
                body = response.json()
                data = body.get("data", {})
                if isinstance(data, dict) and "id" in data:
                    return data["id"]
        except Exception as e:
            logger.warning(f"Could not fetch current user profile: {e}")
        return None

    # ------------------------------------------------------------------ #
    #  PROCESSING ACTIVITY LOADING                                         #
    # ------------------------------------------------------------------ #

    def load_processing_activities(self, csv_filename: str):
        """
        Load Processing Activities from a flat CSV into Flask.

        CSV columns expected:
          name, parent_name (optional), description, activity_type,
          is_active, is_otp, show_on_dpgr, show_on_privacy

        Strategy:
        - Sort records so parents always come before their children.
        - After each successful create, record name→Flask_id in a local map.
        - Resolve parent_name → parent_id using that map.
        - Treat Flask 400 "already exists" as skip (idempotent).

        JWT auth is required — Flask's /processing/create is @jwt_required().
        """
        input_path = os.path.join(DATA_PROCESSED_DIR, csv_filename)
        if not os.path.exists(input_path):
            logger.error(f"CSV not found: {input_path}")
            return

        df = pd.read_csv(input_path)
        df = df.where(pd.notnull(df), None)  # NaN → None
        logger.info(f"Loaded {len(df)} PA records from {csv_filename}")

        # Topological sort: roots first (parent_name is NaN/None), then children
        roots = df[df["parent_name"].isna()].copy()
        children = df[~df["parent_name"].isna()].copy()
        ordered = pd.concat([roots, children], ignore_index=True)

        # Build existing name→id map from Flask
        name_to_id = self._resolve_pa_ids()
        user_map = self._resolve_user_ids()
        default_user_id = self._resolve_current_user_id()

        success_count = 0
        skip_count = 0
        failure_count = 0

        for _, row in ordered.iterrows():
            name = str(row.get("name", "")).strip()
            if not name:
                continue

            # Skip if already exists
            if name in name_to_id:
                logger.info(f"PA '{name}' already exists (id={name_to_id[name]}). Skipping.")
                skip_count += 1
                continue

            parent_name = row.get("parent_name")
            parent_id = name_to_id.get(str(parent_name).strip()) if parent_name else None

            manager_name = row.get("manager_name")
            manager_ids = []
            if manager_name:
                manager_name_clean = str(manager_name).strip().lower()
                if manager_name_clean in user_map:
                    manager_ids = [user_map[manager_name_clean]]
            
            if not manager_ids and default_user_id is not None:
                manager_ids = [default_user_id]

            payload = {
                "name": name,
                "activity_type": str(row.get("activity_type") or "Mandatory/Regulatory"),
                "is_active": bool(row.get("is_active", True)),
                "is_otp": bool(row.get("is_otp", False)),
                "show_on_dpgr": bool(row.get("show_on_dpgr", False)),
                "show_on_privacy": bool(row.get("show_on_privacy", False)),
                "manager_ids": manager_ids,
            }
            if parent_id is not None:
                payload["parent_id"] = parent_id
            desc = row.get("description")
            if desc:
                payload["description"] = str(desc)

            try:
                response = requests.post(
                    f"{self.base_url}/processing/create",
                    headers=self.headers,
                    json=payload,
                    timeout=30,
                )
                if response.status_code in (200, 201):
                    new_id = response.json().get("data", {}).get("id")
                    if new_id:
                        name_to_id[name] = new_id
                    logger.info(f"Created PA '{name}' (id={new_id})")
                    success_count += 1
                elif response.status_code == 400 and "already exists" in response.text.lower():
                    logger.info(f"PA '{name}' already exists. Skipping.")
                    skip_count += 1
                    # Refresh map so children can resolve this parent
                    name_to_id = self._resolve_pa_ids()
                else:
                    logger.error(f"Failed PA '{name}': {response.status_code} - {response.text[:300]}")
                    failure_count += 1
            except Exception as e:
                logger.error(f"Exception creating PA '{name}': {e}")
                failure_count += 1

        logger.info(
            f"PA load complete: {success_count} created, "
            f"{skip_count} skipped, {failure_count} failed."
        )

    # ------------------------------------------------------------------ #
    #  TEMPLATE LOADING                                                    #
    # ------------------------------------------------------------------ #

    def load_templates(self, csv_filename: str):
        """
        Load Templates from a flat CSV into Flask.

        CSV columns expected:
          name, template_type, sub_type, language, email_body,
          subject (optional), is_default, is_granular, status,
          processing_activity_names (optional), effective_from (optional)

        Idempotency: fetch existing template names first; skip any already present.
        JWT auth is required.
        """
        input_path = os.path.join(DATA_PROCESSED_DIR, csv_filename)
        if not os.path.exists(input_path):
            logger.error(f"CSV not found: {input_path}")
            return

        df = pd.read_csv(input_path)
        df = df.where(pd.notnull(df), None)
        logger.info(f"Loaded {len(df)} template records from {csv_filename}")

        # Pre-fetch existing template names to skip duplicates
        existing_names = self._fetch_existing_template_names()
        pa_map = self._resolve_pa_ids()

        success_count = 0
        skip_count = 0
        failure_count = 0

        for _, row in df.iterrows():
            name = str(row.get("name", "")).strip()
            if not name:
                continue

            if name in existing_names:
                logger.info(f"Template '{name}' already exists. Skipping.")
                skip_count += 1
                continue

            pa_names_str = row.get("processing_activity_names")
            pa_ids = []
            if pa_names_str:
                try:
                    # Could be list or string representation of list
                    if isinstance(pa_names_str, str) and pa_names_str.startswith("["):
                        pa_names = ast.literal_eval(pa_names_str)
                    elif isinstance(pa_names_str, str):
                        pa_names = [pa_names_str]
                    else:
                        pa_names = list(pa_names_str)
                    
                    pa_ids = [pa_map[n.strip()] for n in pa_names if n.strip() in pa_map]
                except Exception as e:
                    logger.warning(f"Error parsing processing_activity_names for template '{name}': {e}")

            payload = {
                "name": name,
                "template_type": str(row.get("template_type") or "Email Template"),
                "sub_type": str(row.get("sub_type") or "Email"),
                "language": str(row.get("language") or "English"),
                "email_body": str(row.get("email_body") or "(no content)"),
                "is_default": bool(row.get("is_default", False)),
                "enable_granular_consent": bool(row.get("is_granular", False)),
                "status": str(row.get("status") or "Active"),
                "approval": False,
            }
            subject = row.get("subject")
            if subject:
                payload["subject"] = str(subject)
            if pa_ids:
                payload["processing_activity_ids"] = pa_ids
            eff = row.get("effective_from")
            if eff:
                payload["effective_from"] = str(eff)

            try:
                response = requests.post(
                    f"{self.base_url}/notice-templates/create",
                    headers=self.headers,
                    json=payload,
                    timeout=30,
                )
                if response.status_code in (200, 201):
                    logger.info(f"Created template '{name}'")
                    success_count += 1
                    existing_names.add(name)
                else:
                    logger.error(f"Failed template '{name}': {response.status_code} - {response.text[:300]}")
                    failure_count += 1
            except Exception as e:
                logger.error(f"Exception creating template '{name}': {e}")
                failure_count += 1

        logger.info(
            f"Template load complete: {success_count} created, "
            f"{skip_count} skipped, {failure_count} failed."
        )

    def _fetch_existing_template_names(self) -> set:
        """Fetch all template names already in Flask for the active tenant."""
        try:
            response = requests.get(
                f"{self.base_url}/notice-templates/",
                headers=self.headers,
                timeout=30,
            )
            if response.status_code == 200:
                body = response.json()
                data = body.get("data", {})
                records = data.get("templates", data.get("records", []))
                if isinstance(records, list):
                    return {r["name"] for r in records if isinstance(r, dict) and "name" in r}
        except Exception as e:
            logger.warning(f"Could not fetch existing templates: {e}")
        return set()

    def _resolve_pa_ids(self) -> dict:
        try:
            response = requests.get(
                f"{self.base_url}/processing/activities/simple",
                headers=self.headers,
                timeout=30,
            )
            if response.status_code == 200:
                body = response.json()
                data = body.get("data", {})
                records = []
                if isinstance(data, dict):
                    records = data.get("records", [])
                elif isinstance(data, list):
                    records = data
                if isinstance(records, list):
                    return {
                        pa["name"]: pa["id"]
                        for pa in records
                        if isinstance(pa, dict) and "name" in pa and "id" in pa
                    }
        except Exception as e:
            logger.warning(f"Could not fetch PA list from Flask API: {e}")
        return {}

    def load_deemed_via_import(self, csv_filename: str):
        input_path = os.path.join(DATA_PROCESSED_DIR, csv_filename)
        if not os.path.exists(input_path):
            logger.error(f"Deemed consent CSV not found: {input_path}")
            return

        df = pd.read_csv(input_path)
        if df.empty:
            logger.info("No deemed consent records to load.")
            return

        logger.info(f"Loaded {len(df)} deemed consent records from {csv_filename}")

        group_cols = ["processingType", "consentType", "legacyType"]
        for col in group_cols:
            if col not in df.columns:
                df[col] = "Legacy" if col == "legacyType" else (
                    "Digital" if col == "consentType" else "Mandatory/Regulatory"
                )
        df[group_cols] = df[group_cols].fillna("Unknown")

        total_success = 0
        total_failure = 0

        for group_keys, group_df in df.groupby(group_cols):
            processing_type, consent_type, legacy_type = group_keys
            batch_label = (
                f"{legacy_type}_{consent_type}_{processing_type}"
                .replace("/", "-").replace(" ", "_")
            )
            logger.info(f"Sending batch '{batch_label}' ({len(group_df)} records)...")

            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "Consent Import"
            ws.append([
                "Processing Activities", "Language", "Name",
                "Email", "Mobile", "Identifier", "User Activity Type",
            ])

            for _, row in group_df.iterrows():
                ws.append([
                    str(row.get("processing_activity_name") or ""),
                    "English",
                    str(row.get("name") or ""),
                    str(row.get("email") or ""),
                    str(row.get("phone") or ""),
                    str(row.get("odoo_source_id") or ""),
                    str(processing_type or "Mandatory/Regulatory"),
                ])

            buffer = BytesIO()
            wb.save(buffer)
            buffer.seek(0)

            try:
                response = requests.post(
                    f"{self.base_url}/consent/import",
                    headers={k: v for k, v in self.headers.items() if k != "Content-Type"},
                    files={
                        "file": (
                            f"{batch_label}.xlsx",
                            buffer,
                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        )
                    },
                    data={
                        "legacyType": legacy_type,
                        "consentType": consent_type,
                        "processingType": processing_type,
                    },
                    timeout=120,
                )

                if response.status_code in [200, 201]:
                    logger.info(f"Batch '{batch_label}': {len(group_df)} records imported.")
                    total_success += len(group_df)
                else:
                    logger.error(f"Batch '{batch_label}' failed: {response.status_code} — {response.text[:400]}")
                    total_failure += len(group_df)

            except Exception as e:
                logger.exception(f"Exception during batch '{batch_label}': {e}")
                total_failure += len(group_df)

        logger.info(f"Deemed import complete: {total_success} succeeded, {total_failure} failed.")

    def load_live_via_live_consent(self, csv_filename: str):
        input_path = os.path.join(DATA_PROCESSED_DIR, csv_filename)
        if not os.path.exists(input_path):
            logger.error(f"Live consent CSV not found: {input_path}")
            return

        df = pd.read_csv(input_path)
        if df.empty:
            logger.info("No live consent records to load.")
            return

        logger.info(f"Loaded {len(df)} live consent records from {csv_filename}")

        pa_map = self._resolve_pa_ids()
        if not pa_map:
            logger.warning("PA name→ID map is empty. Records will post without processing_activity_id.")

        success_count = 0
        failure_rows = []
        error_file = os.path.join(DATA_PROCESSED_DIR, f"errors_{csv_filename}")

        for index, row in df.iterrows():
            pa_name = row.get("processing_activity_name")
            pa_id = pa_map.get(str(pa_name).strip()) if pd.notna(pa_name) and pa_name else None

            payload = {
                "name": str(row.get("name") or ""),
                "email": str(row.get("email") or "").lower().strip(),
                "phone": str(row.get("phone") or ""),
                "processing_activity_id": pa_id,
                "otp_required": False,
                "accept_terms": True,
            }

            try:
                response = requests.post(
                    f"{self.base_url}/consent/live-consent",
                    headers=self.headers,
                    json=payload,
                    timeout=30,
                )

                if response.status_code in [200, 201]:
                    logger.info(f"Live consent {index + 1}/{len(df)}: created.")
                    success_count += 1
                elif response.status_code == 400 and "already exists" in response.text.lower():
                    logger.info(f"Live consent {index + 1}/{len(df)}: already exists, skipping.")
                    success_count += 1
                else:
                    logger.error(f"Live consent {index + 1}/{len(df)} failed: {response.status_code} — {response.text[:300]}")
                    failure_rows.append({**payload, "error_status_code": response.status_code, "error_message": response.text})

            except Exception as e:
                logger.exception(f"Exception on live consent {index + 1}: {e}")
                failure_rows.append({**payload, "error_message": str(e)})

        if failure_rows:
            pd.DataFrame(failure_rows).to_csv(error_file, index=False)
            logger.error(f"{len(failure_rows)} failures written to {error_file}")

        logger.info(f"Live consent complete: {success_count} succeeded, {len(failure_rows)} failed.")


def run_loading(csv_filename: str, endpoint: str):
    FlaskLoader(FLASK_API_BASE_URL, FLASK_API_KEY).load_from_csv(csv_filename, endpoint)


def run_deemed_loading(csv_filename: str):
    FlaskLoader(FLASK_API_BASE_URL, FLASK_API_KEY).load_deemed_via_import(csv_filename)


def run_live_loading(csv_filename: str):
    FlaskLoader(FLASK_API_BASE_URL, FLASK_API_KEY).load_live_via_live_consent(csv_filename)


def run_pa_loading(csv_filename: str):
    FlaskLoader(FLASK_API_BASE_URL, FLASK_API_KEY).load_processing_activities(csv_filename)


def run_template_loading(csv_filename: str):
    FlaskLoader(FLASK_API_BASE_URL, FLASK_API_KEY).load_templates(csv_filename)


if __name__ == "__main__":
    pass
