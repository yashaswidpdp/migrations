# 00 — Migration Logic: Overview

This folder documents the **`migration/` ETL project** — the standalone Python CLI
that pulls data out of a legacy **Odoo** instance and loads it into the modern
**Flask** app (`dpdp_python`). The backend-side glue it talks to is documented in
the sibling folder [`../02_dpdp_python_logic/`](../02_dpdp_python_logic/00-overview.md).

> Read these in number order. Each file is self-contained but assumes the
> overview (this file) and the config doc (`01-config-and-env.md`).

## What this project is

A classic **ETL** pipeline with three stages per entity, plus an audit:

```
 Odoo REST API ──Extract──▶ data/raw/*.csv|json
                              │
                              Transform (map Odoo schema → Flask schema)
                              ▼
                            data/processed/*.csv|json
                              │
                              Load (POST to Flask /api/migration/*)
                              ▼
                            Flask Postgres  ──Reconcile──▶ audit report
```

- **Extract** — `scripts/extract/extract_odoo.py`. Read-only GETs against Odoo.
- **Transform** — `scripts/transform/transform_*.py`. One module per entity; maps
  Odoo field names/enums to what Flask expects.
- **Load** — `scripts/load/load_flask.py`. POSTs each processed row to the Flask
  migration endpoints. Idempotent (re-runs skip already-migrated rows).
- **Reconcile** — `scripts/report/reconcile.py`. Proves how much landed and
  explains every record that didn't.
- **Orchestrate** — `main.py`. A `click` CLI exposing per-entity commands plus the
  full-pipeline `migrate-all`.

## Entities migrated

| Entity | Odoo source | Flask target | Module(s) |
|---|---|---|---|
| Request Type | `/request-types` | `/request-types/create` | `transform_request_type` |
| Stakeholder (PA Manager) | `/stakeholders` | `/migration/stakeholder` | `transform_stakeholder`, `stakeholder_*` |
| Processing Activity | `/processing_activities` | native `/processing/create` + `/migration/source-map` | `transform_processing_activity` |
| Template | `/v2/get/templates` | native `/notice-templates/create` + source-map | `transform_template` |
| Vendor | `/vendors_details` | `/migration/vendor` | `transform_vendor` |
| Consent (DPCM) | `/dpcm/dashboard` + `/dpcm/id` | `/migration/consent` | `transform_consent` |
| Request (DPGR) | `/dpgr/dashboard` + `/dpgr/id` | `/migration/request` | `transform_request` |

## Folder map (`migration/`)

```
migration/
├── main.py                     # CLI orchestrator (all commands + migrate-all)
├── config/                     # .env (secrets/knobs) + .env.example
├── scripts/
│   ├── extract/extract_odoo.py # OdooExtractor: pooled session, parallel pagination, resumable enrichment
│   ├── transform/              # transform_*.py — one per entity
│   ├── load/
│   │   ├── load_flask.py        # FlaskLoader — all loaders + sharded parallel writes
│   │   ├── stakeholder_role_mapper.py
│   │   └── stakeholder_report.py
│   ├── report/reconcile.py      # audit (Odoo vs Flask)
│   └── utils/
├── models/                     # local SQLAlchemy-ish dataclasses (reference only — see 08-models.md)
├── data/{raw,processed}/       # ETL artifacts (gitignored)
├── logs/migration.log          # append-only run log
└── docs/                       # ← you are here
```

## Dependency order (why order matters)

Some entities reference others, so load order is enforced (see
`06-orchestrator-main-cli.md` for how `migrate-all` encodes it):

1. **request-type** — consents & requests resolve `request_type_id` by name.
2. **stakeholder** — PA managers must exist before PAs reference them.
3. **processing-activity** — consents/requests/templates/vendors link to PAs.
4. **templates** (then PA↔template link backfill).
5. **vendors** — requests resolve `assignToVendor` by the vendor's name.
6. **consents** — revoke requests link to the consent they withdraw.
7. **requests** — last; depends on all of the above.

## Two cross-cutting properties

- **Idempotency** — every load is safe to re-run. Already-migrated rows return
  HTTP 409 and are skipped; failures are retried. Backed by the
  `migration_source_map` table (see `../02_dpdp_python_logic/04-source_map-idempotency.md`).
- **No side effects** — the migration sends **no emails, no OTP, no
  notifications**. This is the whole reason the backend `migration_ext` exists;
  the exact lines are in `../02_dpdp_python_logic/03-no-email-no-notification.md`.

## Prerequisites to run anything

1. Flask app booted via **`migration_ext.serve`** (not plain `app.py`) so the
   `/api/migration/*` routes exist.
2. **Licenses seeded** for the tenant (`migration_ext.ensure_license`).
3. Fresh `ODOO_JWT_TOKEN` + `FLASK_API_KEY` in `config/.env`.

See `01-config-and-env.md` and the top-level `migration_runbook.md`.
