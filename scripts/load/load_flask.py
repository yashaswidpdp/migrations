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
        }
        tenant_domain = os.getenv("FLASK_TENANT_DOMAIN")
        if tenant_domain:
            self.headers["Host"] = tenant_domain.strip()

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

            # assigned_users comes from the transform as a string repr of an ID
            # list (e.g. "[5]"); parse it back to a real list so the backend's
            # /request/create receives valid IDs.
            au_str = record_data.get("assigned_users")
            if au_str and isinstance(au_str, str):
                try:
                    record_data["assigned_users"] = ast.literal_eval(au_str)
                except Exception:
                    record_data.pop("assigned_users", None)

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
                    total_pages = pagination.get("totalPages", pagination.get("total_page", 1))
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
        # Odoo PA template links carry a NAME; resolve against Flask's template
        # name→id map (templates are always loaded before PAs).
        template_map = self._fetch_template_id_map()

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

            # Odoo effective-from dates -> Flask PA effective_from_* columns
            # (the /processing/create route reads these keys directly).
            for col in ("effective_from_email", "effective_from_sms", "effective_from_privacy"):
                val = row.get(col)
                # pandas .where(...,None) reverts None->NaN in float cols, so an
                # empty cell can arrive as float nan -> str(nan)=='nan'. Guard it.
                if val is not None and not pd.isna(val) and str(val).strip().lower() != "nan":
                    payload[col] = str(val).strip()

            # Odoo template links (name) -> Flask template_id. Resolve by name;
            # warn (don't fail the PA) if a referenced template wasn't migrated.
            for name_col, id_key in (
                ("consent_email_template_name", "consent_email_template_id"),
                ("consent_sms_template_name", "consent_sms_template_id"),
                ("privacy_template_name", "privacy_template_id"),
            ):
                tname = row.get(name_col)
                if tname is None or pd.isna(tname) or not str(tname).strip():
                    continue
                tname = str(tname).strip()
                tid = template_map.get(tname)
                if tid is not None:
                    payload[id_key] = tid
                else:
                    logger.warning(
                        f"PA '{name}': template '{tname}' not found in Flask "
                        f"(skipping {id_key}). Load templates first."
                    )

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

    def patch_processing_activity_links(self, csv_filename: str):
        """Backfill template links + effective-from dates onto PAs that already
        exist in Flask (the create pass skips existing PAs, so their template
        columns stay NULL).

        For each PA row carrying a template name or effective-from date, resolve
        the template name -> Flask id and PUT /processing/<id> with only those
        keys. Idempotent: re-running just re-sets the same values.

        Note: PUT's parse_date is strict %Y-%m-%d, so dates are sent date-only.
        """
        input_path = os.path.join(DATA_PROCESSED_DIR, csv_filename)
        if not os.path.exists(input_path):
            logger.error(f"CSV not found: {input_path}")
            return

        df = pd.read_csv(input_path)
        df = df.where(pd.notnull(df), None)  # NaN → None
        logger.info(f"Loaded {len(df)} PA records from {csv_filename} for link patch")

        name_to_id = self._resolve_pa_ids()
        template_map = self._fetch_template_id_map()

        patched = 0
        skipped = 0
        failed = 0

        for _, row in df.iterrows():
            name = str(row.get("name", "")).strip()
            if not name:
                continue

            pa_id = name_to_id.get(name)
            if pa_id is None:
                logger.warning(f"PA '{name}' not found in Flask; cannot patch. Load PAs first.")
                skipped += 1
                continue

            payload = {}

            # Template links: name -> Flask id.
            for name_col, id_key in (
                ("consent_email_template_name", "consent_email_template_id"),
                ("consent_sms_template_name", "consent_sms_template_id"),
                ("privacy_template_name", "privacy_template_id"),
            ):
                tname = row.get(name_col)
                if tname is None or pd.isna(tname) or not str(tname).strip():
                    continue
                tname = str(tname).strip()
                tid = template_map.get(tname)
                if tid is not None:
                    payload[id_key] = tid
                else:
                    logger.warning(
                        f"PA '{name}': template '{tname}' not found in Flask "
                        f"(skipping {id_key})."
                    )

            # Effective-from dates: PUT wants %Y-%m-%d, transform emits ISO.
            for col in ("effective_from_email", "effective_from_sms", "effective_from_privacy"):
                val = row.get(col)
                if val is not None and not pd.isna(val) and str(val).strip().lower() != "nan":
                    payload[col] = str(val).strip()[:10]

            # Validity fields: create ignores these (applies company defaults),
            # so they MUST be set here. Omit when Odoo had no value.
            for col in ("consent_validity_months", "otp_validity_minutes"):
                val = row.get(col)
                if val is not None and not pd.isna(val):
                    payload[col] = int(val)

            # Visibility flags: backfill in case an earlier load mis-parsed
            # Odoo 'yes'/'no' (bool('no') was True). PUT accepts these keys.
            for col in ("is_active", "show_on_dpgr", "show_on_privacy"):
                val = row.get(col)
                if val is not None and not pd.isna(val):
                    payload[col] = bool(val)

            if not payload:
                skipped += 1
                continue

            try:
                response = requests.put(
                    f"{self.base_url}/processing/{pa_id}",
                    headers=self.headers,
                    json=payload,
                    timeout=30,
                )
                if response.status_code in (200, 201):
                    logger.info(f"Patched PA '{name}' (id={pa_id}): {list(payload.keys())}")
                    patched += 1
                else:
                    logger.error(f"Failed patch PA '{name}': {response.status_code} - {response.text[:300]}")
                    failed += 1
            except Exception as e:
                logger.error(f"Exception patching PA '{name}': {e}")
                failed += 1

        logger.info(
            f"PA link patch complete: {patched} patched, "
            f"{skipped} skipped, {failed} failed."
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

        # name -> Flask id captured from each create response, so a follow-up
        # approval pass in the same run can skip the GET lookup.
        created_ids = {}

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

            # Templates are created as Draft (approval=False). Activation is done
            # separately by hitting the approval API after load (TODO: pending
            # confirmation). Backend ignores any `status` field on create.
            payload = {
                "name": name,
                "template_type": str(row.get("template_type") or "Legacy Consent Email Template"),
                "sub_type": str(row.get("sub_type") or "Email"),
                "language": str(row.get("language") or "English"),
                "email_body": str(row.get("email_body") or "(no content)"),
                "is_default": bool(row.get("is_default", False)),
                "enable_granular_consent": bool(row.get("is_granular", False)),
                "approval": False,
            }
            subject = row.get("subject")
            if subject:
                payload["subject"] = str(subject)
            if pa_ids:
                payload["processing_activity_ids"] = pa_ids
            eff = row.get("effective_from")
            if eff is not None and not pd.isna(eff) and str(eff).strip().lower() != "nan":
                payload["effective_from"] = str(eff)

            try:
                response = requests.post(
                    f"{self.base_url}/notice-templates/create",
                    headers=self.headers,
                    json=payload,
                    timeout=30,
                )
                if response.status_code in (200, 201):
                    new_id = (response.json().get("data") or {}).get("id")
                    if new_id:
                        created_ids[name] = new_id
                    logger.info(f"Created template '{name}' (id={new_id})")
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
        return created_ids

    def approve_templates(self, csv_filename: str, id_map: dict = None):
        """Activate templates that were loaded as Draft.

        Templates are created via /create with approval=False (=> Draft). The
        create endpoint ignores `status`. Activation goes through
        PUT /notice-templates/<id>, which only flips to Active when BOTH
        approval=True AND status="Active" are sent. effective_from is persisted
        only when approval is True.

        Resolves name->id fresh via GET (the create-time IDs don't survive
        across runs), then PUTs each template whose processed status is Active.
        """
        input_path = os.path.join(DATA_PROCESSED_DIR, csv_filename)
        if not os.path.exists(input_path):
            logger.error(f"CSV not found: {input_path}")
            return

        df = pd.read_csv(input_path)
        df = df.where(pd.notnull(df), None)
        logger.info(f"Loaded {len(df)} template records from {csv_filename} for approval")

        # Prefer the stash captured at load time; fall back to a GET lookup
        # (and use it to fill any names missing from the stash).
        id_map = dict(id_map) if id_map else {}
        names_needed = {str(r.get("name", "")).strip() for _, r in df.iterrows()}
        if not id_map or not names_needed.issubset(id_map.keys()):
            fetched = self._fetch_template_id_map()
            for k, v in fetched.items():
                id_map.setdefault(k, v)

        approved = 0
        skipped = 0
        failed = 0

        for _, row in df.iterrows():
            name = str(row.get("name", "")).strip()
            if not name:
                continue

            if str(row.get("status") or "Active").strip().lower() != "active":
                skipped += 1
                continue

            template_id = id_map.get(name)
            if not template_id:
                logger.warning(f"Template '{name}' not found in Flask; cannot approve.")
                failed += 1
                continue

            payload = {"approval": True, "status": "Active"}
            eff = row.get("effective_from")
            if eff is not None and not pd.isna(eff) and str(eff).strip().lower() != "nan":
                payload["effective_from"] = str(eff)

            try:
                response = requests.put(
                    f"{self.base_url}/notice-templates/{template_id}",
                    headers=self.headers,
                    json=payload,
                    timeout=30,
                )
                if response.status_code in (200, 201):
                    logger.info(f"Approved template '{name}' (id={template_id})")
                    approved += 1
                else:
                    logger.error(f"Approve failed '{name}': {response.status_code} - {response.text[:300]}")
                    failed += 1
            except Exception as e:
                logger.error(f"Exception approving template '{name}': {e}")
                failed += 1

        logger.info(
            f"Template approval complete: {approved} approved, "
            f"{skipped} skipped (non-Active), {failed} failed."
        )

    def _fetch_template_id_map(self) -> dict:
        """Fetch all templates from Flask and map name -> id (paginated)."""
        name_to_id = {}
        try:
            page = 1
            while True:
                response = requests.get(
                    f"{self.base_url}/notice-templates/",
                    headers=self.headers,
                    params={"page": page, "per_page": 100},
                    timeout=30,
                )
                if response.status_code != 200:
                    logger.warning(f"Could not fetch templates: {response.status_code} - {response.text[:200]}")
                    break
                body = response.json()
                data = body.get("data", {})
                records = data.get("records", data.get("templates", [])) if isinstance(data, dict) else []
                if not records:
                    break
                for r in records:
                    if isinstance(r, dict) and "name" in r and "id" in r:
                        name_to_id[str(r["name"]).strip()] = r["id"]
                pagination = data.get("pagination", {}) if isinstance(data, dict) else {}
                total_pages = pagination.get("totalPages", pagination.get("total_page", 1))
                if page >= (total_pages or 1):
                    break
                page += 1
        except Exception as e:
            logger.warning(f"Error fetching template id map: {e}")
        return name_to_id

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

    # Map a ProcessingTypeEnum *value* to the enum *member name* the Flask
    # importer's parse_form_data() looks up (ProcessingTypeEnum[NAME.upper()]).
    # Sending the value ("Mandatory/Regulatory") would KeyError and silently
    # default to MANDATORY, so Promotional would never propagate.
    _PROCESSING_TYPE_FORM = {
        "promotional": "PROMOTIONAL",
        "mandatory/regulatory": "MANDATORY",
    }

    def load_legacy_via_import(self, csv_filename: str):
        """Load DIGITAL (legacy) consents via /consent/import mode=LEGACY.

        Backend behaviour (process_excel_file): status is forced to
        "Deemed Consent", legacyType to "Legacy", and consentType is read from
        the form — sending "LEGACY" there is not a valid ConsentTypeEnum name so
        it defaults to DIGITAL, which is exactly what we want for these records.
        per-row processingType comes from the "User Activity Type" column.

        Anomalies (backend limitation, no date column on the legacy importer):
          * the original consent date is NOT preserved (created_at = now)
          * a consent-seeking notice email is sent per record
        """
        input_path = os.path.join(DATA_PROCESSED_DIR, csv_filename)
        if not os.path.exists(input_path):
            logger.error(f"Legacy consent CSV not found: {input_path}")
            return

        df = pd.read_csv(input_path)
        if df.empty:
            logger.info("No legacy consent records to load.")
            return

        logger.info(f"Loaded {len(df)} legacy (digital) consent records from {csv_filename}")

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Consent Import"
        ws.append([
            "Processing Activities", "Language", "Name",
            "Email", "Mobile", "Identifier", "User Activity Type",
            "Consent Date", "Sent On", "Delivered On", "Valid Till", "Reject On",
        ])

        for _, row in df.iterrows():
            ws.append([
                str(row.get("processing_activity_name") or ""),
                "English",
                str(row.get("name") or ""),
                str(row.get("email") or ""),
                str(row.get("phone") or ""),
                str(row.get("odoo_source_id") or ""),
                str(row.get("processingType") or "Mandatory/Regulatory"),
                str(row.get("consent_date") or ""),
                str(row.get("sent_on") or ""),
                str(row.get("delivered_on") or ""),
                str(row.get("valid_till") or ""),
                str(row.get("consent_reject_on") or ""),
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
                        "legacy_consents.xlsx",
                        buffer,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )
                },
                data={
                    "consentType": "LEGACY",   # import MODE (=> stored consentType Digital)
                    "legacyType": "LEGACY",
                    "channel": "EMAIL",
                },
                timeout=180,
            )

            if response.status_code in (200, 201):
                logger.info(f"Legacy import: {len(df)} records submitted. {response.text[:300]}")
            else:
                logger.error(f"Legacy import failed: {response.status_code} — {response.text[:400]}")
        except Exception as e:
            logger.exception(f"Exception during legacy import: {e}")

    def load_paper_via_import(self, csv_filename: str):
        """Load PAPER consents via /consent/import mode=PAPER.

        Backend behaviour (process_paper_excel_file) preserves the per-row
        "Consent Date" (-> created_at / consented_on) and "Consent Status", and
        sets consentType from the form (=> "Paper"). Batched per processingType
        so the form's processingType maps to a valid enum member name.

        Anomaly: legacyType is hardcoded to "Legacy" by the backend, and a
        privacy-notice email is sent per record.
        """
        input_path = os.path.join(DATA_PROCESSED_DIR, csv_filename)
        if not os.path.exists(input_path):
            logger.error(f"Paper consent CSV not found: {input_path}")
            return

        df = pd.read_csv(input_path)
        if df.empty:
            logger.info("No paper consent records to load.")
            return

        logger.info(f"Loaded {len(df)} paper consent records from {csv_filename}")

        if "processingType" not in df.columns:
            df["processingType"] = "Mandatory/Regulatory"
        df["processingType"] = df["processingType"].fillna("Mandatory/Regulatory")

        total_success = 0
        total_failure = 0

        for processing_type, group_df in df.groupby("processingType"):
            form_processing_type = self._PROCESSING_TYPE_FORM.get(
                str(processing_type).strip().lower(), "MANDATORY"
            )
            batch_label = str(processing_type).replace("/", "-").replace(" ", "_")
            logger.info(f"Sending paper batch '{batch_label}' ({len(group_df)} records)...")

            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "Consent Import"
            ws.append([
                "Processing Activities", "Language", "Name", "Email",
                "Mobile", "Identifier", "User Activity Type",
                "Consent Date", "Consent Status", "Comments",
                "File Path of Digital Consent",
                "Sent On", "Delivered On", "Valid Till", "Reject On",
            ])

            for _, row in group_df.iterrows():
                ws.append([
                    str(row.get("processing_activity_name") or ""),
                    "English",
                    str(row.get("name") or ""),
                    str(row.get("email") or ""),
                    str(row.get("phone") or ""),
                    str(row.get("odoo_source_id") or ""),
                    str(row.get("processingType") or "Mandatory/Regulatory"),
                    str(row.get("consent_date") or ""),
                    str(row.get("status") or "Deemed Consent"),
                    "",
                    "",
                    str(row.get("sent_on") or ""),
                    str(row.get("delivered_on") or ""),
                    str(row.get("valid_till") or ""),
                    str(row.get("consent_reject_on") or ""),
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
                            f"paper_{batch_label}.xlsx",
                            buffer,
                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        )
                    },
                    data={
                        "consentType": "PAPER",            # import MODE (=> stored consentType Paper)
                        "processingType": form_processing_type,
                    },
                    timeout=180,
                )

                if response.status_code in (200, 201):
                    logger.info(f"Paper batch '{batch_label}': {len(group_df)} records submitted. {response.text[:300]}")
                    total_success += len(group_df)
                else:
                    logger.error(f"Paper batch '{batch_label}' failed: {response.status_code} — {response.text[:400]}")
                    total_failure += len(group_df)
            except Exception as e:
                logger.exception(f"Exception during paper batch '{batch_label}': {e}")
                total_failure += len(group_df)

        logger.info(f"Paper import complete: {total_success} submitted, {total_failure} failed.")

    def load_consents_via_migration(self, csv_filename: str):
        """Load consents (paper + legacy together) via the migration extension
        endpoint /migration/consent, one JSON record per row.

        Unlike the Excel /consent/import paths, this preserves every Odoo source
        date (consent_date, sent_on, delivered_on, valid_till, consent_reject_on)
        and is idempotent via the migration source-map (HTTP 409 => already
        migrated, skipped). The endpoint reads consentType/status per row, so the
        combined processed_consents.csv can be loaded directly without splitting.
        """
        input_path = os.path.join(DATA_PROCESSED_DIR, csv_filename)
        if not os.path.exists(input_path):
            logger.error(f"Consent CSV not found: {input_path}")
            return

        df = pd.read_csv(input_path)
        if df.empty:
            logger.info("No consent records to load.")
            return

        logger.info(f"Loaded {len(df)} consent records from {csv_filename}")
        pa_map = self._resolve_pa_ids()

        success = 0
        failures = []
        for index, row in df.iterrows():
            record = {k: (None if isinstance(v, float) and pd.isna(v) else v) for k, v in row.to_dict().items()}

            pa_name = record.get("processing_activity_name")
            if pa_name:
                record["processing_activity_id"] = pa_map.get(str(pa_name).strip())
            record.pop("manager_name", None)

            try:
                response = requests.post(
                    f"{self.base_url}/migration/consent",
                    headers=self.headers,
                    json=record,
                    timeout=60,
                )
                if response.status_code in (200, 201):
                    logger.info(f"Loaded consent {index + 1}/{len(df)}")
                    success += 1
                elif response.status_code == 409:
                    logger.info(f"Consent {index + 1} already migrated. Skipping.")
                    success += 1
                else:
                    logger.error(f"Consent {index + 1} failed: {response.status_code} - {response.text[:300]}")
                    failures.append({**record, "error": response.text, "status_code": response.status_code})
            except Exception as e:
                logger.exception(f"Exception loading consent {index + 1}: {e}")
                failures.append({**record, "error": str(e)})

        if failures:
            err_file = os.path.join(DATA_PROCESSED_DIR, f"errors_{csv_filename}")
            pd.DataFrame(failures).to_csv(err_file, index=False)
            logger.error(f"{len(failures)} consent failures written to {err_file}")

        logger.info(f"Consent migration load complete: {success} ok, {len(failures)} failed.")


def run_loading(csv_filename: str, endpoint: str):
    FlaskLoader(FLASK_API_BASE_URL, FLASK_API_KEY).load_from_csv(csv_filename, endpoint)


def run_legacy_loading(csv_filename: str):
    FlaskLoader(FLASK_API_BASE_URL, FLASK_API_KEY).load_legacy_via_import(csv_filename)


def run_paper_loading(csv_filename: str):
    FlaskLoader(FLASK_API_BASE_URL, FLASK_API_KEY).load_paper_via_import(csv_filename)


def run_consent_migration_loading(csv_filename: str):
    FlaskLoader(FLASK_API_BASE_URL, FLASK_API_KEY).load_consents_via_migration(csv_filename)


def run_pa_loading(csv_filename: str):
    FlaskLoader(FLASK_API_BASE_URL, FLASK_API_KEY).load_processing_activities(csv_filename)


def run_pa_link_patch(csv_filename: str):
    FlaskLoader(FLASK_API_BASE_URL, FLASK_API_KEY).patch_processing_activity_links(csv_filename)


def run_template_loading(csv_filename: str):
    FlaskLoader(FLASK_API_BASE_URL, FLASK_API_KEY).load_templates(csv_filename)


def run_template_approval(csv_filename: str):
    FlaskLoader(FLASK_API_BASE_URL, FLASK_API_KEY).approve_templates(csv_filename)


def run_template_load_and_approve(csv_filename: str):
    """Load templates (Draft) then approve them in one run, reusing the
    name->id stash from the create responses to skip the GET lookup."""
    loader = FlaskLoader(FLASK_API_BASE_URL, FLASK_API_KEY)
    created_ids = loader.load_templates(csv_filename)
    loader.approve_templates(csv_filename, id_map=created_ids)


if __name__ == "__main__":
    pass
