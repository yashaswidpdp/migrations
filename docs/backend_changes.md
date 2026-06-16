# Backend (`dpdp_python`) Changes Required for Migration

Because the migration pipeline acts as a bridge between the legacy Odoo data and the new Flask (`dpdp_python`) backend, a few minor schema adjustments and code bug fixes were required in the backend repository to allow the historical data to be successfully loaded. 

If you are pulling new code into your local `dpdp_python` repository before pushing these changes, you may encounter merge conflicts or need to re-apply these changes manually. 

This document serves as a ledger of all modifications made to the backend code strictly for ensuring the ETL pipeline works.

---

## 0. Migration Extension — `dpdp_python/migration_ext/` (PREFERRED MECHANISM)

**Why**: Editing real model/route/service files means every `git pull` of upstream
silently wipes the migration patches (this happened — see Issue 26). To stop that,
all **migration-specific glue** (date preservation + idempotency) now lives in a
self-contained package that does **not exist upstream**, so a pull can never touch it.

**Package**: `dpdp_python/migration_ext/`
- `source_map.py` — a migration-owned `migration_source_map` table
  (`entity, odoo_source_id, flask_id, tenant_id`, unique per entity+source+tenant).
  Idempotency lives here, so the real `requests`/`consents` tables need **no**
  `odoo_source_id` column.
- `routes.py` — blueprint with `POST /api/migration/request` and
  `POST /api/migration/consent`:
  - **request**: reuses `Request.create_request` (so all real business logic is
    kept), forwards `raised_on` (via payload) + `action_date`/`resolution_date`/
    `closed_on` (via `**extra_fields`), dedups via the source-map (HTTP 409 → loader
    skips), and **sends no email** (the route, not the model method, sends mail).
  - **consent**: inserts `Consent` directly (skipping the template/email machinery a
    historical backfill doesn't need), preserving `consent_date`/`sent_on`/
    `delivered_on`/`valid_till`/`consent_reject_on`, dedups via the source-map.
- `serve.py` — launches the normal app via `create_app()` then registers the
  blueprint. **No upstream file is edited.**

**How to run** (under an env that has all backend deps + `SECRET_KEY`):
```bash
cd dpdp_python
python -m migration_ext.serve            # dev, port 5000 (or PORT=…)
# or: gunicorn 'migration_ext.serve:app' -b 0.0.0.0:5000
```
The loader points at `/migration/request` and `/migration/consent` (see
`scripts/load/load_flask.py` + `main.py`). The `migration_source_map` table is
auto-created on first boot (`checkfirst=True`).

**Consequence**: the migration-specific date + dedup logic is **no longer** edited
into `consent_import.py` / `request_routes.py` / model files. Sections 1–4 below
describe fixes that are **genuine upstream behaviour** the migration merely depends
on (e.g. a NOT NULL `user_role_type` will crash *any* portal-user creation, not just
migration) — keep those, but they are not migration glue.

---

## 1. Nullable Nominee Fields (Requests)
**File**: `models/request.py`
**Reason**: Historical grievance data from Odoo does not collect nominee information. Previously, these fields were `nullable=False`, causing 64/64 request inserts to fail with `NOT NULL` constraint violations.
**Change**: Updated the following fields in the `requests` table model to `nullable=True`:
- `nominee_name`
- `nominee_dob`
- `nominee_relation_id`
- `nomination_confirmed`
- `nominee_status`

## 2. Portal User Role Assignment (Consents & Requests)
**File**: `routes/consent_routes.py` (and any request user creation)
**Reason**: A new database constraint requires `user_role_type` to not be null. However, when the system automatically generated "Portal" users for incoming consents/requests, it was not assigning a role, causing a crash.
**Change**: Added `user_role_type="DataPrincipal"` to the `User(...)` constructor when auto-generating portal users on the fly.

## 3. Idempotency (Dedup) and `odoo_source_id` in Request Creation
**File**: `routes/request_routes.py`
**Reason**: Without a dedup check, re-running the migration pipeline would create duplicate request records. The `odoo_source_id` field (the original Odoo numeric ID) is used as the unique idempotency key.
**Changes**:
- Added an **ODOO DEDUP block** before request creation: if `odoo_source_id` is present in the POST payload, it queries `Request.query.filter_by(odoo_source_id=..., tenant_id=...)`. If a match is found, returns HTTP **409** immediately so the loader treats the record as "already migrated" and skips it.
- Added `odoo_source_id=int(odoo_source_id) if odoo_source_id else None` to the `Request.create_request(...)` call so the Odoo ID is persisted on the new record for future dedup checks.

```python
# ---------------- ODOO DEDUP ----------------
odoo_source_id = payload.get("odoo_source_id")
if odoo_source_id:
    existing = Request.query.filter_by(
        odoo_source_id=int(odoo_source_id),
        tenant_id=tenant_id
    ).first()
    if existing:
        return api_response("error",
            f"Request with odoo_source_id {odoo_source_id} already exists",
            {}, 409)
```

## 4. Email Service Response Type Fix
**File**: `services/request_service.py`
**Reason**: When SMTP is not configured (e.g., during a local migration run), four email-sending functions (like `send_request_create_email`) were returning a Flask `api_response` object instead of the expected `(bool, str)` tuple. The caller tried to unpack the response object, causing an unpacking crash.
**Change**: Updated the SMTP check inside these functions to correctly return `False, "SMTP not configured"` instead of `api_response(...)`.

## 4. Local Database Credentials
**File**: `.env` (Flask App)
**Reason**: Local Docker setup used `yashaswi`/`yashaswi123` while the `.env` had `odoo17`/`odoo17`.
**Change**: Updated `DB_USER` and `DB_PASSWORD` to match the correct local docker credentials.

## 5. Required Database Seeding (Post-Reset)
**Location**: Direct SQL / Database
**Reason**: To bypass multi-tenancy validation during migration, the target tenant and licenses must exist in the database.
**Setup Required**: If the database is wiped and recreated (e.g. `flask db migrate`/`upgrade`), you MUST manually insert:
1. A tenant with `domain` and `frontend_domain` matching `dpdpconsultants.com`.
2. A valid **DPCM** license assigned to that tenant (for Consent loading).
3. A valid **DPGR** license assigned to that tenant (for Request loading).
