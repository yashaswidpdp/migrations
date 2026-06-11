# Database Reset & Migration Setup Guide

**Project**: Odoo → Flask Data Migration (`dpdp_python` backend)  
**Last Updated**: 2026-04-29

This guide covers everything needed to reset the `privacium_db` PostgreSQL database and bring it to the correct state so that the migration scripts run cleanly.

---

## When to Use This Guide

- After dropping and recreating the database
- After running `flask db upgrade` on a fresh database
- After restoring a database snapshot that is missing seed data
- When the migration fails with `"Invalid tenant domain"`, `"Invalid request type"`, or `"No active license available"`

---

## Prerequisites

All commands assume:
- PostgreSQL is running on `localhost:5432`
- DB credentials: user `yashaswi`, password `yashaswi123`, database `privacium_db`
- You are in the `/home/yashaswi/Developer/dpdp_python` directory for Flask commands
- You are in the `/home/yashaswi/Developer/migration` directory for migration commands

---

## Step 1 — Drop and Recreate the Database (only if doing a full reset)

Skip this step if you just want to re-seed an existing empty database.

```bash
PGPASSWORD=yashaswi123 psql -U yashaswi -h localhost -d postgres -c "
DROP DATABASE IF EXISTS privacium_db;
CREATE DATABASE privacium_db OWNER yashaswi;
"
```

---

## Step 2 — Run the Alembic Migration

This creates all tables. Run from the `dpdp_python` directory with the venv active.

```bash
cd /home/yashaswi/Developer/dpdp_python
source venv/bin/activate
flask db upgrade
```

Expected output: a long list of `INFO [alembic.runtime.migration] Running upgrade ...` lines ending with no error.

If it crashes mid-way, check `docs/issue_report.md` Issues 13 for known migration incompatibilities and their fixes.

---

## Step 3 — Verify the Tenant Exists

The Flask app seeds the initial tenant on startup. Start Flask once to trigger seeding, then stop it.

```bash
flask run &
sleep 4
kill %1
```

Then confirm the tenant row exists:

```bash
PGPASSWORD=yashaswi123 psql -U yashaswi -h localhost -d privacium_db -c \
  "SELECT id, domain, frontend_domain, active FROM tenants;"
```

Expected output:

```
 id |       domain        |   frontend_domain   | active
----+---------------------+---------------------+--------
  2 | dpdpconsultants.com | dpdpconsultants.com | t
```

If `frontend_domain` is NULL or the column doesn't exist, run:

```sql
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS frontend_domain VARCHAR(100);
UPDATE tenants SET frontend_domain = domain WHERE frontend_domain IS NULL;
ALTER TABLE tenants ALTER COLUMN frontend_domain SET NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_tenants_frontend_domain ON tenants (frontend_domain);
```

---

## Step 4 — Insert Required Seed Data

The migration scripts require three things that are **not** created automatically by the Flask app:

1. A **DPCM license** (for consent loading)
2. A **DPGR license** (for request loading)
3. A **request type** (for DPGR requests)

Run all three inserts in one go. Get the tenant ID from Step 3 (usually `2`) and substitute if different.

```bash
PGPASSWORD=yashaswi123 psql -U yashaswi -h localhost -d privacium_db -c "
-- DPCM license: required for consent (live-consent and import endpoints)
INSERT INTO licenses (tenant_id, license_type_id, total_users, used_users, active, expires_at, expires_users)
SELECT 2, id, 1000, 0, true, '2027-12-31', 0
FROM license_types WHERE code = 'DPCM'
ON CONFLICT DO NOTHING;

-- DPGR license: required for request creation
INSERT INTO licenses (tenant_id, license_type_id, total_users, used_users, active, expires_at, expires_users)
SELECT 2, id, 1000, 0, true, '2027-12-31', 0
FROM license_types WHERE code = 'DPGR'
ON CONFLICT DO NOTHING;

-- Request type: required for DPGR request loading
-- The global unique index on name prevents duplicates; skip if already present
INSERT INTO request_types (
    name, tenant_id,
    sla_expected_days, sla_amber_notification_days, sla_red_notification_days,
    amber_alert_days, red_alert_days,
    is_complaint, is_nominee, nominee_access, is_data_principal,
    consent_withdrawal_check, is_revoke
)
VALUES (
    'Right to grievance redressal (DPDP)', 2,
    30, 25, 28,
    25, 28,
    false, false, false, true,
    true, false
)
ON CONFLICT (name) DO NOTHING;
"
```

---

## Step 5 — Add Migration-Specific DB Columns

These columns are used by the migration for idempotency and are not created by the Alembic migration file.

```bash
PGPASSWORD=yashaswi123 psql -U yashaswi -h localhost -d privacium_db -c "
-- Stores the Odoo source record ID on requests to prevent duplicate inserts on re-run
ALTER TABLE requests ADD COLUMN IF NOT EXISTS odoo_source_id INTEGER;
CREATE UNIQUE INDEX IF NOT EXISTS idx_requests_odoo_source_tenant
    ON requests (tenant_id, odoo_source_id)
    WHERE odoo_source_id IS NOT NULL;
"
```

---

## Step 6 — Verify

Run this verification query to confirm everything is in place before running the migration:

```bash
PGPASSWORD=yashaswi123 psql -U yashaswi -h localhost -d privacium_db -c "
SELECT 'tenant' AS check, domain AS value, active::text AS status
FROM tenants WHERE domain = 'dpdpconsultants.com'
UNION ALL
SELECT 'license_dpcm', lt.code, l.active::text
FROM licenses l JOIN license_types lt ON l.license_type_id = lt.id
WHERE l.tenant_id = 2 AND lt.code = 'DPCM'
UNION ALL
SELECT 'license_dpgr', lt.code, l.active::text
FROM licenses l JOIN license_types lt ON l.license_type_id = lt.id
WHERE l.tenant_id = 2 AND lt.code = 'DPGR'
UNION ALL
SELECT 'request_type', name, 'present'
FROM request_types WHERE tenant_id = 2
UNION ALL
SELECT 'odoo_source_id_column',
    CASE WHEN EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'requests' AND column_name = 'odoo_source_id'
    ) THEN 'requests.odoo_source_id' ELSE 'MISSING' END,
    'check';
"
```

All five rows should appear with `status` = `t` or `present`. If any row is missing, re-run the relevant step above.

---

## Step 7 — Start Flask

```bash
cd /home/yashaswi/Developer/dpdp_python
source venv/bin/activate
flask run
```

Confirm it is running and the tenant resolves:

```bash
curl -s -H "Host: dpdpconsultants.com" http://localhost:5000/api/health
# Expected: {"message":"Server running","status":"ok"}
```

If you see `"Invalid tenant domain: dpdpconsultants.com"`, the tenant row is missing — go back to Step 3.

---

## Step 8 — Run the Migration

```bash
cd /home/yashaswi/Developer/migration
source venv/bin/activate

# Requests (65 records)
python main.py request load

# Consents: deemed batch (Excel upload) + live (per-record JSON)
python main.py consent load
```

Expected final lines:

```
Summary: 65 succeeded, 0 failed.
...
Live consent complete: 363 succeeded, 0 failed.
```

---

## Quick Reference: Common Failures After a Reset

| Error | Cause | Fix |
|---|---|---|
| `"Invalid tenant domain: dpdpconsultants.com"` | Tenant row missing OR Flask not running | Run Step 3, then Step 7 |
| `"Invalid request type"` | No request type for tenant | Run Step 4 (request_types insert) |
| `"No active license available"` | License missing for module | Run Step 4 (licenses inserts) |
| `column tenants.frontend_domain does not exist` | Alembic migration not fully applied | Run Step 3 SQL fix |
| `null value in column user_role_type` | Flask model bug — fixed in `consent_routes.py` and `models/request.py` | No action needed (already patched) |
| Duplicate requests on re-run | `odoo_source_id` column missing | Run Step 5 |
