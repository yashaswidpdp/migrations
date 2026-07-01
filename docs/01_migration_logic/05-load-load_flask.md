# 05 — Load: `scripts/load/load_flask.py`

The load layer reads `data/processed/*` and POSTs each row to the Flask migration
endpoints. The core is the `FlaskLoader` class plus `run_*` entrypoints. Every
loader is **idempotent**: an already-migrated row returns HTTP 409 and is counted
as success/skip; genuine failures are written to `data/processed/errors_<file>`.

## `FlaskLoader.__init__`
- Builds `headers` with the `Authorization` bearer and, if `FLASK_TENANT_DOMAIN`
  is set, a `Host` header so Flask resolves the right tenant.
- Builds a **pooled keep-alive `requests.Session`** (pool ≥ `LOAD_WORKERS`) with
  **`max_retries=0`** — a POST that times out *after* the server committed must
  never be auto-resent (it would double-insert). Failures are captured per row and
  re-run idempotently instead.

## Per-entity loaders

| Method | Endpoint | Notes |
|---|---|---|
| `load_consents_via_migration` | `/migration/consent` | paper + legacy together; parallel, principal-sharded |
| `load_from_csv` | `/migration/request` | requests; parallel, principal+vendor-sharded |
| `load_vendors` | `/migration/vendor` | decodes NDA/VRA attachments into inline Base64 |
| `load_stakeholders` | `/migration/stakeholder` | role names mapped via `StakeholderRoleMapper`; writes a report |
| `load_pa_*` / `load_templates` / `approve_templates` / `patch_*` | native routes + `/migration/source-map` | PA & template create via the real routes, then ledger them |
| `load_request_types` / `seed_request_types` | `/request-types` | idempotent by name |

## Parallel, principal-sharded writes (the speed work)

A load is a **write** with shared server-side state, so it can't fan out blindly:
many consents/requests resolve to the **same data principal**, and concurrent
writes for one principal would double-create it. The loader **shards** instead.

### `_shard_by_keys(records, key_funcs)` — the engine
Union-find: groups row indices so any two rows that share a key land in one shard.
Each `key_func` has its own **namespace**, so e.g. a phone `"5"` and a vendor id
`"5"` never merge. Returns a list of index-lists. Each list is processed
**sequentially**; distinct lists run **in parallel**. Keyless rows are singletons.

Key functions:
- `_phone_key` / `_email_key` — principal identity (email lower-cased).
- `_vendor_keys` — `assigned_vendor_source_ids` (prefixed `v…`), the stable id of
  the vendor whose `VendorActivity`/M2M the request creates.

### `_shard_by_principal(records)`
Thin wrapper → `_shard_by_keys(records, [phone, email])`. Used by the **consent**
load (same principal stays serial).

### Consent load — `load_consents_via_migration`
1. Prepare all records (resolve PA name→id, drop `manager_name`).
2. Shard by principal (~15k rows → ~4k shards).
3. `ThreadPoolExecutor(LOAD_WORKERS)`; each shard runs its consents sequentially via
   `_post_consent`, which handles the **phone-collision retry** (409 mentioning
   "phone" → retry email-only) and the idempotent `409 already migrated` skip.
4. Aggregate counts (thread-safe under a lock), write errors CSV, log every 500.
   **Real result: 15,151 consents in ~5.5 min (was ~50 min sequential).**

### Request load — `load_from_csv` (used only by `/migration/request`)
Same shape, but:
- `_prepare(row)` does request-specific shaping (PA name(s)→ids, request-type
  name→id with default fallback, parse `assigned_users`, synthesize a placeholder
  phone/email only when both are missing, drop noise columns).
- Shards by **principal + vendor** (`[phone, email, vendor]`) so the same vendor's
  `VendorActivity`/M2M is never created twice concurrently.
- `_post` handles the request "Data Principal" 409 retry (re-key email-only) and
  the idempotent skip.

> **Caveat (requests):** rows with neither phone nor email get a shared dummy
> identity → they collapse into one serial shard (correct — they resolve to one
> principal). Blank rows with no PA still fail `Processing activity not found` —
> those are unmigratable source rows, not a load bug.

## Resolver helpers (all paginate across pages)
- `_resolve_pa_ids()` — name→PA id, walks **all** pages, uses the full
  `/processing/activities` endpoint (NOT `/simple`, which omits inactive PAs that
  historical records still reference).
- `_resolve_request_type_id()` / `_resolve_request_type_map()` — name→id.
- `_fetch_template_id_map()` / `_fetch_existing_template_names()` — both paginate
  every page (`per_page` large). **History note:** `_fetch_existing_template_names`
  used to read only page 1 of a 10/page endpoint, so re-runs re-created ~180
  templates as duplicates each time; it now walks all pages and all statuses, so
  the template load is genuinely idempotent.

## `run_*` entrypoints
`run_consent_migration_loading`, `run_loading(csv, endpoint)` (requests),
`run_vendor_loading`, `run_stakeholder_loading`, `run_pa_loading`,
`run_pa_link_patch`, `run_template_loading`, `run_template_approval`,
`run_template_load_and_approve`, `run_template_pa_link_patch`,
`run_request_type_seeding`, `run_request_type_loading`.
