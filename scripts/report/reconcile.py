"""Migration reconciliation audit (Odoo -> Flask).

Builds a per-entity source-vs-destination ledger across the whole pipeline and
renders a single, human-readable .txt audit report. The goal is to *prove* how
much data landed (and explain every record that did not), instead of eyeballing
the migration log.

Four count layers per entity:
    1. SOURCE    data/raw/*               what Odoo gave us
    2. STAGED    data/processed/*         what transform produced
    3. MIGRATED  migration_source_map     what actually landed in Flask
    4. FAILED    data/processed/errors_*  rejected rows + grouped reasons

MIGRATED counts read the live Postgres `migration_source_map` via
`docker exec ... psql` so this audit needs no new Python DB dependency. Every
external read degrades to "unknown" rather than raising, so the report always
renders. Container / creds are overridable via env:
    RECON_PG_CONTAINER (default privacium_postgres)
    RECON_PG_USER      (default yashaswi)
    RECON_PG_DB        (default privacium_db)

USAGE
-----
Offline (ledger-trust, no network):
    python -m scripts.report.reconcile

Live mode (--live): refresh SOURCE from live Odoo and VERIFY each ledger-claimed
"migrated" record against the live Flask app (in its own container), surfacing a
DRIFT verdict when the ledger says landed but the app has no such record.

Tokens are NOT set in the global shell and are NEVER hardcoded here. They are
read from migration/config/.env (gitignored, the same file load_flask uses),
which is auto-loaded at import. Put/confirm these keys in config/.env:

    ODOO_JWT_TOKEN=<source Bearer>                # live Odoo (SOURCE)
    FLASK_API_BASE_URL=http://localhost:<port>   # dest container
    FLASK_API_KEY=<dest Bearer>                   # live Flask (MIGRATED)
    FLASK_TENANT_DOMAIN=<tenant host>             # optional: only if dest needs a Host header

then just:

    cd migration
    python -m scripts.report.reconcile --live    # or set RECON_LIVE=1 in config/.env

Self-test (DB-free internal consistency check):
    python -m scripts.report.reconcile --self-test
"""

from __future__ import annotations

import collections
import csv
import json
import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

# Load migration/config/.env (same file load_flask uses) so tokens live in one
# gitignored file instead of the global shell. Real run: edit config/.env, then
# `python -m scripts.report.reconcile --live`. Never hardcode secrets here.
try:
    from dotenv import load_dotenv
    load_dotenv("config/.env")
except Exception:
    pass

RAW_DIR = os.getenv("DATA_RAW_DIR", "data/raw")
PROC_DIR = os.getenv("DATA_PROCESSED_DIR", "data/processed")
DATA_DIR = os.getenv("DATA_DIR", "data")
REPORT_PATH = os.path.join(PROC_DIR, "reconciliation_report.txt")
ACCEPTED_LOSS_FILE = os.path.join(DATA_DIR, "accepted_loss.json")

PG_CONTAINER = os.getenv("RECON_PG_CONTAINER", "privacium_postgres")
PG_USER = os.getenv("RECON_PG_USER", "yashaswi")
PG_DB = os.getenv("RECON_PG_DB", "privacium_db")

# --- live-API mode (opt-in) ------------------------------------------------- #
# When enabled the audit refreshes SOURCE from live Odoo and verifies MIGRATED
# against the live Flask app (in its own container) instead of trusting only the
# migration_source_map ledger. Tokens are read from env *per run* and never
# logged or written to disk.
#   RECON_LIVE=1                 turn it on (or pass --live)
#   ODOO_BASE_URL, ODOO_JWT_TOKEN          source side  (Bearer)
#   FLASK_API_BASE_URL, FLASK_API_KEY      dest side    (Bearer)
#   FLASK_TENANT_DOMAIN          optional Host header for the Flask container
#   RECON_HTTP_TIMEOUT           per-request seconds (default 20)
LIVE = os.getenv("RECON_LIVE") == "1"
ODOO_BASE_URL = os.getenv("ODOO_BASE_URL",
                          "https://tool.dpdp-portal.dpdpconsultants.com/api")
ODOO_JWT_TOKEN = os.getenv("ODOO_JWT_TOKEN")
FLASK_API_BASE_URL = os.getenv("FLASK_API_BASE_URL")
FLASK_API_KEY = os.getenv("FLASK_API_KEY")
FLASK_TENANT_DOMAIN = os.getenv("FLASK_TENANT_DOMAIN")
HTTP_TIMEOUT = int(os.getenv("RECON_HTTP_TIMEOUT", "20"))

BAR_WIDTH = 28


# --------------------------------------------------------------------------- #
# Low-level counters (all return None on any failure -> "unknown")
# --------------------------------------------------------------------------- #
def count_csv_rows(path: str) -> Optional[int]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, newline="", encoding="utf-8") as f:
            return sum(1 for _ in csv.DictReader(f))
    except Exception:
        return None


def count_json_list(path: str, *list_keys: str) -> Optional[int]:
    """Count items in the first matching top-level list key (e.g. 'vendors')."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, list):
            return len(d)
        for k in list_keys:
            node = d.get(k) if isinstance(d, dict) else None
            if isinstance(node, list):
                return len(node)
            if isinstance(node, dict):  # e.g. {"data": {"templates": [...]}}
                for kk in list_keys:
                    if isinstance(node.get(kk), list):
                        return len(node[kk])
    except Exception:
        return None
    return None


def count_tree_nodes(path: str, key: str) -> Optional[int]:
    """Count every node carrying an 'id' in a nested hierarchy (processing
    activities arrive as a tree, not a flat list)."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return None
    seen = 0

    def walk(node):
        nonlocal seen
        if isinstance(node, dict):
            if "id" in node and "name" in node:
                seen += 1
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(d.get(key) if isinstance(d, dict) else d)
    return seen or None


def error_breakdown(path: str) -> tuple[int, collections.Counter, set]:
    """(total failed rows, Counter of short reason -> count, set of failed odoo ids)."""
    if not os.path.exists(path):
        return 0, collections.Counter(), set()
    try:
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except Exception:
        return 0, collections.Counter(), set()
    counter = collections.Counter()
    ids = set()
    for r in rows:
        msg = r.get("error") or r.get("error_message") or ""
        try:  # error column is often a JSON envelope -> pull .message
            msg = json.loads(msg).get("message", msg)
        except Exception:
            pass
        counter[(str(msg).strip() or "(unspecified)")[:70]] += 1
        oid = r.get("odoo_source_id") or r.get("odoo_id")
        if oid not in (None, ""):
            try:
                ids.add(int(oid))
            except (TypeError, ValueError):
                ids.add(str(oid))
    return len(rows), counter, ids


# --------------------------------------------------------------------------- #
# Live Postgres reads (migration_source_map = source of truth for "landed")
# --------------------------------------------------------------------------- #
def _psql(sql: str) -> Optional[str]:
    try:
        out = subprocess.run(
            ["docker", "exec", PG_CONTAINER, "psql", "-U", PG_USER, "-d", PG_DB,
             "-t", "-A", "-F", ",", "-c", sql],
            capture_output=True, text=True, timeout=15,
        )
        return out.stdout if out.returncode == 0 else None
    except Exception:
        return None


def sourcemap_counts() -> dict:
    # count DISTINCT source ids: a template fans out to many ledger rows under
    # one odoo_source_id, but MIGRATED must compare to the SOURCE count of
    # distinct Odoo records. For 1:1 entities distinct == total (unchanged).
    out = _psql("SELECT entity, count(DISTINCT odoo_source_id) "
                "FROM migration_source_map GROUP BY entity;")
    counts = {}
    if out:
        for line in out.strip().splitlines():
            if "," in line:
                ent, n = line.split(",", 1)
                counts[ent.strip()] = int(n)
    return counts


def sourcemap_ids() -> dict:
    """{entity: {odoo_source_id as str}} — the exact ids that landed in Flask."""
    out = _psql("SELECT entity, odoo_source_id FROM migration_source_map;")
    grouped: dict = collections.defaultdict(set)
    if out:
        for line in out.strip().splitlines():
            if "," in line:
                ent, oid = line.split(",", 1)
                grouped[ent.strip()].add(oid.strip())
    return grouped


def sourcemap_pairs() -> dict:
    """{entity: {odoo_source_id(str): {flask_id(str), ...}}} — the ledger's
    source->dest join. A value is a SET because one source (a template) can map
    to many Flask rows (fan-out). Used to confirm migrated rows exist in the app."""
    out = _psql("SELECT entity, odoo_source_id, flask_id FROM migration_source_map;")
    grouped: dict = collections.defaultdict(lambda: collections.defaultdict(set))
    if out:
        for line in out.strip().splitlines():
            parts = line.split(",")
            if len(parts) >= 3:
                ent, oid, fid = parts[0].strip(), parts[1].strip(), parts[2].strip()
                grouped[ent][oid].add(fid)
    return grouped


# --------------------------------------------------------------------------- #
# Live-API reads (opt-in: prove landing end-to-end instead of trusting ledger)
# --------------------------------------------------------------------------- #
# entity -> (Flask GET path, id field, candidate list keys in the JSON envelope)
# Flask list endpoints paginate (utils/pagination.py): param `per_page` is capped
# at 100, records live under data.records, and the page meta sits at
# data.pagination with camelCase keys (totalPages/hasNext). The reader below
# walks every page using that meta, so live counts are complete, not page-1 only.
FLASK_PER_PAGE = 100  # server hard cap (get_pagination_params max_per_page)
FLASK_LIST_ENDPOINTS = {
    "consent": ("/consent/", "id", ("records", "consents", "data", "results", "items")),
    "vendor": ("/vendor/list", "id", ("records", "vendors", "data", "results", "items")),
    "stakeholder": ("/auth/backend-users", "id", ("records", "users", "data", "results", "items")),
    "processing_activity": ("/processing/activities/simple", "id",
                            ("records", "activities", "data", "results", "items")),
    "template": ("/notice-templates/", "id", ("records", "templates", "data", "results", "items")),
}

# meta keys the server uses (both camelCase and snake_case builders exist)
_TOTAL_PAGES_KEYS = ("totalPages", "total_pages", "pages", "last_page")
_HAS_NEXT_KEYS = ("hasNext", "has_next")


def _find_pagination_meta(payload):
    """Recursively locate the pagination meta dict (the one carrying totalPages /
    hasNext / pages) anywhere in the envelope. Returns {} if none."""
    if isinstance(payload, dict):
        if any(k in payload for k in (_TOTAL_PAGES_KEYS + _HAS_NEXT_KEYS)):
            return payload
        for v in payload.values():
            found = _find_pagination_meta(v)
            if found:
                return found
    elif isinstance(payload, list):
        for v in payload:
            found = _find_pagination_meta(v)
            if found:
                return found
    return {}


def _find_list_of_dicts(payload, prefer_keys=()):
    """Pull the record list out of an arbitrary JSON envelope.
    Prefers the named keys, else returns the first list-of-dicts found (BFS)."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for k in prefer_keys:
        node = payload.get(k)
        if isinstance(node, list):
            return node
        if isinstance(node, dict):
            inner = _find_list_of_dicts(node, prefer_keys)
            if inner:
                return inner
    for v in payload.values():  # fallback: first list-of-dicts anywhere
        if isinstance(v, list) and (not v or isinstance(v[0], dict)):
            return v
    for v in payload.values():
        if isinstance(v, dict):
            inner = _find_list_of_dicts(v, prefer_keys)
            if inner:
                return inner
    return []


def _http_get_json(url: str, token: str, host: Optional[str] = None):
    import requests
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if host:
        headers["Host"] = host.strip()
    resp = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


_LIVE_CACHE: dict = {}  # memoize live reads within a single run (entity-keyed)


def live_dest_records(entity: str) -> Optional[list]:
    """ALL record dicts the live Flask app returns for `entity`, across every
    page. None => could not read (treated as 'unknown', never as success).
    Walks pages using the server's pagination meta (totalPages / hasNext); falls
    back to 'stop on a short page' when no meta is present. Cached per run."""
    ck = ("dest", entity)
    if ck in _LIVE_CACHE:
        return _LIVE_CACHE[ck]
    spec = FLASK_LIST_ENDPOINTS.get(entity)
    if not spec or not (FLASK_API_BASE_URL and FLASK_API_KEY):
        return None
    path, _id_field, keys = spec
    base = FLASK_API_BASE_URL.rstrip("/")
    out: list = []
    try:
        page, max_pages = 1, 1000  # cap so a bad pager can't loop forever
        while page <= max_pages:
            sep = "&" if "?" in path else "?"
            url = f"{base}{path}{sep}page={page}&per_page={FLASK_PER_PAGE}"
            payload = _http_get_json(url, FLASK_API_KEY, FLASK_TENANT_DOMAIN)
            rows = _find_list_of_dicts(payload, keys)
            if not rows:
                break
            out.extend(r for r in rows if isinstance(r, dict))
            meta = _find_pagination_meta(payload)
            has_next = next((meta[k] for k in _HAS_NEXT_KEYS if k in meta), None)
            total_pages = next((meta[k] for k in _TOTAL_PAGES_KEYS if k in meta), None)
            if has_next is not None:
                if not has_next:
                    break
            elif total_pages is not None:
                if page >= int(total_pages):
                    break
            elif len(rows) < FLASK_PER_PAGE:  # short page, no meta => that's all
                break
            page += 1
    except Exception:
        return None
    _LIVE_CACHE[ck] = out
    return out


def live_dest_ids(entity: str) -> Optional[set]:
    """Set of flask record ids present in the live app. None => unreadable."""
    records = live_dest_records(entity)
    if records is None:
        return None
    id_field = FLASK_LIST_ENDPOINTS[entity][1]
    return {str(r[id_field]).strip() for r in records
            if r.get(id_field) is not None}


def live_source_count(entity: str) -> Optional[int]:
    """Live SOURCE count straight from Odoo, reusing the proven extractor (auth +
    pagination). None => unreadable, caller falls back to the file snapshot."""
    if not ODOO_JWT_TOKEN:
        return None
    try:
        from scripts.extract.extract_odoo import OdooExtractor
    except Exception:
        return None
    try:
        ex = OdooExtractor(ODOO_BASE_URL, ODOO_JWT_TOKEN,
                           os.getenv("ODOO_SESSION_ID", ""))
        if entity == "consent":
            return len(ex.fetch_records("/dpcm/dashboard") or [])
        if entity == "request":
            return len(ex.fetch_records("/dpgr/dashboard") or [])
        if entity == "vendor":
            return _count_in(ex.fetch_simple("/vendors_details"), "vendors")
        if entity == "stakeholder":
            return _count_in(ex.fetch_simple("/stakeholders"), "stakeholders")
        if entity == "processing_activity":
            raw = ex.fetch_simple("/processing_activities")
            return _count_tree(raw, "processingActivities")
        if entity == "template":
            return _count_in(ex.fetch_simple("/v2/get/templates"), "templates", "data")
    except Exception:
        return None
    return None


def live_source_records(entity: str) -> Optional[dict]:
    """{odoo_id(str): source_record} pulled live from Odoo, for field-level diff.
    None => unreadable. Tree entities (PA) are flattened to their id-bearing nodes.
    Cached per run."""
    ck = ("src", entity)
    if ck in _LIVE_CACHE:
        return _LIVE_CACHE[ck]
    if not ODOO_JWT_TOKEN:
        return None
    try:
        from scripts.extract.extract_odoo import OdooExtractor
    except Exception:
        return None

    def _by_id(rows):
        out = {}
        for r in rows or []:
            if isinstance(r, dict) and r.get("id") is not None:
                out[str(r["id"]).strip()] = r
        return out

    result = None
    try:
        ex = OdooExtractor(ODOO_BASE_URL, ODOO_JWT_TOKEN,
                           os.getenv("ODOO_SESSION_ID", ""))
        if entity == "consent":
            result = _by_id(ex.fetch_records("/dpcm/dashboard"))
        elif entity == "request":
            result = _by_id(ex.fetch_records("/dpgr/dashboard"))
        elif entity == "vendor":
            result = _by_id(_find_list_of_dicts(ex.fetch_simple("/vendors_details"), ("vendors",)))
        elif entity == "stakeholder":
            result = _by_id(_find_list_of_dicts(ex.fetch_simple("/stakeholders"), ("stakeholders",)))
        elif entity == "template":
            result = _by_id(_find_list_of_dicts(ex.fetch_simple("/v2/get/templates"),
                                                ("templates", "data")))
        elif entity == "processing_activity":
            raw = ex.fetch_simple("/processing_activities")
            flat = {}

            def walk(node):
                if isinstance(node, dict):
                    if node.get("id") is not None and "name" in node:
                        flat[str(node["id"]).strip()] = node
                    for v in node.values():
                        walk(v)
                elif isinstance(node, list):
                    for v in node:
                        walk(v)
            walk(raw.get("processingActivities") if isinstance(raw, dict) else raw)
            result = flat
        if result is not None:
            _LIVE_CACHE[ck] = result
        return result
    except Exception:
        return None
    return None


def _count_in(payload, *keys) -> Optional[int]:
    rows = _find_list_of_dicts(payload, keys)
    return len(rows) if rows else None


def _count_tree(payload, key) -> Optional[int]:
    seen = 0

    def walk(node):
        nonlocal seen
        if isinstance(node, dict):
            if "id" in node and "name" in node:
                seen += 1
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(payload.get(key) if isinstance(payload, dict) else payload)
    return seen or None


def csv_ids(path: str, cols=("id", "odoo_source_id")) -> set:
    if not os.path.exists(path):
        return set()
    try:
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except Exception:
        return set()
    if not rows:
        return set()
    col = next((c for c in cols if c in rows[0]), None)
    if col is None:
        return set()
    return {str(r[col]).strip() for r in rows if str(r.get(col, "")).strip()}


def json_ids(path: str, list_key: str, id_field: str = "id") -> set:
    if not os.path.exists(path):
        return set()
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return set()
    node = d.get(list_key) if isinstance(d, dict) else d
    if not isinstance(node, list):
        return set()
    return {str(x[id_field]).strip() for x in node
            if isinstance(x, dict) and x.get(id_field) is not None}


def tree_ids(path: str, key: str) -> set:
    """Odoo ids of every node carrying id+name in a nested tree (processing
    activities), matching how count_tree_nodes counts the source."""
    if not os.path.exists(path):
        return set()
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return set()
    ids: set = set()

    def walk(node):
        if isinstance(node, dict):
            if "id" in node and "name" in node and node["id"] is not None:
                ids.add(str(node["id"]).strip())
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(d.get(key) if isinstance(d, dict) else d)
    return ids


def json_nested_ids(path: str, list_keys, id_field: str = "id") -> set:
    """Odoo ids from a (possibly nested) JSON list, e.g. data.templates."""
    if not os.path.exists(path):
        return set()
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return set()
    rows = _find_list_of_dicts(d, tuple(list_keys))
    return {str(x[id_field]).strip() for x in rows
            if isinstance(x, dict) and x.get(id_field) is not None}


# --------------------------------------------------------------------------- #
# Field-level value-equality diff (--live): compare every mapped field of every
# migrated record, source (Odoo) vs dest (live Flask), joined via the source-map.
# Safety rule: a field is only flagged MISMATCH when BOTH sides carry a value.
# A missing source value (map uncertainty for an entity) is SKIPPED, never a
# false mismatch — same discipline as the pagination fix.
# --------------------------------------------------------------------------- #
import re as _re


def _n_token(v):
    """Casefold + drop non-alphanumerics, so 'Deemed consent' == 'Deemed Consent'
    and 'legacy' == 'Legacy'. Returns '' for empty/None."""
    if v is None:
        return ""
    return _re.sub(r"[^a-z0-9]", "", str(v).strip().casefold())


def _n_digits(v):
    return _re.sub(r"\D", "", str(v)) if v is not None else ""


def _n_date(v):
    d = _parse_iso_or_dmy(v)
    return d.date().isoformat() if d else _n_token(v)


def _parse_iso_or_dmy(value):
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
                "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s[:19], fmt)
        except ValueError:
            continue
    return None


def _alias(mapping):
    """Normalizer that tokenizes then remaps known-divergent enum tokens to a
    shared canonical token (e.g. source 'mandatory' -> dest 'Mandatory/Regulatory')."""
    def norm(v):
        t = _n_token(v)
        return mapping.get(t, t)
    return norm


def _arr(value, idx):
    """Odoo often packs [id, 'Name'] / [id, 'Name', ...]; pull one index safely."""
    if isinstance(value, (list, tuple)) and len(value) > idx:
        return value[idx]
    return value


# Known enum divergences (everything else is handled by plain token-equality).
_CONSENT_PROC_TYPE = {"mandatory": "mandatoryregulatory"}
_REQ_STATUS = {
    "notassigned": "initiated",
    "assignedtodpo": "assigntodpo",
    "assigntopamanager": "assigntopamanager",
}

# entity -> list of (label, source_getter, dest_getter, normalizer)
FIELD_MAPS = {
    "consent": [
        ("name", lambda s: _arr(s.get("name"), 1), lambda d: d.get("name"), _n_token),
        ("email", lambda s: s.get("eMail"), lambda d: d.get("email"), _n_token),
        ("phone", lambda s: s.get("phone"), lambda d: d.get("phone"), _n_digits),
        ("status", lambda s: s.get("status"), lambda d: d.get("status"), _n_token),
        ("consent_type", lambda s: s.get("paperType"), lambda d: d.get("consent_type"), _n_token),
        ("legacy_type", lambda s: s.get("legacyType"), lambda d: d.get("legacy_type"), _n_token),
        ("processing_type", lambda s: s.get("userActivityType"),
         lambda d: d.get("processing_type"), _alias(_CONSENT_PROC_TYPE)),
    ],
    "request": [
        ("name", lambda s: _arr(s.get("name"), 1) if isinstance(s.get("name"), list) else s.get("name"),
         lambda d: d.get("name"), _n_token),
        ("email", lambda s: s.get("eMail"), lambda d: d.get("email"), _n_token),
        ("phone", lambda s: s.get("phone"), lambda d: d.get("phone"), _n_digits),
        ("status", lambda s: s.get("status"), lambda d: d.get("status"), _alias(_REQ_STATUS)),
    ],
    "template": [
        ("name", lambda s: s.get("name"), lambda d: d.get("name"), _n_token),
        ("language", lambda s: s.get("language"), lambda d: d.get("language"), _n_token),
        ("sub_type", lambda s: s.get("sub_type") or s.get("subType"),
         lambda d: d.get("sub_type"), _n_token),
        ("template_type", lambda s: s.get("template_type") or s.get("templateType"),
         lambda d: d.get("template_type"), _n_token),
    ],
    "vendor": [
        ("company_name", lambda s: s.get("company_name") or s.get("name"),
         lambda d: d.get("company_name"), _n_token),
        ("status", lambda s: s.get("status"), lambda d: d.get("status"), _n_token),
    ],
    "stakeholder": [
        ("name", lambda s: s.get("name"), lambda d: d.get("name"), _n_token),
        ("email", lambda s: s.get("login") or s.get("email"), lambda d: d.get("email"), _n_token),
    ],
    "processing_activity": [
        ("name", lambda s: s.get("name"), lambda d: d.get("name"), _n_token),
    ],
}


# Raw source keys each entity's explicit FIELD_MAPS already consume, so coverage
# accounting doesn't double-count them as "unchecked".
EXPLICIT_SRC_KEYS = {
    "consent": {"name", "eMail", "phone", "status", "paperType",
                "legacyType", "userActivityType"},
    "request": {"name", "eMail", "phone", "status"},
    "template": {"name", "language", "sub_type", "subType",
                 "template_type", "templateType"},
    "vendor": {"company_name", "name", "status"},
    "stakeholder": {"name", "login", "email"},
    "processing_activity": {"name"},
}

def _is_list_mark(v):
    return isinstance(v, tuple) and len(v) == 2 and v[0] == "__list__"


def _flatten(rec, prefix=""):
    """Flatten nested dicts to dotted keys. Lists (history/audit logs, managers,
    attachments arrays) are kept as ('__list__', length) — surfaced in coverage
    and length-compared, but not value-compared element-wise (that would need
    per-entity rules for each list's element shape)."""
    out = {}
    if isinstance(rec, dict):
        for k, v in rec.items():
            kk = f"{prefix}{k}"
            if isinstance(v, dict):
                out.update(_flatten(v, kk + "."))
            elif isinstance(v, list):
                out[kk] = ("__list__", len(v))
            else:
                out[kk] = v
    return out


def _generic_norm(v):
    """Best-effort normalizer for auto-paired fields: phone-ish -> digits,
    date-ish -> ISO date, else token."""
    if v is None:
        return ""
    s = str(v).strip()
    digits = _re.sub(r"\D", "", s)
    if len(digits) >= 7 and _re.fullmatch(r"[\d\s\-\+\(\)]+", s):
        return digits
    d = _parse_iso_or_dmy(s)
    if d:
        return d.date().isoformat()
    return _n_token(s)


def compare_record(entity, src, dst):
    """Compare a source record against its dest record across EVERY field.

    Returns (diffs, coverage):
      diffs    = [(field, src_norm, dst_norm), ...] for value mismatches
      coverage = {src_keys, dst_keys, compared_src, complex_src} flattened key sets

    Two passes: (1) explicit FIELD_MAPS for renamed/enum fields, (2) auto-pair
    every remaining source scalar to a dest scalar with the same normalized name.
    A field is only flagged when BOTH sides carry a value. Unpaired fields are
    reported in coverage, never silently ignored."""
    diffs = []
    # (1) explicit renamed/enum maps
    for label, get_s, get_d, norm in FIELD_MAPS.get(entity, []):
        try:
            sv, dv = get_s(src), get_d(dst)
        except Exception:
            continue
        if sv is None or (isinstance(sv, str) and not sv.strip()):
            continue
        if norm(sv) != norm(dv):
            diffs.append((label, norm(sv), norm(dv)))

    # (2) auto-pair everything else by normalized leaf name
    flat_src = _flatten(src)
    flat_dst = _flatten(dst)
    dst_by_norm = {_n_token(k.split(".")[-1]): (k, v) for k, v in flat_dst.items()}
    explicit = EXPLICIT_SRC_KEYS.get(entity, set())
    compared_src = set()
    for sk, sv in flat_src.items():
        base = sk.split(".")[-1]
        if base in explicit or sk in explicit:
            compared_src.add(sk)
            continue
        hit = dst_by_norm.get(_n_token(base))
        if not hit:
            continue
        _dk, dv = hit
        # list fields (audit logs / managers / attachments): compare LENGTH only
        if _is_list_mark(sv):
            if _is_list_mark(dv):
                compared_src.add(sk)
                if sv[1] != dv[1]:
                    diffs.append((f"{base}[len]", str(sv[1]), str(dv[1])))
            continue
        if sv is None or (isinstance(sv, str) and not sv.strip()) or _is_list_mark(dv):
            continue
        compared_src.add(sk)
        if _generic_norm(sv) != _generic_norm(dv):
            diffs.append((base, _generic_norm(sv), _generic_norm(dv)))

    coverage = {
        "src_keys": set(flat_src.keys()),
        "dst_keys": set(flat_dst.keys()),
        "compared_src": compared_src,
        # list fields present on source but NOT length-paired to a dest list
        "complex_src": {k for k, v in flat_src.items()
                        if _is_list_mark(v) and k not in compared_src},
    }
    return diffs, coverage


def run_field_diff(entity, sm_pairs):
    """Join migrated records source<->dest via the source-map and diff every
    mapped field. Returns a summary dict, or None when live data is unreadable
    or no field map exists for the entity."""
    if entity not in FIELD_MAPS or not FIELD_MAPS[entity]:
        return None
    src_by_id = live_source_records(entity)
    dst_records = live_dest_records(entity)
    if src_by_id is None or dst_records is None:
        return None
    id_field = FLASK_LIST_ENDPOINTS.get(entity, (None, "id"))[1]
    dst_by_id = {str(r[id_field]).strip(): r for r in dst_records
                 if r.get(id_field) is not None}

    pairs = sm_pairs.get(entity, {})
    compared = 0
    mism_records = 0
    field_counts: collections.Counter = collections.Counter()
    samples = []
    all_src: set = set()        # every source field seen (flattened)
    all_dst: set = set()        # every dest field seen
    checked_src: set = set()    # source fields actually value-compared
    complex_src: set = set()    # list/audit-log fields (not value-compared)
    for odoo_id, flask_ids in pairs.items():
        src = src_by_id.get(str(odoo_id))
        if src is None:
            continue
        for fid in flask_ids:
            dst = dst_by_id.get(str(fid))
            if dst is None:
                continue  # absence is DRIFT's job, not the field diff's
            compared += 1
            diffs, cov = compare_record(entity, src, dst)
            all_src |= cov["src_keys"]
            all_dst |= cov["dst_keys"]
            checked_src |= cov["compared_src"]
            complex_src |= cov["complex_src"]
            if diffs:
                mism_records += 1
                for label, ns, nd in diffs:
                    field_counts[label] += 1
                if len(samples) < 10:
                    samples.append((str(odoo_id), str(fid), diffs))
    unchecked_src = sorted(all_src - checked_src - complex_src)
    return {
        "compared": compared,
        "records_mismatched": mism_records,
        "field_counts": field_counts,
        "samples": samples,
        # coverage inventory: every field seen, and what was value-checked
        "src_field_total": len(all_src),
        "dst_field_total": len(all_dst),
        "checked_field_total": len(checked_src),
        "unchecked_src": unchecked_src,
        "complex_src": sorted(complex_src),
    }


def db_table_count(table: str) -> Optional[int]:
    out = _psql(f"SELECT count(*) FROM {table};")
    try:
        return int(out.strip()) if out and out.strip() else None
    except Exception:
        return None


def load_accepted_loss() -> dict:
    """{entity: [ {odoo_id, reason}, ... ]} of records intentionally not migrated."""
    if not os.path.exists(ACCEPTED_LOSS_FILE):
        return {}
    try:
        with open(ACCEPTED_LOSS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        grouped: dict = collections.defaultdict(list)
        for rec in data.get("records", []):
            grouped[rec.get("entity")].append(rec)
        return grouped
    except Exception:
        return {}


# --------------------------------------------------------------------------- #
# Entity model
# --------------------------------------------------------------------------- #
@dataclass
class EntitySpec:
    key: str
    title: str
    source_fn: Callable[[], Optional[int]]
    source_desc: str
    staged: list[tuple[str, str]]          # (label, path)
    sourcemap_key: Optional[str]           # None => migration not source-map tracked
    errors_path: Optional[str]
    db_table: Optional[str]
    raw_ids_fn: Optional[Callable[[], set]] = None  # exact source ids, for id-level diff
    notes: str = ""


@dataclass
class EntityResult:
    spec: EntitySpec
    source: Optional[int]
    staged: list[tuple[str, Optional[int]]]
    migrated: Optional[int]
    db_rows: Optional[int]
    failed: int
    fail_reasons: collections.Counter
    failed_ids: set = field(default_factory=set)
    accepted: list = field(default_factory=list)
    missing_ids: Optional[set] = None      # exact source ids that vanished (id-level diff)
    live_source: Optional[int] = None      # SOURCE re-counted from live Odoo (--live)
    live_dest_ids: Optional[set] = None    # flask ids actually present in live app (--live)
    drift_ids: Optional[set] = None        # ledger says migrated but live app lacks it
    field_diff: Optional[dict] = None      # per-field value-equality summary (--live)

    @property
    def drift(self) -> int:
        return len(self.drift_ids) if self.drift_ids else 0

    @property
    def field_mismatched(self) -> int:
        return self.field_diff.get("records_mismatched", 0) if self.field_diff else 0

    @property
    def accepted_n(self) -> int:
        return len(self.accepted)

    @property
    def _accepted_ids(self) -> set:
        out = set()
        for a in self.accepted:
            oid = a.get("odoo_id")
            try:
                out.add(int(oid))
            except (TypeError, ValueError):
                out.add(oid)
        return out

    @property
    def accepted_extra(self) -> int:
        """Accepted-loss records that are NOT already counted in `failed`."""
        return len(self._accepted_ids - self.failed_ids)

    @property
    def unaccepted_failed(self) -> int:
        """Failures the operator has NOT signed off on."""
        return max(0, self.failed - len(self._accepted_ids & self.failed_ids))

    @property
    def pct(self) -> Optional[float]:
        if not self.source or self.migrated is None:
            return None
        return round(100.0 * self.migrated / self.source, 1)

    @property
    def unexplained(self) -> Optional[int]:
        """Records that vanished without any recorded reason (the dangerous bucket).
        When an id-level diff is available it is authoritative; otherwise fall back
        to arithmetic (source - migrated - failed - accepted_extra)."""
        if self.missing_ids is not None:
            return len(self.missing_ids)
        if self.source is None or self.migrated is None:
            return None
        return self.source - self.migrated - self.failed - self.accepted_extra

    @property
    def verdict(self) -> str:
        if self.drift_ids:
            return "DRIFT"                     # ledger claims landed, live app disagrees
        if self.spec.sourcemap_key is None:
            return "UNTRACKED"
        if self.source is None or self.migrated is None:
            return "UNKNOWN"
        if self.migrated == self.source:
            return "PASS"
        if (self.unexplained or 0) > 0:
            return "GAP"                      # records vanished unexplained
        if self.unaccepted_failed == 0:
            return "PASS*"                    # shortfall fully accepted-loss
        if not self.fail_reasons_data_quality():
            return "RECOVERABLE"              # remaining failures are operational
        return "GAP"

    def fail_reasons_data_quality(self) -> bool:
        """True if any failure looks like a data problem rather than an
        operational/config limit (license, quota...)."""
        operational = ("license", "quota", "limit")
        for reason in self.fail_reasons:
            if not any(t in reason.lower() for t in operational):
                return True
        return False


def build_results() -> list[EntityResult]:
    sm = sourcemap_counts()
    sm_ids = sourcemap_ids()
    sm_pairs = sourcemap_pairs() if LIVE else {}
    accepted = load_accepted_loss()

    specs = [
        EntitySpec(
            "consent", "Consent",
            lambda: count_csv_rows(f"{RAW_DIR}/raw_consents.csv"),
            "raw_consents.csv",
            [("legacy", f"{PROC_DIR}/processed_consents_legacy.csv"),
             ("paper", f"{PROC_DIR}/processed_consents_paper.csv")],
            "consent", f"{PROC_DIR}/errors_processed_consents.csv", "consents",
            raw_ids_fn=lambda: csv_ids(f"{RAW_DIR}/raw_consents.csv"),
        ),
        EntitySpec(
            "request", "Data Principal Request",
            lambda: count_csv_rows(f"{RAW_DIR}/raw_requests.csv"),
            "raw_requests.csv",
            [("processed", f"{PROC_DIR}/processed_requests.csv")],
            "request", None, "requests",
            raw_ids_fn=lambda: csv_ids(f"{RAW_DIR}/raw_requests.csv"),
        ),
        EntitySpec(
            "vendor", "Vendor",
            lambda: count_json_list(f"{RAW_DIR}/raw_vendors.json", "vendors"),
            "raw_vendors.json",
            [("processed", f"{PROC_DIR}/processed_vendors.csv")],
            "vendor", f"{PROC_DIR}/errors_processed_vendors.csv", "vendors",
            raw_ids_fn=lambda: json_ids(f"{RAW_DIR}/raw_vendors.json", "vendors"),
        ),
        EntitySpec(
            "stakeholder", "Internal Stakeholder",
            lambda: count_json_list(f"{RAW_DIR}/raw_stakeholders.json", "stakeholders"),
            "raw_stakeholders.json",
            [("processed", f"{PROC_DIR}/processed_stakeholders.csv")],
            "stakeholder", None, None,
            raw_ids_fn=lambda: json_ids(f"{RAW_DIR}/raw_stakeholders.json", "stakeholders"),
            notes="Endpoint hardcodes user_role_type=PAManager; Odoo DPO vs PA Manager not preserved.",
        ),
        EntitySpec(
            "processing_activity", "Processing Activity",
            lambda: count_tree_nodes(f"{RAW_DIR}/raw_processing_activities.json",
                                     "processingActivities"),
            "raw_processing_activities.json (tree)",
            [("processed", f"{PROC_DIR}/processed_processing_activities.csv")],
            "processing_activity", None, "processing_activity",
            raw_ids_fn=lambda: tree_ids(f"{RAW_DIR}/raw_processing_activities.json",
                                        "processingActivities"),
            notes="Source-map tracked via /migration/source-map (loader records each created PA).",
        ),
        EntitySpec(
            "template", "Template",
            lambda: count_json_list(f"{RAW_DIR}/raw_templates.json", "data", "templates"),
            "raw_templates.json",
            [("processed (expanded rows)", f"{PROC_DIR}/processed_templates.csv")],
            "template", None, "notice_templates",
            raw_ids_fn=lambda: json_nested_ids(f"{RAW_DIR}/raw_templates.json",
                                               ("templates", "data")),
            notes="Source-map tracked; one Odoo template fans out to many rows, "
                  "each recorded under the same odoo_source_id with a distinct sub_key.",
        ),
    ]

    results = []
    for s in specs:
        failed, reasons, failed_ids = (
            error_breakdown(s.errors_path) if s.errors_path
            else (0, collections.Counter(), set())
        )
        # id-level diff: source ids that are neither migrated, failed, nor accepted.
        missing_ids = None
        if s.raw_ids_fn and s.sourcemap_key:
            raw_ids = s.raw_ids_fn()
            if raw_ids:
                landed = sm_ids.get(s.sourcemap_key, set())
                failed_str = {str(x) for x in failed_ids}
                accepted_str = {str(a.get("odoo_id")) for a in accepted.get(s.key, [])}
                missing_ids = raw_ids - landed - failed_str - accepted_str

        # --- live-API enrichment (opt-in) --- #
        live_source = drift_ids = present = field_diff = None
        source = s.source_fn()
        if LIVE:
            live_source = live_source_count(s.key)
            if live_source is not None:
                source = live_source             # live Odoo wins over the snapshot
            present = live_dest_ids(s.key)       # flask ids actually in the live app
            if present is not None and s.sourcemap_key:
                ledger_fids = set()              # flatten fan-out sets
                for fids in sm_pairs.get(s.sourcemap_key, {}).values():
                    ledger_fids |= fids
                drift_ids = ledger_fids - present  # claimed-migrated but not live
            # field-level value-equality (every mapped field, source vs dest)
            if s.sourcemap_key:
                field_diff = run_field_diff(s.sourcemap_key, sm_pairs)

        results.append(EntityResult(
            spec=s,
            source=source,
            staged=[(lbl, count_csv_rows(p)) for lbl, p in s.staged],
            migrated=sm.get(s.sourcemap_key) if s.sourcemap_key else None,
            db_rows=db_table_count(s.db_table) if s.db_table else None,
            failed=failed,
            fail_reasons=reasons,
            failed_ids=failed_ids,
            accepted=accepted.get(s.key, []),
            missing_ids=missing_ids,
            live_source=live_source,
            live_dest_ids=present,
            drift_ids=drift_ids,
            field_diff=field_diff,
        ))
    return results


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _bar(pct: Optional[float]) -> str:
    if pct is None:
        return "[" + "?" * BAR_WIDTH + "]"
    filled = int(round(BAR_WIDTH * pct / 100.0))
    return "[" + "#" * filled + "-" * (BAR_WIDTH - filled) + "]"


def _fmt(n: Optional[int]) -> str:
    return "  n/a" if n is None else f"{n:>5}"


VERDICT_GLYPH = {
    "PASS": "+", "PASS*": "+", "RECOVERABLE": "~", "GAP": "x", "DRIFT": "!",
    "UNTRACKED": "?", "UNKNOWN": "?",
}


def render(results: list[EntityResult]) -> str:
    L: list[str] = []
    line = "=" * 78
    L.append(line)
    L.append("  ODOO -> FLASK MIGRATION : RECONCILIATION AUDIT".ljust(78))
    L.append(f"  generated {datetime.now():%Y-%m-%d %H:%M:%S}  |  source-map = live Postgres truth")
    if LIVE:
        L.append("  mode: LIVE  |  SOURCE=live Odoo  |  MIGRATED verified against live Flask app")
    L.append(line)
    L.append("")

    # ---- summary table ----
    L.append("SUMMARY  (migrated / source)")
    L.append("-" * 78)
    L.append(f"  {'entity':22} {'src':>5} {'migr':>5} {'fail':>5} {'acc':>4} {'unexp':>6} {'%':>7}  verdict")
    tracked_src = tracked_mig = 0
    for r in results:
        unexp = r.unexplained
        pct = r.pct
        L.append(
            f"  {r.spec.title[:22]:22} {_fmt(r.source)} {_fmt(r.migrated)}"
            f" {r.failed:>5} {r.accepted_n:>4}"
            f" {('   n/a' if unexp is None else f'{unexp:>6}')}"
            f" {('    n/a' if pct is None else f'{pct:>6.1f}%')}"
            f"  {VERDICT_GLYPH.get(r.verdict, ' ')} {r.verdict}"
        )
        if r.spec.sourcemap_key and r.source and r.migrated is not None:
            tracked_src += r.source
            tracked_mig += r.migrated
    L.append("-" * 78)
    overall = round(100.0 * tracked_mig / tracked_src, 1) if tracked_src else 0.0
    L.append(f"  TRACKED ENTITIES OVERALL: {tracked_mig}/{tracked_src} = {overall}%   {_bar(overall)}")
    L.append("")
    L.append("  legend: src=Odoo source  migr=landed in Flask  fail=rejected  "
             "acc=accepted-loss  unexp=unexplained")
    L.append("          OK pass | OK* covered by accepted-loss | FIX recoverable "
             "(operational) | GAP investigate | ?? not tracked")
    L.append("")

    # ---- per-entity detail ----
    L.append("=" * 78)
    L.append("PER-ENTITY DETAIL")
    L.append("=" * 78)
    for r in results:
        L.append("")
        L.append(f"### {r.spec.title}  [{r.verdict}]   {_bar(r.pct)} "
                 f"{'' if r.pct is None else str(r.pct) + '%'}")
        L.append(f"    source        : {_fmt(r.source).strip()}  ({r.spec.source_desc})")
        for lbl, n in r.staged:
            L.append(f"    staged[{lbl}]  : {_fmt(n).strip()}")
        if r.spec.sourcemap_key:
            L.append(f"    migrated      : {_fmt(r.migrated).strip()}  "
                     f"(migration_source_map['{r.spec.sourcemap_key}'])")
        else:
            L.append(f"    migrated      : not source-map tracked")
        if r.db_rows is not None:
            L.append(f"    flask db rows : {r.db_rows}  ({r.spec.db_table})")
        if LIVE:
            if r.live_source is not None:
                L.append(f"    live source   : {r.live_source}  (live Odoo re-count)")
            if r.live_dest_ids is not None:
                L.append(f"    live in app   : {len(r.live_dest_ids)}  "
                         f"(GET {FLASK_LIST_ENDPOINTS.get(r.spec.key, ('n/a',))[0]})")
            elif r.spec.key in FLASK_LIST_ENDPOINTS:
                L.append("    live in app   : n/a  (live read failed -> not counted as success)")
        if r.drift_ids:
            L.append(f"    !! DRIFT      : {r.drift} ledger-migrated record(s) NOT in live app "
                     f"-> ledger lies / record deleted")
            shown = sorted(r.drift_ids, key=lambda x: (len(x), x))[:40]
            L.append(f"        missing flask ids: {', '.join(shown)}"
                     + (" ..." if r.drift > 40 else ""))
        if r.field_diff is not None:
            fd = r.field_diff
            L.append(f"    field check   : {fd['compared']} record(s) compared")
            # coverage inventory: every field seen, and how many value-checked,
            # so anything NOT compared is visible (never silently skipped).
            L.append(f"        coverage   : source has {fd['src_field_total']} field(s), "
                     f"value-checked {fd['checked_field_total']}; "
                     f"dest has {fd['dst_field_total']} field(s)")
            if fd["unchecked_src"]:
                shown = ", ".join(fd["unchecked_src"][:30])
                L.append(f"        unchecked source fields ({len(fd['unchecked_src'])}): "
                         f"{shown}" + (" ..." if len(fd['unchecked_src']) > 30 else ""))
                L.append("            ^ no same-named dest field; add a FIELD_MAPS rule to value-check")
            if fd["complex_src"]:
                shown = ", ".join(fd["complex_src"][:20])
                L.append(f"        complex/list fields (audit logs, managers, attachments) "
                         f"not value-checked: {shown}")
            if fd["records_mismatched"]:
                L.append(f"        !! {fd['records_mismatched']} record(s) differ "
                         f"source vs live app:")
                for label, n in fd["field_counts"].most_common():
                    L.append(f"           - {n:>4} x  field '{label}' mismatched")
                for oid, fid, diffs in fd["samples"][:5]:
                    parts = "; ".join(f"{lbl}: '{s}' != '{d}'" for lbl, s, d in diffs)
                    L.append(f"           e.g. odoo#{oid}->flask#{fid}: {parts}")
            else:
                L.append("        all value-checked fields match")
        if r.failed:
            L.append(f"    failed        : {r.failed}")
            for reason, n in r.fail_reasons.most_common():
                L.append(f"        - {n:>4} x  {reason}")
        if r.accepted:
            L.append(f"    accepted loss : {r.accepted_n}")
            for a in r.accepted:
                L.append(f"        - odoo#{a.get('odoo_id')}  {a.get('reason')}")
        if r.unexplained and r.unexplained > 0:
            L.append(f"    !! UNEXPLAINED: {r.unexplained}  (in source, but neither migrated, "
                     f"failed, nor accepted) -> INVESTIGATE")
            if r.missing_ids:
                shown = sorted(r.missing_ids, key=lambda x: (len(x), x))[:40]
                L.append(f"        missing odoo ids: {', '.join(shown)}"
                         + (" ..." if len(r.missing_ids) > 40 else ""))
        if r.spec.notes:
            L.append(f"    note          : {r.spec.notes}")

    # ---- remaining requirements ----
    L.append("")
    L.append("=" * 78)
    L.append("REMAINING REQUIREMENTS  (ranked)")
    L.append("=" * 78)
    for i, item in enumerate(derive_requirements(results), 1):
        L.append(f"  {i}. [{item['sev']}] {item['title']}")
        L.append(f"        {item['detail']}")

    # ---- methodology ----
    L.append("")
    L.append("=" * 78)
    L.append("METHODOLOGY / LIMITATIONS")
    L.append("=" * 78)
    L.append("  - 'migrated' = DISTINCT odoo_source_ids in live migration_source_map")
    L.append("    (idempotency ledger), the only authoritative 'landed in Flask' signal.")
    L.append("    All six entities are now tracked; template maps one source to many")
    L.append("    Flask rows (distinct sub_key each), so its migrated count is distinct")
    L.append("    sources, not emitted rows.")
    L.append("  - 'failed' counts come from data/processed/errors_*.csv (latest load run).")
    L.append("  - 'accepted loss' is the operator-maintained data/accepted_loss.json.")
    L.append("  - Any read failure renders as n/a rather than aborting the audit.")
    L.append("")
    return "\n".join(L)


def derive_requirements(results: list[EntityResult]) -> list[dict]:
    """Turn the numbers into a ranked action list (the 'what's left' section)."""
    reqs: list[dict] = []
    by_key = {r.spec.key: r for r in results}

    c = by_key.get("consent")
    if c and c.failed:
        lic = sum(n for reason, n in c.fail_reasons.items() if "license" in reason.lower())
        if lic:
            reqs.append({
                "sev": "HIGH", "title": f"Recover {lic} license-blocked consents",
                "detail": "Failures are 'No active license available' (operational, not data). "
                          "Add license capacity then re-run `consent load`; they should land.",
            })

    for r in results:
        if r.field_diff and r.field_diff.get("records_mismatched"):
            fc = r.field_diff["field_counts"]
            top = ", ".join(f"{lbl}({n})" for lbl, n in fc.most_common(6))
            reqs.append({
                "sev": "HIGH",
                "title": f"Reconcile {r.field_diff['records_mismatched']} {r.spec.title} "
                         f"record(s) with field-level mismatches",
                "detail": f"Migrated rows whose live Flask values differ from the Odoo source. "
                          f"Most-affected fields: {top}. Inspect the per-entity samples; if a "
                          f"field is a known transform mapping, extend reconcile's FIELD_MAPS "
                          f"normalizer rather than re-migrating.",
            })

    for r in results:
        if r.drift_ids:
            ids = ", ".join(sorted(r.drift_ids, key=lambda x: (len(x), x))[:40])
            reqs.append({
                "sev": "HIGH", "title": f"Resolve {r.drift} drifted {r.spec.title} record(s)",
                "detail": f"migration_source_map claims these landed but the live Flask app "
                          f"returns no such record -> ledger is stale or rows were deleted "
                          f"post-load. flask ids: {ids}",
            })

    for r in results:
        if r.unexplained and r.unexplained > 0:
            ids = ""
            if r.missing_ids:
                ids = " odoo ids: " + ", ".join(
                    sorted(r.missing_ids, key=lambda x: (len(x), x))[:40])
            reqs.append({
                "sev": "HIGH", "title": f"Investigate {r.unexplained} dropped {r.spec.title} record(s)",
                "detail": f"In source but neither migrated, failed, nor accepted -> silently "
                          f"dropped at load.{ids}",
            })

    for r in results:
        if r.spec.sourcemap_key is None and r.spec.key in ("processing_activity", "template"):
            reqs.append({
                "sev": "MED", "title": f"Add source-map tracking for {r.spec.title}",
                "detail": "Not recorded in migration_source_map -> re-runs are not idempotent and "
                          "completion cannot be audited per-record. Mirror the vendor/stakeholder pattern.",
            })
        elif r.spec.sourcemap_key and r.migrated is None and r.spec.key in ("processing_activity", "template"):
            reqs.append({
                "sev": "MED", "title": f"Run a {r.spec.title} load to populate the new ledger",
                "detail": "Tracking is wired (loader -> /migration/source-map) but the ledger has no "
                          f"rows yet for {r.spec.key}. Re-run the load so existing records backfill.",
            })

    s = by_key.get("stakeholder")
    if s:
        reqs.append({
            "sev": "MED", "title": "Decide DPO vs PA Manager role fidelity",
            "detail": "All stakeholders land as user_role_type=PAManager; Odoo DPO distinction is lost. "
                      "If DPO must persist, backfill via /stakeholder/<id>/update-roles.",
        })

    reqs.append({
        "sev": "LOW", "title": "Rotate shared secrets",
        "detail": "Odoo JWT + Flask API key were exposed during debugging; rotate and confirm "
                  "config/.env stays gitignored.",
    })
    return reqs


# --------------------------------------------------------------------------- #
# Public entry + self-test
# --------------------------------------------------------------------------- #
def run_reconciliation(write: bool = True) -> str:
    results = build_results()
    report = render(results)
    if write:
        os.makedirs(PROC_DIR, exist_ok=True)
        with open(REPORT_PATH, "w", encoding="utf-8") as f:
            f.write(report)
    return report


def self_test() -> list[str]:
    """Internal consistency checks. Returns list of failure strings (empty = ok)."""
    fails: list[str] = []
    results = build_results()
    if not results:
        return ["build_results() returned nothing"]
    for r in results:
        if r.source is not None and r.migrated is not None:
            if r.migrated > r.source:
                fails.append(f"{r.spec.key}: migrated {r.migrated} > source {r.source}")
            if r.pct is not None and not (0 <= r.pct <= 100):
                fails.append(f"{r.spec.key}: pct {r.pct} out of range")
    # ledger identity must hold where computable (offline only: live mode swaps
    # SOURCE for the live Odoo count, which won't match file-derived missing_ids)
    for r in (results if not LIVE else []):
        if r.unexplained is not None and r.source is not None and r.migrated is not None:
            lhs = r.source
            rhs = r.migrated + r.failed + r.accepted_extra + r.unexplained
            if lhs != rhs:
                fails.append(f"{r.spec.key}: ledger identity broken {lhs} != {rhs}")
    # render must not raise and must be non-trivial
    rep = render(results)
    if len(rep) < 200 or "RECONCILIATION AUDIT" not in rep:
        fails.append("render() produced a trivial/invalid report")
    return fails


if __name__ == "__main__":
    import sys
    if "--live" in sys.argv:
        LIVE = True
        if not (FLASK_API_BASE_URL and FLASK_API_KEY):
            print("WARN: --live set but FLASK_API_BASE_URL / FLASK_API_KEY missing; "
                  "dest verification will read n/a.", file=sys.stderr)
    if "--self-test" in sys.argv:
        problems = self_test()
        print("SELF-TEST:", "PASS" if not problems else "FAIL")
        for p in problems:
            print("  -", p)
        sys.exit(1 if problems else 0)
    print(run_reconciliation())
    print(f"\n[written] {REPORT_PATH}")
