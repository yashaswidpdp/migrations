# Odoo → Flask Migration Runbook (step-by-step, run to completion)

Last updated: 2026-06-22. This is the authoritative, ordered procedure. It folds
in every fix and gotcha learned during the prod (tech-uat) run. Read the
**Critical facts** box first — most lost time came from those.

---

## Critical facts (read first)

- **Odoo is READ-ONLY.** Every Odoo call is HTTP GET. All writes go to the Flask
  app. The source is never mutated. Safe to run against production Odoo.
- **Flask's DB is the Docker container `privacium_postgres`.** Always inspect/seed
  via `docker exec privacium_postgres psql -U yashaswi -d privacium_db -c "..."`.
  A bare `psql -h localhost` may hit a *different* Postgres and show wrong/empty
  results. (`reconcile` already uses the container.)
- **Tenant.** This dataset is tenant **1** (`SK Finance`, host
  `skfinance.localhost.com`). Confirm with the tenants query below and substitute
  if different. Seed licenses for the tenant the API key resolves to (errors show
  `tenant_id=N`).
- **Licenses + the migration columns are NOT in alembic.** A DB reset wipes them.
  Re-seed AFTER every reset, BEFORE loading. Don't reset again without re-seeding.
- **The Flask JWT expires fast.** A 15k load can outlive it (401 mid-run). Get a
  long-TTL token, or just re-run on expiry — loads are idempotent/resumable.
- **Everything is idempotent via `migration_source_map`.** Re-running skips
  already-migrated rows (409) and retries failures. A crash is not fatal.

---

## Config (`migration/config/.env`)

```
ODOO_BASE_URL=https://tech.portal-uat.dpdpconsultants.com/api   # prod source (GET only)
ODOO_JWT_TOKEN=<source bearer>
FLASK_API_BASE_URL=http://localhost:5000/api                    # write target
FLASK_API_KEY=<dest bearer>                                     # refresh when it 401s
FLASK_TENANT_DOMAIN=skfinance.localhost.com
```

All commands run from `migration/` with the venv active:
```bash
cd /home/yashaswi/Developer/migrations_odoo_flask/migration && source venv/bin/activate
```

---

## Step 0 — Prereqs

1. Postgres container up: `docker ps | grep privacium_postgres`
2. Flask app running on `localhost:5000` **with the latest code** (it must include
   `migration_ext`, the consent listing `Consent.id` tiebreaker, and the template
   PUT plural-key fix). Restart Flask after any dpdp_python code change.
3. Fresh `FLASK_API_KEY` and `ODOO_JWT_TOKEN` in `config/.env`. Verify the Flask
   token:
   ```bash
   curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $(grep ^FLASK_API_KEY= config/.env|cut -d= -f2-)" \
     -H "Host: skfinance.localhost.com" "http://localhost:5000/api/consent/?page=1&per_page=1"
   ```
   `200` = good. `401` = refresh the key.

---

## Step 1 — Reset the database (full rebuild only)

Skip if you are not doing a clean rebuild.

```bash
# drop + recreate
PGPASSWORD=yashaswi123 psql -U yashaswi -h localhost -d postgres -c \
  "DROP DATABASE IF EXISTS privacium_db; CREATE DATABASE privacium_db OWNER yashaswi;"

# recreate schema via alembic — from dpdp_python, NOT the migration repo
cd /home/yashaswi/Developer/dpdp_python && source venv/bin/activate && flask db upgrade
cd /home/yashaswi/Developer/migrations_odoo_flask/migration && source venv/bin/activate
```

---

## Step 2 — Confirm tenant + seed (AFTER every reset, BEFORE loading)

```bash
# which tenant does the API key map to?
docker exec privacium_postgres psql -U yashaswi -d privacium_db -c \
  "SELECT id, tenant_name, frontend_domain FROM tenants;"
```
Use that id below (here `1`).

**2a. Migration-only columns** (raw SQL, not in alembic):
```bash
docker exec privacium_postgres psql -U yashaswi -d privacium_db -c "
ALTER TABLE requests ADD COLUMN IF NOT EXISTS odoo_source_id INTEGER;
CREATE UNIQUE INDEX IF NOT EXISTS idx_requests_odoo_source_tenant
  ON requests (tenant_id, odoo_source_id) WHERE odoo_source_id IS NOT NULL;
ALTER TABLE requests ADD COLUMN IF NOT EXISTS action_date timestamp;
"
```
(The `migration_source_map` table auto-creates on Flask boot.)

**2b. Licenses — all modules, big seat count** (DPCM=consent, DPGR=request,
DPTPA=vendor; rest future-proofing). Seats are consumed per NEW data principal:
```bash
docker exec privacium_postgres psql -U yashaswi -d privacium_db -c "
INSERT INTO licenses (tenant_id, license_type_id, total_users, used_users, active, expires_at, expires_users)
SELECT 1, id, 100000, 0, true, '2030-12-31', 0
FROM license_types WHERE code IN ('DPCM','DPGR','DPTPA','DPAP','DPIA','DDMT')
AND NOT EXISTS (SELECT 1 FROM licenses l WHERE l.tenant_id=1 AND l.license_type_id=license_types.id);
"
```

**2c. Stakeholder role alias** — map Odoo roles to a real Flask role. Create
`migration/data/stakeholder_role_aliases.json`:
```json
{ "PA Manager": "PA Manager Full Access", "DPO": "PA Manager Full Access" }
```

**2d. Request types** — only if the tenant has none
(`SELECT count(*) FROM request_types WHERE tenant_id=1;`). If empty, see
`db_reset_guide.md` Step 4b.

---

## Step 3 — Load entities IN THIS ORDER

Order resolves the PA↔Template circular dependency (PA references templates;
templates reference PAs). Load PA first (template refs deferred), then templates
(PA ids resolve at create), then backfill both directions.

```bash
# 1) Stakeholders — PA managers are backend users (role alias must exist)
python main.py stakeholder run-all

# 2) Processing Activities — template refs skipped now (warns), patched in 4
python main.py processing-activity extract
python main.py processing-activity transform
python main.py processing-activity load          # load ONLY — not run-all (it patch-links too early)

# 3) Templates — PAs now exist, so PA links resolve at create; then activate
python main.py template run-all                   # extract → transform → load → approve

# 4) Backfill BOTH link directions
python main.py processing-activity patch-links    # template refs onto PAs
python main.py template patch-pa-links            # PA links onto templates (idempotent)

# 5) Vendors — need PAs (department) + DPTPA license
python main.py vendor run-all

# 6) Consents (~15k; long + resumable). request-type then requests last.
python main.py consent run-all
python main.py request-type load                  # if not already seeded
python main.py request run-all --user-id <FLASK_USER_ID>
```

Notes:
- Run the long ones (consent, request) in `tmux`/`nohup` so a dropped shell
  doesn't kill them. On token expiry, refresh the key and re-run the same command
  — it 409-skips what landed and retries the rest.
- **Do NOT use `processing-activity run-all`** here — it runs `patch-links`
  immediately after PA load, before templates exist, so the backfill no-ops.

---

## Step 4 — Reconcile (verify)

```bash
# fast: SOURCE from the raw snapshot (no Odoo re-pull), field-diff + DRIFT live vs Flask
python main.py reconcile --live --cached-source
# or the module form:
python -m scripts.report.reconcile --live --cached-source

# offline (counts only, instant, no network):
python -m scripts.report.reconcile

# report file:
less -R data/processed/reconciliation_report.txt
```
- Use `--cached-source` while iterating (the raw snapshot is reused). Drop it for
  a final sign-off run that re-pulls live Odoo (slow: ~13 min for 15k).
- Do **not** reconcile mid-load — counts/DRIFT are only valid once a load finishes.
- A `LIVE VERIFY FAILED` banner / `UNVERIFIED` verdict means the Flask token died
  — refresh and re-run; never trust a count-only PASS from a 401 run.

---

## Targeted re-runs (no full rebuild)

Most fixes don't need a full reset. Loads are idempotent.

- **Retry just the failures:** re-run the entity's `... load`. Failed rows
  (not in the source-map) are retried; landed rows 409-skip.
- **Reset requests only** (e.g. to re-link vendors/PA after a code fix):
  ```bash
  docker exec privacium_postgres psql -U yashaswi -d privacium_db -c "
  BEGIN;
  UPDATE consents SET request_id = NULL, is_revoke = false WHERE request_id IS NOT NULL;
  DELETE FROM vendor_activities;
  DELETE FROM request_processing_activity;
  DELETE FROM request_assigned_user;
  DELETE FROM requests;                                  -- CASCADE: logs, assigned_vendor, tracks, comments
  DELETE FROM migration_source_map WHERE entity='request';
  COMMIT;
  "
  # then: restart Flask (if code changed) → python main.py request load
  ```
- **Reset consents only:**
  ```bash
  docker exec privacium_postgres psql -U yashaswi -d privacium_db -c "
  BEGIN;
  UPDATE vendor_activities SET consent_id = NULL WHERE consent_id IS NOT NULL;
  DELETE FROM consents;                                  -- CASCADE: internal_logs
  DELETE FROM migration_source_map WHERE entity='consent';
  COMMIT;
  "
  # then: python main.py consent load
  ```
- **Template PA links wrong/missing:** just `python main.py template patch-pa-links`
  (after Flask has the PUT plural-key fix). No template reload needed.

---

## Known / accepted residuals (not data loss)

- **Duplicate phone → NULL.** `phone_unique_per_tenant`: a phone reused across
  data principals is kept on the first, dropped (`-`) on the rest. Accepted.
- **Principal name last-write-wins.** Same email with two spellings in Odoo →
  Flask stores one (the last request migrated). Accepted policy.
- **Stakeholder DPO → PAManager.** Role fidelity not preserved; backfill via
  `/stakeholder/<id>/update-roles` if DPO must persist.
- **`isDefault` collapse.** Flask allows ONE default template per
  (template_type, language); if Odoo has several, only one stays default.
- **3 consents "PA not found: None"** — genuinely have no PA in source.
- **Vendor "already exists as DataPrincipal"** — same person is both a data
  principal and a vendor; data-level clash, fix at source if needed.

---

## Troubleshooting quick map

| Symptom | Cause | Fix |
|---|---|---|
| `No active license ... tenant_id=N` (intermittent) | licenses missing/empty in the container DB | Step 2b (seed via `docker exec`) |
| Consents load but many `400` | new-principal rows need a seat; license absent | same as above |
| `Role(s) not found: DPO/PA Manager` | role alias file missing | Step 2c |
| `Processing activity not found: X` | (old) PA resolver page-1/`/simple` bug — now fixed; ensure latest loader | re-run `consent load` |
| Token `401` mid-run | JWT expired | refresh key, re-run (resumable) |
| reconcile re-pulls all of Odoo (slow) | true `--live` | add `--cached-source` |
| Duplicate rows on re-run | `requests.odoo_source_id` / source-map missing | Step 2a |
| Everything UNVERIFIED / banner | Flask token dead during reconcile | refresh, re-run |

---

## What lives where

- Migration CLI: `migration/main.py` (Click; `--help` on any group).
- Loaders: `migration/scripts/load/load_flask.py`.
- Transforms: `migration/scripts/transform/*`. Extract: `migration/scripts/extract/extract_odoo.py`.
- Reconcile: `migration/scripts/report/reconcile.py`.
- Backend glue (survives dpdp_python `git pull`): `dpdp_python/migration_ext/`.
- Core fixes that must be PR'd to dpdp_python (lost on git pull): consent listing
  `Consent.id` tiebreaker (`routes/consent/_helpers.py`); template PUT plural key
  (`routes/notice_template/crud.py`).
