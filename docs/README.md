# Migration Docs — Index

Documentation for the Odoo → Flask migration, split into two sequential sets.
Read each folder in number order.

## 📂 [`01_migration_logic/`](01_migration_logic/00-overview.md) — the ETL project (`migration/`)

| # | Doc |
|---|---|
| 00 | [Overview](01_migration_logic/00-overview.md) — what it is, ETL flow, entities, dependency order |
| 01 | [Config & environment](01_migration_logic/01-config-and-env.md) — `.env` keys, worker knobs, token expiry |
| 02 | [Extract — `extract_odoo.py`](01_migration_logic/02-extract-extract_odoo.md) — pooled session, parallel pagination, resumable enrichment |
| 03 | [Transform — conventions](01_migration_logic/03-transform-overview.md) |
| 04 | [Transform — per entity](01_migration_logic/04-transform-per-entity.md) |
| 05 | [Load — `load_flask.py`](01_migration_logic/05-load-load_flask.md) — idempotency, principal/vendor sharding |
| 06 | [Orchestrator — `main.py` & `migrate-all`](01_migration_logic/06-orchestrator-main-cli.md) |
| 07 | [Reconcile — the audit report](01_migration_logic/07-reconcile-report.md) |
| 08 | [`migration/models/`](01_migration_logic/08-models.md) |
| 09 | [Performance, parallelism & resume](01_migration_logic/09-performance-and-resume.md) |

## 📂 [`02_dpdp_python_logic/`](02_dpdp_python_logic/00-overview.md) — the backend glue (`migration_ext`)

| # | Doc |
|---|---|
| 00 | [Overview](02_dpdp_python_logic/00-overview.md) — extension-not-edit, endpoints, boot contract |
| 01 | [`register_migration` & `serve.py`](02_dpdp_python_logic/01-register_migration-and-serve.md) |
| 02 | [`routes.py` — the endpoints](02_dpdp_python_logic/02-routes-migration-endpoints.md) |
| 03 | [⭐ No emails / OTP / notifications — the exact lines](02_dpdp_python_logic/03-no-email-no-notification.md) |
| 04 | [Idempotency — `source_map.py`](02_dpdp_python_logic/04-source_map-idempotency.md) |
| 05 | [`ensure_license.py`](02_dpdp_python_logic/05-ensure_license.md) |
| 06 | [Attachments subsystem](02_dpdp_python_logic/06-attachments-subsystem.md) |
| 07 | [Operator tools — `reset.py` & `seed_operator.py`](02_dpdp_python_logic/07-reset-and-seed_operator.md) |

> Older standalone docs (`migration_ext_architecture.md`, `migration_runbook.md`,
> `db_reset_guide.md`, audits, etc.) remain alongside this index and are
> cross-referenced from the numbered docs.

---

# 🏃 Run command cheat-sheet

> Paths assume `dpdp_python/` and `migration/` siblings. Adjust to your checkout.

## 0. Prerequisites (once per environment / after a DB reset)

```bash
# (a) Boot the Flask app the RIGHT way — migration routes only exist via serve:
cd dpdp_python
./venv/bin/python -m migration_ext.serve            # dev, port 5000
# or: nohup ./venv/bin/python -m migration_ext.serve > migration_serve.log 2>&1 &

# (b) Seed licenses for the tenant (else consent/request/vendor loads 404→"No active license"):
./venv/bin/python -m migration_ext.ensure_license --tenant 1

# (c) Confirm config/.env has fresh ODOO_JWT_TOKEN + FLASK_API_KEY + FLASK_TENANT_DOMAIN
```

Verify the migration routes are mounted (expect 400/422, NOT 404):
```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST \
  -H "Authorization: Bearer $(grep ^FLASK_API_KEY= dpdp_python/.env 2>/dev/null || grep ^FLASK_API_KEY= migration/config/.env|cut -d= -f2-)" \
  -H "Host: skfinance.localhost.com" -H "Content-Type: application/json" \
  -d '{}' http://localhost:5000/api/migration/consent
```

## 1. Migrate everything (one command)

```bash
cd migration && source venv/bin/activate
python main.py migrate-all                          # whole pipeline, dependency order
python main.py migrate-all --user-id <FLASK_USER_ID> # optional fallback request owner
python main.py migrate-all --continue-on-error      # don't stop on a stage failure
```

## 2. Per-entity (stage by stage)

```bash
# full pipeline for one entity (extract → transform → load [→ approve]):
python main.py request-type run-all
python main.py stakeholder run-all
python main.py processing-activity load             # load only (NOT run-all — patches links too early)
python main.py template run-all
python main.py processing-activity patch-links      # after templates exist
python main.py template patch-pa-links
python main.py vendor run-all
python main.py consent run-all
python main.py request run-all --user-id <FLASK_USER_ID>

# individual stages:
python main.py <entity> extract
python main.py <entity> transform
python main.py <entity> load
python main.py consent enrich                        # by-id backfill (resumable)
```

## 3. Throughput knobs (env, per-run)

```bash
ENRICH_WORKERS=32 python main.py consent run-all     # faster extraction (watch Odoo 429/503)
LOAD_WORKERS=4    python main.py request run-all      # gentler writes on DB contention
MAX_RECORDS=50    python main.py consent extract      # test slice (sequential paging)
```

## 4. Reconcile (verify)

```bash
python main.py reconcile                              # offline, counts only, instant
python main.py reconcile --live --cached-source       # field-diff vs live Flask, no Odoo re-pull
python main.py reconcile --live                        # full: re-pull Odoo + verify live
less -R data/processed/reconciliation_report.txt
```

## 5. Operator tools (dpdp_python venv)

```bash
cd dpdp_python
./venv/bin/python -m migration_ext.ensure_license --tenant 1   # seed/raise licenses
./venv/bin/python -m migration_ext.seed_operator --tenant 1    # operator + fresh FLASK_API_KEY
./venv/bin/python -m migration_ext.reset                       # DRY RUN: show what a clean reset deletes
./venv/bin/python -m migration_ext.reset --yes --reset-licenses # execute clean reset
```

## Recover from common errors

| Symptom | Fix |
|---|---|
| every `/migration/*` load → **404** | app booted wrong; restart via `migration_ext.serve` |
| `No active license available` | run `ensure_license --tenant <id>` |
| **401** on loads | refresh `FLASK_API_KEY` (or `seed_operator` if the user was wiped) |
| extraction aborts "Token Expired" | refresh `ODOO_JWT_TOKEN`; re-run resumes from the enrich checkpoint |
| `Processing activity not found` on a few rows | blank source rows (no PA) — unmigratable, ignore |
