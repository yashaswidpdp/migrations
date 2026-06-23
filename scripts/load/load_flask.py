import requests
import pandas as pd
import os
import sys
import logging
import ast
import base64
import json
from io import BytesIO
from dotenv import load_dotenv
import openpyxl
from typing import Optional

from scripts.load.stakeholder_role_mapper import StakeholderRoleMapper
from scripts.load import stakeholder_report

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

    def _record_source_map(self, entity, odoo_id, flask_id, sub_key=""):
        """Record an odoo->flask mapping in the migration ledger for entities
        created via the native routes (processing_activity, template), so they
        become idempotent + auditable like the /migration/* entities.

        Best-effort: a recorder failure is logged, never fatal to the load. The
        endpoint is idempotent (200 'exists' when already recorded)."""
        if not odoo_id or flask_id is None:
            return
        try:
            resp = requests.post(
                f"{self.base_url}/migration/source-map",
                headers=self.headers,
                json={
                    "entity": entity,
                    "odoo_source_id": int(odoo_id),
                    "flask_id": int(flask_id),
                    "sub_key": sub_key or "",
                },
                timeout=30,
            )
            if resp.status_code not in (200, 201):
                logger.warning(
                    f"source-map record failed {entity} {odoo_id}->{flask_id}: "
                    f"{resp.status_code} {resp.text[:200]}"
                )
        except (TypeError, ValueError): 
            logger.warning(f"source-map skip {entity}: bad ids {odoo_id}->{flask_id}")
        except Exception as e:
            logger.warning(f"source-map record exception {entity} {odoo_id}: {e}")

    def load_from_csv(self, csv_filename: str, endpoint: str):
        input_path = os.path.join(DATA_PROCESSED_DIR, csv_filename)
        if not os.path.exists(input_path):
            logger.error(f"Input CSV not found: {input_path}")
            return

        # Force `phone` to read as string. pandas otherwise infers the column as
        # float64 whenever blanks are present, turning '9878987819' into the float
        # 9878987819.0; the row-level NaN->None guard below only nulls true NaNs,
        # so a non-blank phone would reach the API as '9878987819.0' (the trailing
        # '.0' then lands in Flask). dtype=str preserves the exact digits (and any
        # leading zero); pandas ignores the key for CSVs that have no phone column.
        df = pd.read_csv(input_path, dtype={"phone": str})
        logger.info(f"Loaded {len(df)} records from {csv_filename}")

        success_count = 0
        failure_rows = []
        error_file = os.path.join(DATA_PROCESSED_DIR, f"errors_{csv_filename}")

        pa_map = self._resolve_pa_ids()
        default_request_type_id = self._resolve_request_type_id()
        request_type_map = self._resolve_request_type_map()

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

            # Resolve request type per record from the enriched name (from
            # /dpgr/id `requestType [id, name]`); fall back to the tenant default.
            rt_name = record_data.get("request_type_name")
            resolved_rt_id = None
            if rt_name:
                resolved_rt_id = request_type_map.get(str(rt_name).strip().lower())
                if resolved_rt_id is None:
                    logger.warning(
                        f"Request type '{rt_name}' not found in Flask request_types; "
                        f"falling back to default id={default_request_type_id}."
                    )
            final_rt_id = resolved_rt_id or default_request_type_id
            if final_rt_id:
                record_data["request_type_id"] = final_rt_id
            record_data.pop("request_type_name", None)

            # assigned_users comes from the transform as a string repr of an ID
            # list (e.g. "[5]"); parse it back to a real list so the backend's
            # /request/create receives valid IDs.
            au_str = record_data.get("assigned_users")
            if au_str and isinstance(au_str, str):
                try:
                    record_data["assigned_users"] = ast.literal_eval(au_str)
                except Exception:
                    record_data.pop("assigned_users", None)

            # Only synthesize a placeholder phone when there is NO email to key
            # on. Injecting a shared dummy phone for email-bearing rows makes
            # every such request collide on that one phone (email=X but
            # phone=<dummy owned by the first row>) -> identity-conflict 409.
            # Email rows must stay phone-less so they resolve by email to the
            # principal the consent migration already created.
            if not record_data.get("phone") and not record_data.get("email"):
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

                # Identity-conflict retry: a 409 mentioning "Data Principal"
                # means the carried phone resolves to a DIFFERENT principal than
                # the email (shared dummy numbers in the source). Re-key by email
                # alone so the request still attaches to the right person. A 409
                # WITHOUT that phrase is the idempotent "already migrated" case.
                if (
                    response.status_code == 409
                    and record_data.get("phone")
                    and "data principal" in response.text.lower()
                ):
                    retry = {k: v for k, v in record_data.items() if k != "phone"}
                    logger.warning(
                        f"Record {index + 1}: phone collided with another principal; "
                        f"retrying email-only."
                    )
                    response = requests.post(
                        f"{self.base_url}{endpoint}",
                        headers=self.headers,
                        json=retry,
                        timeout=30,
                    )

                if response.status_code in [200, 201]:
                    logger.info(f"Loaded record {index + 1}/{len(df)}")
                    success_count += 1
                elif response.status_code == 409 and "data principal" not in response.text.lower():
                    # Genuinely already migrated (source-map hit) — idempotent skip.
                    logger.info(f"Record {index + 1} already migrated. Skipping.")
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

    def load_vendors(self, csv_filename: str = "processed_vendors.csv"):
        """Migrate vendors to /migration/vendor. Resolves Odoo department names
        -> Flask PA ids; idempotent via the source-map (a 409 that is NOT an
        identity conflict = 'already migrated' -> skip). On an identity conflict
        (vendor email/phone resolves to a different principal) retry email-only
        so the vendor still lands."""
        input_path = os.path.join(DATA_PROCESSED_DIR, csv_filename)
        if not os.path.exists(input_path):
            logger.error(f"Vendor CSV not found: {input_path}")
            return
        # phone as string — see load_from_csv: avoids the float '...0' artifact a
        # blank-bearing phone column otherwise gets, which would land in Flask.
        df = pd.read_csv(input_path, dtype={"phone": str})
        logger.info(f"Loaded {len(df)} vendor records from {csv_filename}")

        pa_map = self._resolve_pa_ids()
        attach_manifest = self._load_vendor_attachment_manifest()
        endpoint = f"{self.base_url}/migration/vendor"
        created = skipped = failed = 0
        failure_rows = []

        for index, row in df.iterrows():
            data = {k: (None if isinstance(v, float) and pd.isna(v) else v) for k, v in row.to_dict().items()}

            # Attach decoded documents (sidecar files) back as inline Base64 so
            # the migration endpoint can store them via the real upload service.
            attachments = self._build_attachments(attach_manifest, data.get("odoo_source_id"))
            if attachments:
                data["attachments"] = attachments

            # Odoo department names -> Flask processing-activity ids.
            names = data.pop("processing_activity_names", None)
            pa_ids = []
            if names:
                try:
                    for n in ast.literal_eval(str(names)):
                        pid = pa_map.get(str(n).strip())
                        if pid:
                            pa_ids.append(pid)
                except Exception:
                    pass
            data["processing_activity_ids"] = pa_ids

            # Empty risk -> None so the endpoint stores NULL (no assessed risk).
            if not data.get("risk_level"):
                data["risk_level"] = None

            try:
                resp = requests.post(endpoint, headers=self.headers, json=data, timeout=30)
                if resp.status_code == 409 and data.get("phone") and "principal" in resp.text.lower():
                    retry = {k: v for k, v in data.items() if k != "phone"}
                    logger.warning(f"Vendor row {index + 1}: phone collided with another principal; retrying email-only.")
                    resp = requests.post(endpoint, headers=self.headers, json=retry, timeout=30)

                if resp.status_code in (200, 201):
                    body = resp.json().get("data", {})
                    logger.info(f"Created vendor '{data.get('company_name')}' (id={body.get('id')}, vendor_id={body.get('vendor_id')}).")
                    created += 1
                elif resp.status_code == 409 and "already migrated" in resp.text.lower():
                    # True idempotent skip — the source-map already has this vendor.
                    logger.info(f"Vendor '{data.get('company_name')}' already migrated. Skipping.")
                    skipped += 1
                else:
                    # Real failure — incl. "Vendor already exists for user"
                    # (a different Odoo vendor sharing this contact user). Surface  
                    # it, never silently count as success.
                    logger.error(f"Failed vendor '{data.get('company_name')}': {resp.status_code} - {resp.text[:300]}")
                    failure_rows.append({**data, "error_message": resp.text, "status_code": resp.status_code})
                    failed += 1
            except Exception as e:
                logger.error(f"Exception loading vendor row {index + 1}: {e}")
                failure_rows.append({**data, "error_message": str(e)})
                failed += 1

        if failure_rows:
            err = os.path.join(DATA_PROCESSED_DIR, f"errors_{csv_filename}")
            pd.DataFrame(failure_rows).to_csv(err, index=False)
            logger.error(f"{len(failure_rows)} vendor failures written to {err}")
        logger.info(f"Vendor load complete: {created} created, {skipped} skipped, {failed} failed.")

    def _load_vendor_attachment_manifest(self) -> dict:
        """Read the transform's vendor attachment manifest (keyed by Odoo id).
        Missing manifest => no attachments (older runs / list-only extracts)."""
        path = os.path.join(DATA_PROCESSED_DIR, "vendor_attachments_manifest.json")
        if not os.path.exists(path):
            return {}
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Could not read vendor attachment manifest {path}: {e}")
            return {}

    def _build_attachments(self, manifest: dict, odoo_source_id) -> dict:
        """Re-encode this vendor's sidecar files into the inline-Base64 payload
        the /migration/vendor endpoint expects:

            {field: {"fileName": <name>, "fileContent": <base64>}}

        A sidecar referenced by the manifest but missing on disk is skipped with
        a warning (never fatal — the vendor still migrates without that doc)."""
        if not manifest or odoo_source_id is None:
            return {}
        entry = manifest.get(str(odoo_source_id)) or manifest.get(odoo_source_id)
        if not isinstance(entry, dict):
            return {}
        out = {}
        for field, meta in entry.items():
            if not isinstance(meta, dict):
                continue
            sidecar = meta.get("path")
            if not sidecar or not os.path.exists(sidecar):
                logger.warning(f"Vendor {odoo_source_id} {field}: sidecar missing ({sidecar}); skipping.")
                continue
            with open(sidecar, "rb") as f:
                content = base64.b64encode(f.read()).decode("ascii")
            out[field] = {"fileName": meta.get("fileName") or os.path.basename(sidecar),
                          "fileContent": content}
        return out

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

    def _resolve_request_type_map(self) -> dict:
        """Map every Flask request type name (lower-cased) -> id, so an enriched
        Odoo `requestType` name resolves to the right id instead of a hardcoded
        default. Mirrors the PA / template name->id resolution.

        GET /request-types/ paginates (default 10/page, max 100), so page through
        ALL pages — a single call only sees the first page and silently drops
        types whose names live further down (e.g. lowest ids under id-desc sort)."""
        name_to_id = {}
        page = 1
        try:
            while True:
                response = requests.get(
                    f"{self.base_url}/request-types/",
                    headers=self.headers,
                    params={"page": page, "per_page": 100},
                    timeout=30,
                )
                if response.status_code != 200:
                    break
                body = response.json()
                data = body.get("data", {})
                records = data.get("records", []) if isinstance(data, dict) else data
                if not isinstance(records, list) or not records:
                    break
                for r in records:
                    if isinstance(r, dict) and "name" in r and "id" in r:
                        name_to_id[str(r["name"]).strip().lower()] = r["id"]
                if len(records) < 100:
                    break
                page += 1
        except Exception as e:
            logger.warning(f"Could not build request-type name map: {e}")
        return name_to_id

    def seed_request_types(self, seed_file: str = "request_types_seed.json"):
        """Create the Flask request_types that the migrated Odoo requests refer
        to. Idempotent: skips any name already present. Must run before the
        request load, otherwise `request_type_name` can't resolve to an id.

        Note: the backend allows only ONE request type with is_revoke=True per
        tenant — the seed file reflects that (only the revoke type sets it)."""
        import json
        path = seed_file
        if not os.path.isabs(path) and not os.path.exists(path):
            path = os.path.join(os.getenv("DATA_DIR", "data"), seed_file)
        if not os.path.exists(path):
            logger.error(f"Request-type seed file not found: {path}")
            return
        with open(path, encoding="utf-8") as f:
            payloads = json.load(f)

        existing = self._resolve_request_type_map()  # name(lower) -> id
        created = skipped = failed = 0
        for p in payloads:
            name = str(p.get("name", "")).strip()
            if not name:
                continue
            if name.lower() in existing:
                logger.info(f"Request type '{name}' already exists (id={existing[name.lower()]}). Skipping.")
                skipped += 1
                continue
            try:
                resp = requests.post(
                    f"{self.base_url}/request-types/create",
                    headers=self.headers,
                    json=p,
                    timeout=30,
                )
                if resp.status_code in (200, 201):
                    logger.info(f"Created request type '{name}'.")
                    created += 1
                elif resp.status_code == 400 and "already exists" in resp.text:
                    skipped += 1
                else:
                    logger.error(f"Failed to create request type '{name}': {resp.status_code} - {resp.text}")
                    failed += 1
            except Exception as e:
                logger.error(f"Exception creating request type '{name}': {e}")
                failed += 1
        logger.info(f"Request-type seed summary: {created} created, {skipped} skipped, {failed} failed.")

    def load_request_types(self, json_file: str = "processed_request_types.json"):
        """Load transformed Odoo request types (data/processed) into Flask.
        New names -> POST /request-types/create; names already present ->
        PUT /request-types/<id> so re-runs propagate SLA/flag fixes instead of
        skipping (create never touches an existing record). Idempotent: re-running
        with the same payload is a no-op update. Must run before consent + request
        loads so request_type_name resolves."""
        path = os.path.join(DATA_PROCESSED_DIR, json_file)
        if not os.path.exists(path):
            logger.error(f"Processed request-type file not found: {path}")
            return
        with open(path, encoding="utf-8") as f:
            payloads = json.load(f)

        existing = self._resolve_request_type_map()  # name(lower) -> id
        created = updated = failed = 0
        for p in payloads:
            name = str(p.get("name", "")).strip()
            if not name:
                continue
            rt_id = existing.get(name.lower())
            if rt_id is not None:
                # Already present: PUT the payload so SLA-model changes land on the
                # existing record. Backend updates only the fields we send and
                # leaves the rest (e.g. is_active) untouched.
                try:
                    resp = requests.put(
                        f"{self.base_url}/request-types/{rt_id}",
                        headers=self.headers,
                        json=p,
                        timeout=30,
                    )
                    if resp.status_code in (200, 201):
                        logger.info(f"Updated request type '{name}' (id={rt_id}).")
                        updated += 1
                    else:
                        logger.error(f"Failed to update request type '{name}' (id={rt_id}): {resp.status_code} - {resp.text}")
                        failed += 1
                except Exception as e:
                    logger.error(f"Exception updating request type '{name}' (id={rt_id}): {e}")
                    failed += 1
                continue
            try:
                resp = requests.post(
                    f"{self.base_url}/request-types/create",
                    headers=self.headers,
                    json=p,
                    timeout=30,
                )
                if resp.status_code in (200, 201):
                    logger.info(f"Created request type '{name}'.")
                    created += 1
                else:
                    logger.error(f"Failed to create request type '{name}': {resp.status_code} - {resp.text}")
                    failed += 1
            except Exception as e:
                logger.error(f"Exception creating request type '{name}': {e}")
                failed += 1
        logger.info(f"Request-type load summary: {created} created, {updated} updated, {failed} failed.")

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
                # Backfill the ledger for an already-loaded PA (mirrors the
                # template loader) so a re-run records previously-loaded rows
                # the up-front _resolve_pa_ids() map would otherwise early-skip.
                self._record_source_map(
                    "processing_activity", row.get("odoo_id"), name_to_id[name]
                )
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
                    # Ledger the mapping so PA loads are idempotent + auditable.
                    self._record_source_map("processing_activity", row.get("odoo_id"), new_id)
                    success_count += 1
                elif response.status_code == 400 and "already exists" in response.text.lower():
                    logger.info(f"PA '{name}' already exists. Skipping.")
                    skip_count += 1
                    # Refresh map so children can resolve this parent
                    name_to_id = self._resolve_pa_ids()
                    # Record the existing PA too, so a re-run backfills the ledger.
                    self._record_source_map(
                        "processing_activity", row.get("odoo_id"), name_to_id.get(name)
                    )
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
        # name -> flask id, used to ledger templates that already exist (so a
        # re-run backfills the source-map for previously-loaded templates).
        existing_template_ids = self._fetch_template_id_map()
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
                # Backfill the ledger for an already-loaded template.
                tmpl_sub_key = "|".join(str(row.get(c) or "") for c in
                                        ("template_type", "sub_type", "language"))
                self._record_source_map(
                    "template", row.get("odoo_id"),
                    existing_template_ids.get(name), sub_key=tmpl_sub_key,
                )
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
                    # Ledger every emitted template row under its Odoo source id.
                    # sub_key discriminates fan-out (same odoo_id across multiple
                    # type/channel/language rows) so each lands a distinct mapping.
                    tmpl_sub_key = "|".join(str(row.get(c) or "") for c in
                                            ("template_type", "sub_type", "language"))
                    self._record_source_map(
                        "template", row.get("odoo_id"), new_id, sub_key=tmpl_sub_key
                    )
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

    def patch_template_pa_links(self, csv_filename: str = "processed_templates.csv"):
        """Backfill processing-activity links onto templates ALREADY in Flask.

        Re-runnable. The create pass only links PAs if pa_map was complete at
        create time; templates created earlier (incomplete pa_map / before PAs)
        end up with no PA link. This re-resolves the names against the full pa_map
        and PUTs `processing_activity_ids` onto each existing template. Idempotent
        (PUT replaces the M2M). Skips DEFAULT templates (they cannot carry PAs).
        Requires the PUT plural-key fix in notice_template/crud.py."""
        input_path = os.path.join(DATA_PROCESSED_DIR, csv_filename)
        if not os.path.exists(input_path):
            logger.error(f"CSV not found: {input_path}")
            return 0

        df = pd.read_csv(input_path)
        df = df.where(pd.notnull(df), None)
        name_to_id = self._fetch_template_id_map()   # paginated, full
        pa_map = self._resolve_pa_ids()              # paginated + full endpoint
        logger.info(
            f"patch_template_pa_links: {len(name_to_id)} templates, {len(pa_map)} PAs resolved."
        )

        patched = skipped = failed = 0
        for _, row in df.iterrows():
            name = str(row.get("name", "")).strip()
            if not name:
                continue
            if bool(row.get("is_default", False)):
                skipped += 1                          # default templates carry no PA
                continue

            pa_names_str = row.get("processing_activity_names")
            if not pa_names_str:
                skipped += 1
                continue
            try:
                if isinstance(pa_names_str, str) and pa_names_str.startswith("["):
                    pa_names = ast.literal_eval(pa_names_str)
                elif isinstance(pa_names_str, str):
                    pa_names = [pa_names_str]
                else:
                    pa_names = list(pa_names_str)
            except Exception:
                pa_names = []
            pa_ids = [pa_map[n.strip()] for n in pa_names if str(n).strip() in pa_map]
            if not pa_ids:
                skipped += 1
                continue

            tid = name_to_id.get(name)
            if not tid:
                logger.warning(f"patch_template_pa_links: template '{name}' not in Flask; skipping.")
                failed += 1
                continue
            try:
                resp = requests.put(
                    f"{self.base_url}/notice-templates/{tid}",
                    headers=self.headers,
                    json={"processing_activity_ids": pa_ids},
                    timeout=30,
                )
                if resp.status_code in (200, 201):
                    patched += 1
                else:
                    logger.error(
                        f"patch_template_pa_links '{name}' (id={tid}): "
                        f"{resp.status_code} - {resp.text[:200]}"
                    )
                    failed += 1
            except Exception as e:
                logger.error(f"patch_template_pa_links '{name}': {e}")
                failed += 1

        logger.info(
            f"Template PA-link patch: {patched} patched, {skipped} skipped, {failed} failed."
        )
        return patched

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
        """name -> Flask PA id, across ALL pages. The endpoint paginates (50/page),
        so reading only page 1 silently drops PAs on later pages -> every consent /
        template / request that references a missing PA fails 'PA not found' or
        loses its PA link. Walk every page using the pagination meta."""
        # Use the FULL /processing/activities endpoint, not /simple: /simple omits
        # INACTIVE PAs, but historical consents/requests still reference them (e.g.
        # a deactivated 'Testing' activity) and must resolve. Walk every page.
        out: dict = {}
        page = 1
        try:
            while page <= 1000:                      # hard cap; bad pager can't loop forever
                response = requests.get(
                    f"{self.base_url}/processing/activities",
                    headers=self.headers,
                    params={"page": page, "per_page": 100},
                    timeout=30,
                )
                if response.status_code != 200:
                    break
                data = response.json().get("data", {})
                if isinstance(data, dict):
                    records = data.get("records", [])
                    pag = data.get("pagination", {}) or {}
                else:
                    records = data if isinstance(data, list) else []
                    pag = {}
                for pa in records:
                    if isinstance(pa, dict) and "name" in pa and "id" in pa:
                        out[pa["name"]] = pa["id"]
                total_pages = pag.get("totalPages")
                has_next = pag.get("hasNext")
                if not records or has_next is False or (total_pages and page >= int(total_pages)):
                    break
                page += 1
        except Exception as e:
            logger.warning(f"Could not fetch PA list from Flask API: {e}")
        return out

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

    # ------------------------------------------------------------------ #
    #  INTERNAL STAKEHOLDER LOADING                                        #
    # ------------------------------------------------------------------ #

    def load_stakeholders(self, csv_filename: str = "processed_stakeholders.csv"):
        """Migrate internal stakeholders via the email-free migration endpoint
        POST /migration/stakeholder.

        That endpoint (migration_ext) creates a Backend PAManager user with NO
        outbound communication — no welcome/credential email, no SMTP, no OTP,
        no notifications, no background jobs — and is idempotent via the
        migration source-map. A historical backfill must never email real users.

        Per stakeholder:
          * validate email (skip+log when missing)
          * map Odoo role NAMES -> Flask role ids (fail+log on any unmapped role)
          * POST; 409 "already migrated" is the idempotent skip, an existing
            user with the same email is reused/mapped (created=false).
        A single failure never aborts the run — every record produces a report
        row, and a CSV/JSON summary is written at the end.

        NOTE: like the public route, this endpoint always creates the user
        Active; Odoo `is_active` is not applied (all source records are active).
        """
        input_path = os.path.join(DATA_PROCESSED_DIR, csv_filename)
        if not os.path.exists(input_path):
            logger.error(f"Stakeholder CSV not found: {input_path}")
            return

        df = pd.read_csv(input_path)
        logger.info(f"Loaded {len(df)} stakeholder records from {csv_filename}")

        mapper = StakeholderRoleMapper(self.base_url, self.headers)
        endpoint = f"{self.base_url}/migration/stakeholder"
        results = []

        for index, row in df.iterrows():
            data = {k: (None if isinstance(v, float) and pd.isna(v) else v) for k, v in row.to_dict().items()}
            odoo_id = data.get("odoo_source_id")
            name = str(data.get("name") or "").strip()
            email = str(data.get("email") or "").strip().lower()
            phone = str(data.get("phone") or "").strip() or None

            base = {"odoo_source_id": odoo_id, "name": name, "email": email}

            # ---- email required ----
            if not email:
                res = {**base, "status": stakeholder_report.FAILED, "reason": "Missing email (Odoo login was false)"}
                stakeholder_report.log_result(res); results.append(res)
                continue

            # ---- role name -> Flask id (never carry Odoo ids) ----
            try:
                role_names = ast.literal_eval(str(data.get("role_names") or "[]"))
                if not isinstance(role_names, list):
                    role_names = []
            except Exception:
                role_names = []
            role_ids, unmapped = mapper.resolve(role_names)
            if unmapped:
                res = {**base, "status": stakeholder_report.FAILED,
                       "reason": f"Role(s) not found in Flask: {', '.join(unmapped)}",
                       "role_names": role_names}
                stakeholder_report.log_result(res); results.append(res)
                continue

            # ---- create via migration endpoint (no email, idempotent) ----
            payload = {"odoo_source_id": odoo_id, "name": name, "email": email, "role_ids": role_ids}
            if phone:
                payload["phone"] = phone
            try:
                resp = requests.post(endpoint, headers=self.headers, json=payload, timeout=60)
                if resp.status_code in (200, 201):
                    body = resp.json().get("data") or {}
                    created = body.get("created", True)
                    res = {**base,
                           "status": stakeholder_report.CREATED if created else stakeholder_report.UPDATED,
                           "flask_user_id": body.get("id"), "role_ids": role_ids}
                elif resp.status_code == 409:
                    # source-map hit -> already migrated.
                    res = {**base, "status": stakeholder_report.SKIPPED,
                           "flask_user_id": (resp.json().get("data") or {}).get("id"),
                           "reason": "Already migrated (source-map)"}
                else:
                    res = {**base, "status": stakeholder_report.FAILED,
                           "reason": f"Create failed: {resp.status_code} - {resp.text[:200]}"}
            except Exception as e:
                res = {**base, "status": stakeholder_report.FAILED, "reason": f"Create exception: {e}"}
            stakeholder_report.log_result(res); results.append(res)

        stakeholder_report.write_report(results, csv_filename)

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
                # An identity collision on phone (the consent's principal shares a
                # phone with a different existing principal) must NOT be swallowed
                # as "already migrated" — retry email-only so the consent still
                # attaches, mirroring the vendor loader.
                if response.status_code == 409 and record.get("phone") and "phone" in response.text.lower():
                    retry = {k: v for k, v in record.items() if k != "phone"}
                    logger.warning(f"Consent {index + 1}: phone collided with another principal; retrying email-only.")
                    response = requests.post(
                        f"{self.base_url}/migration/consent",
                        headers=self.headers, json=retry, timeout=60,
                    )

                if response.status_code in (200, 201):
                    logger.info(f"Loaded consent {index + 1}/{len(df)}")
                    success += 1
                elif response.status_code == 409 and "already migrated" in response.text.lower():
                    # True idempotent skip — the source-map already has this consent.
                    logger.info(f"Consent {index + 1} already migrated. Skipping.")
                    success += 1
                else:
                    # Real failure (incl. unrecoverable 409s, 400s). Never count as ok.
                    logger.error(f"Consent {index + 1} failed: {response.status_code} - {response.text[:300]}")
                    failures.append({**record, "error": response.text, "status_code": response.status_code})
            except Exception as e:
                logger.exception(f"Exception loading consent {index + 1}: {e}")
                failures.append({**record, "error": str(e)})

        err_file = os.path.join(DATA_PROCESSED_DIR, f"errors_{csv_filename}")
        if failures:
            pd.DataFrame(failures).to_csv(err_file, index=False)
            logger.error(f"{len(failures)} consent failures written to {err_file}")
        elif os.path.exists(err_file):
            # Clean run: drop the stale errors file so audits don't read old failures.
            os.remove(err_file)
            logger.info(f"Cleared stale errors file {err_file} (0 failures this run).")

        logger.info(f"Consent migration load complete: {success} ok, {len(failures)} failed.")


def run_loading(csv_filename: str, endpoint: str):
    FlaskLoader(FLASK_API_BASE_URL, FLASK_API_KEY).load_from_csv(csv_filename, endpoint)


def run_request_type_seeding(seed_file: str = "request_types_seed.json"):
    FlaskLoader(FLASK_API_BASE_URL, FLASK_API_KEY).seed_request_types(seed_file)


def run_request_type_loading(json_file: str = "processed_request_types.json"):
    FlaskLoader(FLASK_API_BASE_URL, FLASK_API_KEY).load_request_types(json_file)


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


def run_template_pa_link_patch(csv_filename: str = "processed_templates.csv"):
    FlaskLoader(FLASK_API_BASE_URL, FLASK_API_KEY).patch_template_pa_links(csv_filename)


def run_vendor_loading(csv_filename: str = "processed_vendors.csv"):
    FlaskLoader(FLASK_API_BASE_URL, FLASK_API_KEY).load_vendors(csv_filename)


def run_stakeholder_loading(csv_filename: str = "processed_stakeholders.csv"):
    FlaskLoader(FLASK_API_BASE_URL, FLASK_API_KEY).load_stakeholders(csv_filename)


if __name__ == "__main__":
    pass
