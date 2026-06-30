import requests
import pandas as pd
import os
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict, Any
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

load_dotenv("config/.env")

ODOO_BASE_URL = os.getenv("ODOO_BASE_URL", "https://tool.dpdp-portal.dpdpconsultants.com/api")
ODOO_JWT_TOKEN = os.getenv("ODOO_JWT_TOKEN")
ODOO_SESSION_ID = os.getenv("ODOO_SESSION_ID")
BATCH_SIZE = int(os.getenv("BATCH_SIZE", 50))
MAX_RECORDS = int(os.getenv("MAX_RECORDS", 0))  # 0 means no limit
DATA_RAW_DIR = os.getenv("DATA_RAW_DIR", "data/raw")
# Concurrency for the N+1 by-id enrichment fan-out. Each enrich step makes one
# HTTP call per record (100k records -> 100k calls); running them sequentially
# is the migration's dominant cost. Pure I/O wait, so a thread pool gives a
# near-linear speedup. Start conservative (server rate-limit / WAF); raise if
# the Odoo side tolerates it.
ENRICH_WORKERS = int(os.getenv("ENRICH_WORKERS", 16))

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
        # One pooled, keep-alive session reused for every call. Previously each
        # requests.get() opened a fresh TLS connection — at 100k by-id calls the
        # repeated handshakes dominate. pool_maxsize must cover ENRICH_WORKERS so
        # concurrent threads don't serialize on a too-small connection pool.
        # Retry/backoff absorbs transient 429/5xx (incl. rate-limit pushback when
        # threads ramp up). Auth (Authorization/cookies) lives on the session.
        pool = max(ENRICH_WORKERS, 32)
        retry = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=frozenset(["GET"]),
        )
        adapter = HTTPAdapter(pool_connections=pool, pool_maxsize=pool, max_retries=retry)
        self.session = requests.Session()
        self.session.headers.update(self.headers)
        self.session.cookies.update(self.cookies)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def _fetch_page(self, endpoint: str, page: int, filters: Dict[str, Any] = None):
        """Fetch a single dashboard page. Returns (records, total_pages, ok).

        `total_pages` is the `pagination.total_page` value when the endpoint
        provides it (else None). `ok` is False on HTTP error, the HTTP-200 auth
        error envelope, or an exception — callers abort rather than treat it as
        an empty page.
        """
        params = {"page_no": page, "rec_limit": BATCH_SIZE}
        if filters:
            params.update(filters)
        try:
            response = self.session.get(f"{self.base_url}{endpoint}", params=params, timeout=30)
            if response.status_code != 200:
                logger.error(f"Failed to fetch page {page}: {response.status_code} - {response.text}")
                return [], None, False

            data = response.json()
            # Odoo wraps auth errors in an HTTP-200 envelope, e.g.
            # {"message": "Token Expired", "status_code": 401}. Abort loudly
            # instead of treating it as an empty page.
            if isinstance(data, dict) and data.get("status_code", 200) != 200:
                logger.error(
                    f"API error envelope on page {page}: {data.get('status_code')} - "
                    f"{data.get('message')}. Refresh ODOO_JWT_TOKEN in config/.env."
                )
                return [], None, False

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

            pagination = data.get("pagination", {}) if isinstance(data, dict) else {}
            return records or [], pagination.get("total_page"), True
        except Exception as e:
            logger.exception(f"Exception occurred during extraction (page {page}): {e}")
            return [], None, False

    def fetch_records(self, endpoint: str, filters: Dict[str, Any] = None) -> List[Dict[str, Any]]:
        """Fetch all records from a paginated Odoo dashboard endpoint.

        Page 1 is fetched first to learn `pagination.total_page`; the remaining
        pages are then fetched concurrently (ENRICH_WORKERS) when the total is
        known. Falls back to sequential paging when the endpoint omits
        total_page, or when a MAX_RECORDS test-limit is set (a parallel fan-out
        can't honor an early stop cleanly).
        """
        logger.info(f"Fetching page 1 for {endpoint}...")
        first, total_pages, ok = self._fetch_page(endpoint, 1, filters)
        if not ok:
            logger.error("Aborting extraction (page 1 failed).")
            return []
        if not first:
            logger.info("No records found.")
            return []
        all_records = list(first)
        logger.info(f"Successfully fetched {len(first)} records from page 1.")

        # Sequential fallback: unknown page count, or a MAX_RECORDS test-limit.
        if total_pages is None or (MAX_RECORDS and MAX_RECORDS > 0):
            return self._paginate_sequential(endpoint, filters, all_records, total_pages)

        if total_pages <= 1:
            return all_records

        pages = list(range(2, total_pages + 1))
        logger.info(f"{endpoint}: {total_pages} pages total; fetching 2..{total_pages} "
                    f"with {ENRICH_WORKERS} workers...")
        with ThreadPoolExecutor(max_workers=ENRICH_WORKERS) as ex:
            results = list(ex.map(lambda p: self._fetch_page(endpoint, p, filters), pages))

        # Fail-fast on incompleteness: a silently-dropped page would yield a short
        # dataset that only reconcile catches later. Retry any failed pages once
        # (the session's own retry/backoff already covers transient blips), then
        # abort loudly if any still fail — never return a known-incomplete pull.
        page_records = {}
        failed = []
        for page, (records, _tp, page_ok) in zip(pages, results):
            if page_ok:
                page_records[page] = records
            else:
                failed.append(page)
        if failed:
            logger.warning(f"{endpoint}: {len(failed)} page(s) failed; retrying once: {failed}")
            for page in failed:
                records, _tp, page_ok = self._fetch_page(endpoint, page, filters)
                if page_ok:
                    page_records[page] = records
                else:
                    raise RuntimeError(
                        f"Extraction ABORTED: {endpoint} page {page} failed twice. "
                        f"Refusing to return an incomplete dataset (check ODOO_JWT_TOKEN / rate limits)."
                    )
        for page in pages:                      # extend in page order (determinism)
            all_records.extend(page_records[page])
        logger.info(f"Fetched {len(all_records)} total records from {endpoint}.")
        return all_records

    def _paginate_sequential(self, endpoint, filters, all_records, total_pages, start_page=2):
        """Sequential page walk from `start_page`, continuing into `all_records`
        (which already holds page 1). Used when total_page is unknown (stop on a
        short page) or a MAX_RECORDS limit is active (stop once reached)."""
        page = start_page
        while True:
            if MAX_RECORDS and MAX_RECORDS > 0 and len(all_records) >= MAX_RECORDS:
                logger.info(f"Reached MAX_RECORDS limit of {MAX_RECORDS}. Stopping extraction.")
                return all_records[:MAX_RECORDS]

            logger.info(f"Fetching page {page} for {endpoint}...")
            records, tp, ok = self._fetch_page(endpoint, page, filters)
            if not ok:
                break
            if not records:
                logger.info("No more records found.")
                break
            all_records.extend(records)
            logger.info(f"Successfully fetched {len(records)} records from page {page}.")

            if tp is not None:
                total_pages = tp
            if total_pages is not None:
                if page >= total_pages:
                    break
            elif len(records) < BATCH_SIZE:
                break
            page += 1

        if MAX_RECORDS and MAX_RECORDS > 0:
            return all_records[:MAX_RECORDS]
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
            response = self.session.get(
                url,
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
            response = self.session.get(
                url,
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


def run_request_type_extraction(output_file: str = "raw_request_types.json"):
    """Extract all request types from Odoo (/request-types) and save the
    `requestTypes` array as JSON. Source for the Flask request_types seed that
    must exist before consents/requests load."""
    extractor = OdooExtractor(ODOO_BASE_URL, ODOO_JWT_TOKEN, ODOO_SESSION_ID)
    raw = extractor.fetch_simple("/request-types")
    if not raw:
        logger.error("Request-type extraction returned empty response.")
        return
    records = raw.get("requestTypes", []) if isinstance(raw, dict) else raw
    if not records:
        logger.error("Request-type response had no 'requestTypes' array.")
        return
    extractor.save_to_json(records, output_file)


def run_vendor_extraction(output_file: str = "raw_vendors.json"):
    """Extract all vendors from Odoo (/vendors_details) and save as JSON."""
    extractor = OdooExtractor(ODOO_BASE_URL, ODOO_JWT_TOKEN, ODOO_SESSION_ID)
    raw = extractor.fetch_simple("/vendors_details")
    if raw:
        extractor.save_to_json(raw, output_file)
    else:
        logger.error("Vendor extraction returned empty response.")

def run_stakeholder_extraction(output_file: str = "raw_stakeholders.json"):
    """Extract all internal stakeholders from Odoo (GET /stakeholders) and save
    as JSON. The endpoint returns the full set in one shot (no pagination):

        {"status": "success", "recordType": "Internal Stakeholders",
         "totalInternalStakeholders": N, "stakeholders": [ {...} ]}

    Saved verbatim so the transform owns all field mapping/role flattening.
    """
    extractor = OdooExtractor(ODOO_BASE_URL, ODOO_JWT_TOKEN, ODOO_SESSION_ID)
    raw = extractor.fetch_simple("/stakeholders")
    if raw:
        extractor.save_to_json(raw, output_file)
    else:
        logger.error("Stakeholder extraction returned empty response.")


def _enrich_ids(extractor, endpoint, result_key, ids, checkpoint_path, label):
    """Fetch one by-id record per id, concurrently, with on-disk checkpointing.

    Returns {id: rec_dict} covering every id in `ids` (missing/failed -> {}).

    Resume: the checkpoint is a JSONL file (one `{"id":.., "rec":..}` per line).
    On entry, ids already in the file are loaded and skipped, so a run killed at
    record 90k (token expiry, network drop, crash) resumes instead of re-fetching
    from zero. ex.map preserves submission order, so the main thread owns every
    file write — no lock needed. The caller removes the checkpoint only after the
    enriched CSV is safely written.
    """
    cache = {}
    if os.path.exists(checkpoint_path):
        with open(checkpoint_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    cache[str(row["id"])] = row.get("rec") or {}
                except (ValueError, KeyError):
                    continue  # tolerate a half-written final line from a hard kill
        if cache:
            logger.info(f"Resuming {label}: {len(cache)}/{len(ids)} already cached "
                        f"in {checkpoint_path}")

    missing = [rid for rid in ids if str(rid) not in cache]
    if not missing:
        logger.info(f"{label}: all {len(ids)} ids already cached; skipping fetch.")
    else:
        total = len(missing)
        done = 0
        # Append mode: each completed record is flushed so a crash loses at most
        # the in-flight batch. Workers only do HTTP; this loop does all writes.
        with open(checkpoint_path, "a", encoding="utf-8") as fh, \
             ThreadPoolExecutor(max_workers=ENRICH_WORKERS) as ex:
            for rid, rec in zip(missing, ex.map(
                lambda r: extractor.fetch_by_id(endpoint, r, result_key=result_key), missing)):
                rec = rec or {}
                cache[str(rid)] = rec
                fh.write(json.dumps({"id": rid, "rec": rec}) + "\n")
                done += 1
                if done % 500 == 0:
                    fh.flush()
                    logger.info(f"Enriched {done}/{total} {label} "
                                f"(cached {len(cache)}/{len(ids)})...")
    return {rid: cache.get(str(rid), {}) for rid in ids}


def run_request_enrichment(raw_file: str = "raw_requests.csv"):
    """Enrich the dashboard request CSV with the per-record by-id fields the
    dashboard omits — chiefly `requestType [id, name]`, the assignee carried in
    `trackAssigneeStatus`, the internal allottee in `assignToDM`, and the vendor
    handling the request in `assignToVendor` (the vendor<->request "activity"
    linkage). Adds columns in place.

    `/dpgr/dashboard` has no request type / assignee / vendor link at all;
    `GET /dpgr/id?id=<N>` carries all of them.
    """
    path = os.path.join(DATA_RAW_DIR, raw_file)
    if not os.path.exists(path):
        logger.error(f"Cannot enrich, raw file not found: {path}")
        return
    # phone as string: this function rewrites the CSV (df.to_csv below), so a
    # float-inferred phone ('0025520778' -> 25520778.0) would be persisted back
    # into the raw file and lose its leading zeros before transform ever runs.
    df = pd.read_csv(path, dtype={"phone": str})
    if "id" not in df.columns:
        logger.error("Request CSV has no 'id' column; cannot enrich by id.")
        return

    # by-id-only fields the dashboard omits. Collected per-record and written
    # as new columns the transform reads.
    byid_cols = ("dpComment", "escalatedComment", "escalatedDate",
                 "closingComment", "withdrawalComment", "iPAddress",
                 "deviceType", "resolutionDate", "closedOn", "actionDate")

    extractor = OdooExtractor(ODOO_BASE_URL, ODOO_JWT_TOKEN, ODOO_SESSION_ID)
    ids = df["id"].tolist()

    # Parallel I/O fan-out with resume: one GET /dpgr/id per record. Returns
    # {id: rec} for every id; recs[i] lines up with df row i. Column assembly
    # below stays single-threaded.
    checkpoint = os.path.join(DATA_RAW_DIR, raw_file + ".enrich.jsonl")
    rec_by_id = _enrich_ids(extractor, "/dpgr/id", "dpgr", ids, checkpoint, "requests")
    recs = [rec_by_id[rid] for rid in ids]

    request_types, assignee_emails, consents = [], [], []
    # New: vendor<->request activity link + real internal allottee. Both arrive
    # as `[ {id, name} ]` arrays on /dpgr/id; carried verbatim as JSON so the
    # transform owns the id/name extraction (mirrors the `consent` column).
    assign_to_vendors, assign_to_dms = [], []
    assignee_statuses, assignee_raised_ons = [], []
    extra = {col: [] for col in byid_cols}
    for rec in recs:
        request_types.append(json.dumps(rec.get("requestType")) if rec.get("requestType") else "")
        track = rec.get("trackAssigneeStatus") or []
        first = track[0] if track and isinstance(track[0], dict) else {}
        assignee_emails.append(first.get("email") or "")
        # Real internal assignee identity (status + when) — previously dropped, so
        # every migrated request lost "who it was allotted to". `assignedTo,` is
        # the (typo'd) Odoo key carrying `[id, name]`.
        assignee_statuses.append(first.get("status") or "")
        assignee_raised_ons.append(first.get("raisedOn") or "")
        # Linked consent(s) for revoke requests -> the transform extracts the ids.
        consents.append(json.dumps(rec.get("consent")) if rec.get("consent") else "")
        # Vendor handling the request + internal allottee (Data Manager).
        assign_to_vendors.append(json.dumps(rec.get("assignToVendor")) if rec.get("assignToVendor") else "")
        assign_to_dms.append(json.dumps(rec.get("assignToDM")) if rec.get("assignToDM") else "")
        for col in byid_cols:
            extra[col].append(rec.get(col) or "")

    df["requestType"] = request_types
    df["assignee_email"] = assignee_emails
    df["assignee_status"] = assignee_statuses
    df["assignee_raised_on"] = assignee_raised_ons
    df["consent"] = consents
    df["assignToVendor"] = assign_to_vendors
    df["assignToDM"] = assign_to_dms
    for col in byid_cols:
        df[col] = extra[col]
    df.to_csv(path, index=False)
    # CSV is safely written -> drop the resume checkpoint.
    if os.path.exists(checkpoint):
        os.remove(checkpoint)
    filled = sum(1 for x in request_types if x)
    vfilled = sum(1 for x in assign_to_vendors if x)
    logger.info(f"Request enrichment done: {filled}/{len(df)} rows carry requestType, "
                f"{vfilled}/{len(df)} carry a vendor link. Updated {path}")


def run_consent_enrichment(raw_file: str = "raw_consents.csv"):
    """Backfill consent type fields from `GET /dpcm/id?id=<N>` where the
    dashboard left them blank (notably `userActivityType`). The by-id endpoint
    uses different key names (`digitalPaper`/`legacyLive`) — mapped back onto the
    dashboard column names the transform already reads."""
    path = os.path.join(DATA_RAW_DIR, raw_file)
    if not os.path.exists(path):
        logger.error(f"Cannot enrich, raw file not found: {path}")
        return
    # phone as string (this function also rewrites the CSV) — keep leading zeros.
    df = pd.read_csv(path, dtype={"phone": str})
    if "id" not in df.columns:
        logger.error("Consent CSV has no 'id' column; cannot enrich by id.")
        return

    for col in ("userActivityType", "paperType", "legacyType", "template",
                "artifactId", "iPAddress", "deviceType", "dpgrRequestNo",
                "closedOn", "createdOn", "lastUpdatedOn"):
        if col not in df.columns:
            df[col] = ""

    extractor = OdooExtractor(ODOO_BASE_URL, ODOO_JWT_TOKEN, ODOO_SESSION_ID)
    ids = df["id"].tolist()

    # Parallel I/O fan-out with resume: one GET /dpcm/id per record. Returns
    # {id: rec}; the df.loc mutations below stay single-threaded (pandas writes
    # are not thread-safe).
    checkpoint = os.path.join(DATA_RAW_DIR, raw_file + ".enrich.jsonl")
    rec_by_id = _enrich_ids(extractor, "/dpcm/id", "dpcm", ids, checkpoint, "consents")

    filled = 0
    for rec_id in ids:
        rec = rec_by_id.get(rec_id)
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

    df.to_csv(path, index=False)
    # CSV is safely written -> drop the resume checkpoint.
    if os.path.exists(checkpoint):
        os.remove(checkpoint)
    logger.info(f"Consent enrichment done: backfilled userActivityType on {filled} rows. Updated {path}")


if __name__ == "__main__":
    pass
