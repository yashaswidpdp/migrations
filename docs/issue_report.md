# Migration Issue Report

**Project**: Odoo → Flask Data Migration  
**Last Updated**: 2026-04-30  
**Scope**: Full ETL pipeline — Extract (Odoo), Transform, Load (Flask)

---

## Issue 1 — Odoo API Returns 405 Method Not Allowed

### Symptom
Running `python main.py` crashed immediately with HTTP 405 on every extraction attempt.

### Root Cause
The extraction code used `requests.post()` to call the Odoo dashboard endpoints (`/dpcm/dashboard`, `/dpgr/dashboard`). The Odoo REST API only accepts GET requests on these endpoints. POST is not allowed and returns 405.

### Contributing Factor
The original `docs/mapping.md` (written before testing) incorrectly documented the Odoo source method as `POST`. This was written from assumption, not from live API inspection.

### Fix Applied
Changed `requests.post(url, json=params)` to `requests.get(url, params=params)` in `scripts/extract/extract_odoo.py`. Pagination parameters (`page_no`, `rec_limit`) are sent as query string parameters, not a request body.

### File
`scripts/extract/extract_odoo.py`

---

## Issue 2 — Wrong Base URL (Extra `/v2` Path Segment)

### Symptom
All Odoo API calls returned 404 even after fixing the HTTP method.

### Root Cause
The default value for `ODOO_BASE_URL` in the code was hardcoded as `https://tool.dpdp-portal.dpdpconsultants.com/api/v2`. The actual Odoo API lives at `https://tool.dpdp-portal.dpdpconsultants.com/api` — no `/v2` segment. The `/v2` path caused every endpoint to resolve to a non-existent URL.

### Fix Applied
Removed `/v2` from the default URL. `config/.env` now explicitly sets `ODOO_BASE_URL=https://tool.dpdp-portal.dpdpconsultants.com/api`.

### File
`scripts/extract/extract_odoo.py`, `config/.env`

---

## Issue 3 — MAX_RECORDS Defaulted to 10, Silently Capping Extraction

### Symptom
Extraction completed "successfully" but only 10 records were ever written to CSV, regardless of how many records exist in Odoo.

### Root Cause
The code had a comment `# 0 means no limit` but the actual default was `MAX_RECORDS = int(os.getenv("MAX_RECORDS", 10))`. The `.env` file also had `MAX_RECORDS=10` set explicitly. This meant every extraction run was silently capped at 10 records with no warning in the logs that a limit was active.

### Impact
All downstream processing (transform, load) ran against a 10-record sample, giving false confidence that the full dataset was being migrated.

### Fix Applied
Changed the code default to `0`. Updated `config/.env` to `MAX_RECORDS=0` for production runs. For test/dev runs, a small value can be set explicitly and intentionally.

### File
`scripts/extract/extract_odoo.py`, `config/.env`

---

## Issue 4 — Pagination Terminated Too Early

### Symptom
Even without the `MAX_RECORDS` cap, extraction would stop after the first page in some cases.

### Root Cause
The original pagination termination condition was `if len(records) < BATCH_SIZE: break`. This assumes that a partial page means the last page — but the Odoo API returns a `pagination` object in every response with a `total_page` field that is the authoritative signal for "last page". Using record count as a heuristic incorrectly stopped early when the last page happened to be full.

### Fix Applied
The code now reads `data.get("pagination", {}).get("total_page")` from the response. If present, it uses `if page >= total_pages: break`. The record-count heuristic is kept only as a fallback for endpoints that omit `total_page`.

### File
`scripts/extract/extract_odoo.py`

---

## Issue 5 — Wrong Flask Endpoint for Consent Loading

### Symptom
Consents were being sent to `/consent/create` which returned 404. The endpoint does not exist.

### Root Cause
The endpoint path was assumed based on a naming convention without inspecting the actual Flask route definitions. The real endpoints for consent creation are `/consent/live-consent` (for live consents) and `/consent/import` (for deemed/legacy consents via Excel upload).

### Broader Problem Discovered
These two Flask endpoints have hardcoded enum behavior:

| Endpoint | Hardcodes | Input Format |
|---|---|---|
| `/consent/import` | `status = Deemed Consent`, `legacyType = Legacy` | Excel file (multipart) |
| `/consent/live-consent` | `status = Consented`, `legacyType = Live` | JSON body |

Sending all consents to a single endpoint would corrupt the `status` and `legacyType` values for one subset of the data.

### Fix Applied
Implemented a **split loading strategy**:
1. `transform_consent.py` now splits output into `_deemed.csv` (records where `status == "Deemed Consent"`) and `_live.csv` (all other statuses).
2. `_deemed.csv` → `load_deemed_via_import()` → builds an Excel file per group, POSTs multipart to `/consent/import`.
3. `_live.csv` → `load_live_via_live_consent()` → POSTs JSON per record to `/consent/live-consent`.

Each subset is routed to the endpoint whose hardcoded values happen to match that subset's data. No Flask backend changes were required.

### Files
`scripts/transform/transform_consent.py`, `scripts/load/load_flask.py`, `main.py`

---

## Issue 6 — `/consent/import` Requires Excel, Not JSON

### Symptom
Attempts to POST JSON to `/consent/import` returned 400/422 errors.

### Root Cause
The `/consent/import` Flask route expects a multipart/form-data request with an `.xlsx` file attachment — not a JSON body. This is consistent with an admin bulk-import UI workflow. Additionally, the three enum fields (`legacy_type`, `consent_type`, `processing_type`) are sent as separate form fields alongside the file and apply globally to all rows in that file.

### Implication
Records must be grouped by `(processingType, consentType, legacyType)` before building Excel files. All rows in a single uploaded file must share the same values for those three fields, because the endpoint applies them to every row.

### Fix Applied
`load_deemed_via_import()` groups rows by `(processingType, consentType, legacyType)`, builds one in-memory Excel workbook per group using `openpyxl` + `BytesIO`, and POSTs each as multipart. The `Content-Type` header is NOT set manually — `requests` sets it automatically with the correct multipart boundary. Setting it manually breaks the request.

### File
`scripts/load/load_flask.py`

---

## Issue 7 — Processing Activity IDs Are Not Transferable Between Systems

### Symptom
Consents loaded into Flask had incorrect `processing_activity_id` values, linking to wrong PAs.

### Root Cause
Odoo assigns its own integer IDs to Processing Activities (e.g., PA "Delhi Account" has `id=13` in Odoo). Flask has its own separate auto-increment IDs for the same entities (the same "Delhi Account" PA might have `id=3` in Flask). The migration was copying Odoo's numeric IDs directly into Flask payloads, causing FK references to point to entirely different or non-existent PAs.

### Fix Applied
Two resolution strategies, one per load path:
- **Live consents** (`load_live_via_live_consent`): calls `_resolve_pa_ids()` which GETs `/processing-activity/` from the Flask API and builds a `{name: flask_id}` map. Each record's `processing_activity_name` is used to look up the correct Flask ID at load time.
- **Requests** (`load_from_csv`): lazy-imports Flask's SQLAlchemy models (`ProcessingActivity`, `User`) and queries the live database directly by name.

### File
`scripts/load/load_flask.py`

---

## Issue 8 — SQLAlchemy Imports at Module Level Before sys.path Was Set

### Symptom
`import scripts.load.load_flask` crashed with `ModuleNotFoundError: No module named 'app'` even before any migration code ran.

### Root Cause
The Flask app's models live in a sibling directory (`dpdp_python/`) that is not on Python's default path. The file added the path via `sys.path.append(...)` but the three Flask imports (`from app import create_app`, `from models.processing_activity import ProcessingActivity`, `from models.user import User`) were placed *above* the `sys.path.append` call at the module level. Python resolves imports at parse time, so the path wasn't set yet when the imports were attempted.

### Fix Applied
- `sys.path.append(...)` stays at module level (line 11) so it runs immediately on import.
- The three Flask model imports were moved inside the `load_from_csv()` function body as **lazy imports** — they only execute the first time that function is called, by which point the path is already set.

### File
`scripts/load/load_flask.py`

---

## Issue 9 — `rag_status` Field Silently Missing From Request Output

### Symptom
All migrated Requests had no `rag_status` value in Flask, defaulting to whatever Flask used as a fallback.

### Root Cause
`transform_request.py` computed `map_rag_status(row.get("ragStatus", "Green"))` and assigned it to a local variable, but never included it in the `record` dict that was written to the output CSV. The computation was there, the result was silently discarded.

### Fix Applied
Added `"rag_status": map_rag_status(row.get("ragStatus", "Green"))` to the record dict.

### File
`scripts/transform/transform_request.py`

---

## Issue 10 — No Live Consents in Raw Odoo Data (Expected Behavior, Misunderstood)

### Symptom
After extraction, `raw_consents.csv` contained only `legacyType = "legacy"` records. No `legacyType = "live"` records appeared. This was initially thought to be a bug.

### Diagnosis
This is expected and correct. Odoo is the legacy system. All data in it is, by definition, historical legacy data. "Live" consents (`legacyType = "Live"`) are those collected through the *new* Flask-powered digital consent flow — a flow that did not exist when Odoo was in use. Live consents will be created in Flask going forward; they are not something that would ever exist in Odoo to migrate.

### Secondary Note
There were 2 records in Odoo with `status = "Consented"` and `legacyType = "legacy"`. These are records where a user explicitly consented through the old Odoo workflow. The split strategy routes them to `/consent/live-consent` which hardcodes `legacyType = "Live"` — a minor semantic mismatch. No Flask endpoint handles `Consented + Legacy` without overriding one field. Preserving the correct `status = Consented` was chosen as the higher-priority field.

### Action
No code change. The behavior is correct. Document it to prevent future confusion.

---

## Issue 11 — Flask Database Missing `frontend_domain` Column (Active)

### Symptom
Every call to `/consent/live-consent` returned HTTP 500 with:
```
sqlalchemy.exc.ProgrammingError: column tenants.frontend_domain does not exist
```

### Root Cause
The Flask `Tenant` model (`dpdp_python/models/licenses.py`) has `frontend_domain = db.Column(db.String(100), unique=True, nullable=False)` added recently. An Alembic migration was generated (`b5f57f279449`) that adds this column — but `flask db upgrade` was never run, so the column does not exist in the live PostgreSQL database.

`flask db upgrade` cannot be run directly because:
- The database was originally created via `db.create_all()` (not via migrations), so `alembic_version` is empty
- The migration also tries to `CREATE TABLE backend_users` and `CREATE TABLE consent_processing_activity`, both of which already exist in the database — Alembic would crash before reaching the `ADD COLUMN` for `frontend_domain`

### Fix Required
Run the following from `/home/yashaswi/Developer/dpdp_python`:

```bash
source venv/bin/activate

python -c "
from app import create_app
app = create_app()
with app.app_context():
    from app import db
    db.session.execute(db.text('ALTER TABLE tenants ADD COLUMN frontend_domain VARCHAR(100)'))
    db.session.execute(db.text('UPDATE tenants SET frontend_domain = domain'))
    db.session.execute(db.text('ALTER TABLE tenants ALTER COLUMN frontend_domain SET NOT NULL'))
    db.session.execute(db.text(
        'ALTER TABLE tenants ADD CONSTRAINT tenants_frontend_domain_key UNIQUE (frontend_domain)'
    ))
    db.session.commit()
    print('frontend_domain column added.')
"

flask db stamp b5f57f279449
```

The `UPDATE tenants SET frontend_domain = domain` seeds existing rows with the value from the `domain` column (already unique per tenant) so the `UNIQUE` and `NOT NULL` constraints can be applied cleanly. After this, `flask db stamp` marks the migration as applied so future `flask db migrate` / `flask db upgrade` cycles work correctly.

### File
`dpdp_python/models/licenses.py` (model), `dpdp_python/migrations/versions/b5f57f279449_.py` (migration)

---

---

## Issue 11 — Flask Database Missing `frontend_domain` Column (RESOLVED)

**Date fixed: 2026-04-29**

### Symptom
Every call to `/consent/live-consent` returned HTTP 500:
```
sqlalchemy.exc.ProgrammingError: column tenants.frontend_domain does not exist
```

### Root Cause
The Flask `Tenant` model (`dpdp_python/models/licenses.py:42`) declared `frontend_domain` as a NOT NULL column. The Alembic migration that creates this column (`b5f57f279449`) was never applied. The `before_request` hook `resolve_tenant_from_domain()` in `app.py` queries `Tenant.query.filter_by(domain=host)` on every request, which causes SQLAlchemy to SELECT all model columns including the non-existent `frontend_domain`. PostgreSQL throws `UndefinedColumn` before the route handler runs.

### Fix Applied
Added the column directly via SQL (bypassing Alembic since the migration file had incompatibilities with the populated database):
```sql
ALTER TABLE tenants ADD COLUMN frontend_domain VARCHAR(100);
UPDATE tenants SET frontend_domain = domain;
ALTER TABLE tenants ALTER COLUMN frontend_domain SET NOT NULL;
CREATE UNIQUE INDEX uq_tenants_frontend_domain ON tenants (frontend_domain);
```
Then ran `flask db upgrade` (with the migration file patched for all other incompatibilities) and stamped `flask db stamp b5f57f279449`.

### Files
`dpdp_python/models/licenses.py`, `dpdp_python/migrations/versions/b5f57f279449_.py`, direct SQL on `privacium_db`

---

## Issue 12 — HTTP 400 `"Invalid tenant domain: localhost"` on All API Calls

**Date fixed: 2026-04-29**

### Symptom
363/363 live consent loads failed with HTTP 400:
```json
{"message": "Invalid tenant domain: localhost"}
```

### Root Cause
Flask's `before_request` hook extracts the tenant from `request.host.split(":")[0]`. When `requests` (the Python library) calls `http://localhost:5000/api/...`, it auto-sets `Host: localhost:5000`. Flask extracts `localhost`. No tenant has `domain = 'localhost'`, so every request is rejected before reaching any route handler.

### Fix Applied
Added `"Host": os.getenv("FLASK_TENANT_DOMAIN")` to `self.headers` in `FlaskLoader.__init__`. Added `FLASK_TENANT_DOMAIN=dpdpconsultants.com` to `config/.env`.

Also fixed `load_deemed_via_import`: it was building a filtered headers dict `{k: v for k, v in self.headers.items() if k != "Content-Type"}` which also dropped the `Host` header — changed to keep all headers except `Content-Type`.

### Files
`scripts/load/load_flask.py`, `config/.env`

---

## Issue 13 — `flask db upgrade` Failing: Multiple NOT NULL and Type Incompatibilities

**Date fixed: 2026-04-29**

### Symptom
`flask db upgrade` crashed with a series of errors when run against the populated `privacium_db` database. The migration was auto-generated against a fresh DB schema and assumed empty tables.

### Root Cause and Fixes (in order of failure)

1. **`consent_decision` NOT NULL with 132 NULL rows** — Fixed with `UPDATE consents SET consent_decision = 'PENDING' WHERE consent_decision IS NULL` before the `ALTER COLUMN`.

2. **`requests.status` datatype mismatch** — Casting from old enum `request_status_old` to new enum `request_status` not possible without `USING` clause. Fixed with `postgresql_using='status::text::request_status'`.

3. **Nominee fields NOT NULL with NULL data** — `nominee_name` (19 NULLs), `nominee_dob` (27), `nominee_relation_id` (27), `nominee_status` (99). Fixed with backfills: `'N/A'`, `'2000-01-01'`, `1` (FK to first nominee_relation), `'null'::json`.

4. **`user_role_type` enum type does not exist** — New enum not yet in DB. Fixed with idempotent `DO $$ BEGIN CREATE TYPE user_role_type AS ENUM (...); EXCEPTION WHEN duplicate_object THEN NULL; END $$`.

5. **`user_role_type` NOT NULL without data** — Column added as nullable first, backfilled from old `backend_user_type`/`portal_user_type`, then set NOT NULL in a second batch.

6. **`vendor_processing_activity` PK columns can't be set nullable** — Migration tried to `ALTER COLUMN` primary-key member columns to `nullable=True`. PostgreSQL rejects this. Removed those `alter_column` calls.

7. **`tenants.frontend_domain` already added manually** — Added idempotent check via `information_schema.columns` before attempting to add the column.

8. **`vendors.vendor_id` NOT NULL with NULL data** — Backfilled with `'VND-' || id::text`.

### Files
`dpdp_python/migrations/versions/b5f57f279449_.py`

---

## Issue 14 — `flask run` Failing: `'CONSENT' not in TemplateTypeEnum`

**Date fixed: 2026-04-29**

### Symptom
Flask started but crashed during template seeding with:
```
ValueError: 'CONSENT' is not a valid value of TemplateTypeEnum
```

### Root Cause
The `email_templates` table had uppercase values (`'CONSENT'`, `'EMAIL'`) stored in the `template_type` column. The SQLAlchemy model expected mixed-case values (`'Consent'`, `'Email Template'`). Additionally the column was `VARCHAR(12)`, too short for `'Email Template'` (14 chars).

### Fix Applied
```sql
ALTER TABLE email_templates ALTER COLUMN template_type TYPE VARCHAR(50);
UPDATE email_templates SET template_type = 'Consent' WHERE template_type = 'CONSENT';
UPDATE email_templates SET template_type = 'Email Template' WHERE template_type IN ('EMAIL', 'Email');
```

### Files
Direct SQL on `privacium_db`

---

## Issue 15 — HTTP 500 `"No active license available"` for Consent Loading

**Date fixed: 2026-04-29**

### Symptom
Live consent loading returned HTTP 500 for every record: `"No active license available"`.

### Root Cause
Tenant 3 (`dpdpconsultants.com`) had zero rows in the `licenses` table. The `consume_license()` utility checks for an active license before allowing any user creation. With no license record, all consent creations fail.

### Fix Applied
Inserted a DPCM license (module code `DPCM`, `license_type_id = 3`) for tenant 3:
```sql
INSERT INTO licenses (tenant_id, license_type_id, total_users, used_users, active, expires_at, expires_users)
VALUES (3, 3, 1000, 0, true, '2027-12-31', 0);
```

### Files
Direct SQL on `privacium_db`

---

## Issue 16 — HTTP 500 `"null value in column user_role_type"` for Consent Loading

**Date fixed: 2026-04-29**

### Symptom
After the license was added, consent loading returned HTTP 500:
```
psycopg2.errors.NotNullViolation: null value in column "user_role_type" of relation "users"
```

### Root Cause
The `create_consent` route in `consent_routes.py` creates a new `User` when one doesn't already exist. The `User` constructor call was missing `user_role_type`, which is a `NOT NULL` column (added as part of the Alembic migration).

### Fix Applied
Added `user_role_type="DataPrincipal"` to the `User(...)` constructor in `consent_routes.py`.

### Files
`dpdp_python/routes/consent_routes.py`

---

## Issue 17 — `"No module named 'flask'"` when Loading Requests

**Date fixed: 2026-04-29**

### Symptom
`python main.py request load` crashed immediately:
```
ModuleNotFoundError: No module named 'flask'
```

### Root Cause
`load_from_csv` contained `from app import create_app` and direct SQLAlchemy model imports at the top of the function body. The migration venv does not have Flask installed (and shouldn't — it's a separate environment). These imports were left from an earlier design that was later abandoned.

### Fix Applied
Removed all Flask/SQLAlchemy imports from `load_from_csv`. PA resolution now uses the existing `_resolve_pa_ids()` HTTP method. Request type resolution uses the new `_resolve_request_type_id()` HTTP method. No direct DB access from the migration side.

### Files
`scripts/load/load_flask.py`

---

## Issue 18 — `"Out of range float values are not JSON compliant: nan"` for Request Loading

**Date fixed: 2026-04-29**

### Symptom
Request loading raised a `ValueError` on records where pandas read empty CSV cells as `float('nan')`. `requests.post(json=...)` uses `json.dumps`, which cannot serialise `nan`.

### Fix Applied
Added a NaN → `None` cleanup at the start of each row's processing:
```python
record_data = {k: (None if isinstance(v, float) and pd.isna(v) else v) for k, v in row.to_dict().items()}
```

### Files
`scripts/load/load_flask.py`

---

## Issue 19 — HTTP 400 `"Invalid request type"` for All Request Records

**Date fixed: 2026-04-29**

### Symptom
All 64 request records failed with HTTP 400 `"Invalid request type"`.

### Root Cause (three parts)

**Part A** — Tenant 3 had no request types. The processed CSV has `request_type_id = 1` (Odoo's ID). No Flask request type with `id = 1` exists for tenant 3.

**Part B** — `_resolve_request_type_id()` (newly added) was calling the wrong URL: `/request-type/` instead of `/request-types/`.

**Part C** — The response parsing was wrong: the method accessed `body["data"]` directly, but the actual shape is `body["data"]["records"]`.

### Fix Applied
- Inserted request type `"Right to grievance redressal (DPDP)"` for tenant 3 (`id = 5` after insert).
- Fixed the URL in `_resolve_request_type_id()` to `/request-types/`.
- Fixed response parsing to `data.get("records", []) if isinstance(data, dict) else data`.

### Files
`scripts/load/load_flask.py`, direct SQL on `privacium_db`

---

## Issue 20 — HTTP 400 `"null value in column nominee_name"` for Request Loading

**Date fixed: 2026-04-29**

### Symptom
After request type was resolved, all 64 records failed:
```
psycopg2.errors.NotNullViolation: null value in column "nominee_name" of relation "requests"
```

### Root Cause
The `requests` table and `Request` model had `nominee_name`, `nominee_dob`, `nominee_relation_id`, `nomination_confirmed`, and `nominee_status` defined as `NOT NULL`. Odoo grievance redressal requests don't have nominee data — these fields are only populated when a request specifically involves a nominee appointment.

### Fix Applied
Made all five columns nullable in the DB and updated the model to match:
```sql
ALTER TABLE requests ALTER COLUMN nominee_name DROP NOT NULL;
ALTER TABLE requests ALTER COLUMN nominee_dob DROP NOT NULL;
ALTER TABLE requests ALTER COLUMN nominee_relation_id DROP NOT NULL;
ALTER TABLE requests ALTER COLUMN nomination_confirmed DROP NOT NULL;
ALTER TABLE requests ALTER COLUMN nominee_status DROP NOT NULL;
```

### Files
`dpdp_python/models/request.py`, direct SQL on `privacium_db`

---

## Issue 21 — HTTP 400 `"Object of type Response is not JSON serializable"` for Request Loading

**Date fixed: 2026-04-29**

### Symptom
After nominee fix, all 64 records failed with HTTP 400 and the message `"Object of type Response is not JSON serializable"`.

### Root Cause
Four functions in `request_service.py` (`send_request_create_email`, `send_nomination_email`, `send_nominee_accept_email`, `send_nominee_decline_email`) returned `api_response("error", "SMTP not configured", 500)` when SMTP was not configured. `api_response()` returns a tuple `(Flask Response, int)`. The callers expected `(bool, str)` and unpacked accordingly — `create_ok` received a `Response` object. That object then ended up inside the `create_email_status` dict, and when `api_response("success", ..., {... create_email_status ...})` tried to serialise it via `jsonify`, Python threw `TypeError: Object of type Response is not JSON serializable`. This exception was caught by the route's `except` block and returned as a 400 with that error message.

### Fix Applied
Changed all four early-return cases from `return api_response(...)` to `return False, "SMTP not configured"`.

### Files
`dpdp_python/services/request_service.py`

---

## Issue 22 — HTTP 500 `"null value in column user_role_type"` for Request Loading

**Date fixed: 2026-04-29**

### Symptom
After the SMTP fix, some request records (those for users who didn't already exist) failed with the same `user_role_type` NOT NULL violation seen in Issue 16, but this time from the request path.

### Root Cause
`_get_or_create_user()` in `models/request.py` creates a new `User` without `user_role_type`, same as the consent route bug in Issue 16.

### Fix Applied
Added `user_role_type="DataPrincipal"` to the `User(...)` constructor in `_get_or_create_user`.

### Files
`dpdp_python/models/request.py`

---

## Issue 23 — HTTP 500 `"No active license available"` for Request Loading

**Date fixed: 2026-04-29**

### Symptom
After the user creation fix, 43/64 records failed with `"No active license available"`.

### Root Cause
Requests consume a DPGR module license. Tenant 3 only had a DPCM license (added in Issue 15). The DPGR license (`license_type_id = 1`) was missing.

### Fix Applied
```sql
INSERT INTO licenses (tenant_id, license_type_id, total_users, used_users, active, expires_at, expires_users)
VALUES (3, 1, 1000, 0, true, '2027-12-31', 0);
```

### Files
Direct SQL on `privacium_db`

---

## Issue 24 — HTTP 400 `"Missing fields: email"` for 12 Phone-Only Request Records

**Date fixed: 2026-04-29**

### Symptom
12 request records failed with `"Missing fields: email"`. These were records where the Odoo source had a phone number but no email.

### Root Cause
The `/request/create` Flask route requires `email` as a mandatory field. The migration had no fallback for phone-only records.

### Fix Applied
Added a placeholder email generator in `load_from_csv`:
```python
if not record_data.get("email"):
    phone_val = str(record_data.get("phone", "")).strip()
    record_data["email"] = f"{phone_val}@migration.local"
```

### Files
`scripts/load/load_flask.py`

---

## Issue 25 — Request Loading Not Idempotent (Duplicates on Re-run)

**Date fixed: 2026-04-29**

### Symptom
Running `python main.py request load` twice created duplicate rows in the `requests` table. Each run auto-generates a new `request_no`, so Flask had no way to detect that the same Odoo record was being imported again.

### Root Cause
The `requests` table had no column to store the Odoo source record ID. `request_no` is always auto-generated by Flask and cannot be supplied from the payload.

### Fix Applied
Three-part fix:

1. **DB**: Added `odoo_source_id INTEGER` column + partial unique index:
```sql
ALTER TABLE requests ADD COLUMN IF NOT EXISTS odoo_source_id INTEGER;
CREATE UNIQUE INDEX IF NOT EXISTS idx_requests_odoo_source_tenant
    ON requests (tenant_id, odoo_source_id)
    WHERE odoo_source_id IS NOT NULL;
```

2. **Model** (`dpdp_python/models/request.py`): Added `odoo_source_id = db.Column(db.Integer, nullable=True, index=True)`.

3. **Route** (`dpdp_python/routes/request_routes.py`): Added dedup check in `create_request()` after request-type validation:
```python
odoo_source_id = payload.get("odoo_source_id")
if odoo_source_id:
    existing = Request.query.filter_by(
        odoo_source_id=int(odoo_source_id), tenant_id=tenant_id
    ).first()
    if existing:
        return api_response("error", f"Request with odoo_source_id {odoo_source_id} already exists", {}, 409)
```
Also passes `odoo_source_id` as a kwarg to `Request.create_request()` so it is stored on insert.

The migration loader already treats HTTP 409 as "skip" — no changes needed to `load_flask.py`.

### Files
`dpdp_python/models/request.py`, `dpdp_python/routes/request_routes.py`, direct SQL on `privacium_db`

---

---

## Issue 26 — All Backend Patches Overwritten After `git pull` in `dpdp_python`

### Symptom
After pulling latest upstream changes into the Flask backend (`dpdp_python`), all migration-specific patches were overwritten:
- Nominee columns reverted to `nullable=False`
- `odoo_source_id` column definition removed from model
- `user_role_type="DataPrincipal"` removed from User constructors
- ODOO DEDUP block removed from `request_routes.py`
- `api_response(...)` bug re-introduced in 3 email service functions

### Root Cause
The local patches were applied directly on `main`/`dev` without a dedicated branch. An upstream pull from the remote repository overwrote all local-only changes.

### Fix Applied (2026-04-30)
Reapplied all patches manually in order:
1. `models/request.py`: nominee columns → `nullable=True`, added `odoo_source_id` column, `user_role_type="DataPrincipal"` in `_get_or_create_user()`
2. `routes/request_routes.py`: re-added ODOO DEDUP block, passed `odoo_source_id` kwarg to `create_request()`
3. `routes/consent_routes.py`: re-added `user_role_type="DataPrincipal"` to User constructor
4. `services/request_service.py`: fixed `send_nomination_email`, `send_nominee_accept_email`, `send_nominee_decline_email` returning `api_response(...)` → `(False, "SMTP not configured")`
5. Direct SQL: re-ran `ALTER TABLE requests ALTER COLUMN <nominee_col> DROP NOT NULL` (idempotent — already done in DB)

### Prevention
See `docs/backend_changes.md` — the canonical ledger of all migration-required patches. Re-read it and reapply after every `git pull` into `dpdp_python`.

### Files
`dpdp_python/models/request.py`, `dpdp_python/routes/request_routes.py`, `dpdp_python/routes/consent_routes.py`, `dpdp_python/services/request_service.py`

---

## Summary Table

| # | Issue | Severity | Status | Fix Location |
|---|---|---|---|---|
| 1 | Odoo API requires GET, not POST | Critical | Fixed | `extract_odoo.py` |
| 2 | Wrong base URL (`/v2` suffix) | Critical | Fixed | `extract_odoo.py`, `.env` |
| 3 | `MAX_RECORDS=10` silently capped extraction | High | Fixed | `extract_odoo.py`, `.env` |
| 4 | Pagination terminated too early | High | Fixed | `extract_odoo.py` |
| 5 | Wrong Flask consent endpoint | Critical | Fixed | `load_flask.py`, `main.py` |
| 6 | `/consent/import` requires Excel, not JSON | Critical | Fixed | `load_flask.py` |
| 7 | Odoo PA IDs ≠ Flask PA IDs | High | Fixed | `load_flask.py` |
| 8 | SQLAlchemy imports before `sys.path` set | Critical | Fixed | `load_flask.py` |
| 9 | `rag_status` computed but not included in output | Medium | Fixed | `transform_request.py` |
| 10 | No live consents in Odoo raw data | Low | Not a bug — documented | — |
| 11 | `tenants.frontend_domain` column missing in DB | Critical | Fixed | Flask DB direct SQL, migration |
| 12 | HTTP 400 `"Invalid tenant domain: localhost"` | Critical | Fixed | `load_flask.py`, `.env` |
| 13 | `flask db upgrade` multiple NOT NULL / type failures | Critical | Fixed | `b5f57f279449_.py` |
| 14 | Flask start crash: `'CONSENT' not in TemplateTypeEnum` | Critical | Fixed | DB direct SQL |
| 15 | No active DPCM license for tenant 3 | Critical | Fixed | DB direct SQL |
| 16 | `user_role_type` NOT NULL on consent user creation | Critical | Fixed | `consent_routes.py` |
| 17 | `"No module named 'flask'"` in request loader | Critical | Fixed | `load_flask.py` |
| 18 | pandas NaN not JSON-serialisable | High | Fixed | `load_flask.py` |
| 19 | HTTP 400 `"Invalid request type"` — missing type, wrong URL, wrong parsing | Critical | Fixed | `load_flask.py`, DB direct SQL |
| 20 | Nominee columns NOT NULL, Odoo has no nominee data | High | Fixed | `models/request.py`, DB direct SQL |
| 21 | `send_request_create_email` returning `Response` instead of `(bool, str)` | High | Fixed | `services/request_service.py` |
| 22 | `user_role_type` NOT NULL on request user creation | Critical | Fixed | `models/request.py` |
| 23 | No active DPGR license for tenant 3 | Critical | Fixed | DB direct SQL |
| 24 | 12 phone-only request records have no email | Medium | Fixed | `load_flask.py` |
| 25 | Request loading not idempotent — duplicates on re-run | High | Fixed | `models/request.py`, `routes/request_routes.py`, DB direct SQL |
| 26 | All backend patches overwritten after `git pull` | Critical | Fixed | All 4 backend files (see Issue 26 body above) |
