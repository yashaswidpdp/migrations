"""Transform Odoo internal stakeholders (GET /stakeholders) into the flat CSV
the Flask loader posts to /api/stakeholder/create.

Odoo source fields -> Flask:
    name        -> name
    login       -> email          (Odoo stores the email in `login`)
    phone       -> phone          (Odoo emits `false` for empty -> '')
    is_active   -> is_active       (Flask create can't set this; loader patches
                                    via /update-roles only when False)
    role_ids[]  -> role_names      (Odoo role NAMES, deduped — the loader maps
                                    names -> Flask role ids; NEVER carry ids)
    id          -> odoo_source_id  (audit / dedup key, not sent to Flask)

Role ids are intentionally dropped here: Odoo and Flask role ids differ, and the
same role name carries several Odoo ids (DPO=4,5,9). Mapping is by name, done in
the loader against the live Flask role list. See stakeholder_role_mapper.py.
"""

import json
import logging
import os

import pandas as pd
from dotenv import load_dotenv

load_dotenv("config/.env")
DATA_RAW_DIR = os.getenv("DATA_RAW_DIR", "data/raw")
DATA_PROCESSED_DIR = os.getenv("DATA_PROCESSED_DIR", "data/processed")

logger = logging.getLogger("transform_stakeholder")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")


def _clean(value) -> str:
    """Odoo emits `false` (bool) for empty fields; coerce those + None to ''."""
    if value is None or value is False:
        return ""
    s = str(value).strip()
    return "" if s.lower() in ("false", "nan", "none") else s


def _role_names(role_ids) -> list:
    """Flatten Odoo role_ids [{id,name,...}] -> deduped list of role NAMES,
    preserving first-seen order. Drops blanks. One Odoo user can list the same
    role name twice (e.g. user 6 has two 'DPO') -> dedup so Flask gets it once.
    """
    out = []
    seen = set()
    for r in role_ids or []:
        if not isinstance(r, dict):
            continue
        name = _clean(r.get("name"))
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


def _extract_records(raw):
    """The /stakeholders envelope nests the list under `stakeholders`; tolerate a
    bare list or other common wrapper keys too."""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for key in ("stakeholders", "data", "records", "result"):
            if isinstance(raw.get(key), list):
                return raw[key]
    return []


def transform_stakeholder_data(input_filename: str = "raw_stakeholders.json",
                               output_filename: str = "processed_stakeholders.csv"):
    input_path = os.path.join(DATA_RAW_DIR, input_filename)
    if not os.path.exists(input_path):
        logger.error(f"Input file not found: {input_path}")
        return

    with open(input_path, encoding="utf-8") as f:
        raw = json.load(f)

    records = _extract_records(raw)
    logger.info(f"Loaded {len(records)} stakeholders from {input_filename}")

    rows = []
    for s in records:
        if not isinstance(s, dict):
            continue
        names = _role_names(s.get("role_ids"))
        rows.append({
            "odoo_source_id": s.get("id"),
            "name": _clean(s.get("name")),
            # Odoo keeps the email in `login`.
            "email": _clean(s.get("login")).lower(),
            # `false` -> '' so the loader never POSTs a boolean phone.
            "phone": _clean(s.get("phone")),
            # Real bool; Flask create always makes users active, loader only
            # patches when this is explicitly False.
            "is_active": bool(s.get("is_active", True)),
            # Role NAMES only, as a JSON list string the loader maps to ids.
            "role_names": json.dumps(names, ensure_ascii=False),
        })

    os.makedirs(DATA_PROCESSED_DIR, exist_ok=True)
    output_path = os.path.join(DATA_PROCESSED_DIR, output_filename)
    pd.DataFrame(rows).to_csv(output_path, index=False)

    missing_email = sum(1 for r in rows if not r["email"])
    no_roles = sum(1 for r in rows if r["role_names"] == "[]")
    logger.info(
        f"Transformation complete. Saved {len(rows)} stakeholders to {output_path} "
        f"({missing_email} missing email, {no_roles} with no roles)."
    )


if __name__ == "__main__":
    pass
