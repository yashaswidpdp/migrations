# 02 — Extract: `scripts/extract/extract_odoo.py`

The extract layer pulls raw data from Odoo's REST API and saves it under
`data/raw/`. All Odoo calls are **GET** (Odoo is never mutated). The core is the
`OdooExtractor` class plus a set of `run_*` entrypoints the CLI calls.

## `OdooExtractor` — the HTTP client

Constructed with `(base_url, jwt_token, session_id)`. Auth uses **both** a JWT
bearer header and a `session_id` cookie (some Odoo dashboards require the cookie).

### Pooled keep-alive session (`__init__`)
A single `requests.Session` is built with an `HTTPAdapter` whose pool is sized to
`max(ENRICH_WORKERS, 32)`, plus a `Retry(total=3, backoff_factor=0.5,
status_forcelist=[429,500,502,503,504])`. Auth lives on the session. **Why:** the
old code opened a fresh TLS connection per call; at 100k by-id calls the repeated
handshakes dominated. Keep-alive + a pool big enough for every worker thread
removes that, and retry/backoff absorbs transient rate-limit pushback.

## The two cost centers (and how they were made fast)

Extraction has two HTTP phases. At 15k–100k records the second dominated; a naive
run took ~3 hours. Three optimizations cut it to ~10 minutes:

### 1. Dashboard pagination — `fetch_records()` (parallel)
- `_fetch_page(endpoint, page, filters)` fetches one page, returning
  `(records, total_pages, ok)`. It also catches the **HTTP-200 auth-error
  envelope** (`status_code != 200`) and returns `ok=False` so the caller aborts
  rather than treating expiry as an empty page.
- `fetch_records()` fetches **page 1 first** to learn `pagination.total_page`, then
  fetches pages `2..N` **concurrently** through a `ThreadPoolExecutor`
  (`ENRICH_WORKERS`). `ex.map` preserves order.
- **Sequential fallback** (`_paginate_sequential`) is used when the endpoint omits
  `total_page`, or when `MAX_RECORDS > 0` (a parallel fan-out can't honor an early
  stop cleanly). It stops on a short page or once `MAX_RECORDS` is reached.

### 2. By-id enrichment — `_enrich_ids()` (parallel + resumable)
The dashboard omits some fields (e.g. consent `userActivityType`, `template`,
`artifactId`; request `requestType`, assignee, vendor link), so each record needs a
second call: `GET /dpcm/id?id=N` or `GET /dpgr/id?id=N`. That's the N+1 bottleneck.

`_enrich_ids(extractor, endpoint, result_key, ids, checkpoint_path, label)`:
- Fans the by-id calls out across `ENRICH_WORKERS` threads (`ex.map` → order
  preserved, so `recs[i]` lines up with row `i`).
- **Resumable checkpoint:** writes a JSONL file next to the raw CSV
  (`raw_consents.csv.enrich.jsonl`), one `{"id":.., "rec":..}` per completed record,
  flushed every 500. On restart it loads the checkpoint and **skips already-fetched
  ids** — a run killed at 90k resumes from 90k. It tolerates a half-written final
  line. The caller deletes the checkpoint only **after** the enriched CSV is
  written, so an interrupted run always leaves it for resume.
- Because `ex.map` preserves submission order, the **main thread owns all file
  writes** → no lock needed.

The two enrichment entrypoints `run_consent_enrichment()` / `run_request_enrichment()`
call `_enrich_ids`, then assemble the new columns single-threaded and rewrite the
raw CSV in place.

## Other fetch helpers

| Method | Use |
|---|---|
| `fetch_simple(endpoint)` | one-shot, non-paginated GET (PAs, templates, request-types, vendors, stakeholders) |
| `fetch_by_id(endpoint, id, result_key)` | a single by-id record (used by the enrichers) |
| `save_to_csv` / `save_to_json` | persist to `data/raw/` |

## `run_*` entrypoints (called by the CLI)

| Function | Output |
|---|---|
| `run_extraction(endpoint, file)` | paginated dashboard → CSV (consents `/dpcm/dashboard`, requests `/dpgr/dashboard`) |
| `run_pa_extraction` | `/processing_activities` tree → JSON |
| `run_template_extraction` | `/v2/get/templates` → JSON |
| `run_request_type_extraction` | `/request-types` → JSON (`requestTypes` array) |
| `run_vendor_extraction` | `/vendors_details` → JSON |
| `run_stakeholder_extraction` | `/stakeholders` → JSON |
| `run_request_enrichment` / `run_consent_enrichment` | backfill by-id fields into the raw CSV |

## Gotchas handled here

- **`phone` as string** — the enrichers read CSVs with `dtype={"phone": str}` so a
  blank-bearing phone column isn't inferred as float (`'0025...'` → `25...0`),
  preserving leading zeros and avoiding a trailing `.0`.
- **Auth-error envelope** — every fetcher guards the HTTP-200 `status_code != 200`
  case and aborts loudly (never saves a bogus empty dataset).

See `09-performance-and-resume.md` for the end-to-end performance story and the
`ENRICH_WORKERS`/`MAX_RECORDS` interplay.
