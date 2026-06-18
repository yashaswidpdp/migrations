"""Map Odoo role NAMES -> Flask role IDs by name, fetched live from Flask.

Why name-based: Odoo and Flask assign different ids to the same role, and the
same Odoo role name carries several ids (DPO=4,5,9). The only stable join key is
the role name, so we fetch the Flask role catalogue at run time and never
hardcode an id.

Optional alias file (data/stakeholder_role_aliases.json) bridges naming
differences between the two systems, e.g.

    { "PA Manager": "Full Access", "DPO": "Full Access" }

maps Odoo "PA Manager"/"DPO" onto whatever role actually exists in the Flask
tenant. Aliases are applied before the name lookup. Matching is
case-insensitive throughout.

Every resolution (hit, alias, miss) is logged for auditability.
"""

import json
import logging
import os

import requests

logger = logging.getLogger("stakeholder_role_mapper")

DATA_DIR = os.getenv("DATA_DIR", "data")
ROLE_ALIAS_FILE = os.getenv("STAKEHOLDER_ROLE_ALIAS_FILE", "stakeholder_role_aliases.json")


class StakeholderRoleMapper:
    def __init__(self, base_url: str, headers: dict):
        self.base_url = base_url.rstrip("/")
        self.headers = headers
        self._name_to_id = None          # lower(flask role name) -> id
        self._aliases = None             # lower(odoo name) -> flask name (original case)

    # ---- catalogue -------------------------------------------------------- #
    def _fetch_flask_roles(self) -> dict:
        """GET /roles/details (paginated) -> {lower(name): id}. Only non-system,
        tenant-scoped roles are returned by that endpoint."""
        name_to_id = {}
        page = 1
        while True:
            try:
                resp = requests.get(
                    f"{self.base_url}/roles/details",
                    headers=self.headers,
                    params={"page": page, "per_page": 100},
                    timeout=30,
                )
            except Exception as e:
                logger.warning(f"Could not fetch Flask roles: {e}")
                break
            if resp.status_code != 200:
                logger.warning(f"Roles fetch failed: {resp.status_code} - {resp.text[:200]}")
                break
            data = resp.json().get("data", {})
            records = data.get("records", []) if isinstance(data, dict) else []
            for r in records:
                if isinstance(r, dict) and r.get("name") and r.get("id") is not None:
                    name_to_id[str(r["name"]).strip().lower()] = r["id"]
            pagination = data.get("pagination", {}) if isinstance(data, dict) else {}
            total_pages = pagination.get("totalPages", pagination.get("total_page", 1)) or 1
            if page >= total_pages:
                break
            page += 1
        logger.info(f"Fetched {len(name_to_id)} Flask roles: {sorted(name_to_id)}")
        return name_to_id

    def _load_aliases(self) -> dict:
        path = ROLE_ALIAS_FILE
        if not os.path.isabs(path) and not os.path.exists(path):
            path = os.path.join(DATA_DIR, ROLE_ALIAS_FILE)
        if not os.path.exists(path):
            return {}
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
            aliases = {str(k).strip().lower(): str(v).strip() for k, v in raw.items()}
            logger.info(f"Loaded {len(aliases)} stakeholder role aliases from {path}")
            return aliases
        except Exception as e:
            logger.warning(f"Could not read role alias file {path}: {e}")
            return {}

    def _ensure_loaded(self):
        if self._name_to_id is None:
            self._name_to_id = self._fetch_flask_roles()
        if self._aliases is None:
            self._aliases = self._load_aliases()

    # ---- resolution ------------------------------------------------------- #
    def resolve(self, role_names: list):
        """Resolve a list of Odoo role names -> (flask_role_ids, unmapped_names).

        Applies the alias map, then a case-insensitive name lookup against the
        live Flask catalogue. Deduplicates the resulting ids. Any name that has
        no Flask match is returned in `unmapped` so the caller can fail the
        stakeholder per the migration contract.
        """
        self._ensure_loaded()
        ids, unmapped, seen = [], [], set()
        for raw_name in role_names or []:
            name = str(raw_name).strip()
            if not name:
                continue
            target = self._aliases.get(name.lower(), name)
            rid = self._name_to_id.get(target.strip().lower())
            if rid is None:
                logger.warning(f"Role '{name}' (-> '{target}') not found in Flask roles.")
                unmapped.append(name)
                continue
            if name.lower() != target.lower():
                logger.info(f"Role alias applied: Odoo '{name}' -> Flask '{target}' (id={rid}).")
            else:
                logger.info(f"Role mapped: '{name}' -> Flask id={rid}.")
            if rid not in seen:
                seen.add(rid)
                ids.append(rid)
        return ids, unmapped
