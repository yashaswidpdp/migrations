# 00 — dpdp_python Logic (`migration_ext`): Overview

This folder documents the **backend glue** that lives in
`dpdp_python/migration_ext/`. It is the server-side counterpart to the
`migration/` ETL project (`../01_migration_logic/`). The ETL loader POSTs to the
endpoints defined here.

> Files are numbered; read in order. `03-no-email-no-notification.md` is the
> centerpiece — it pins down, line by line, how the migration creates real
> records while sending **no emails, OTP, or notifications**.

## The core idea: an *extension*, not an edit

`migration_ext` is a package that does **not exist upstream**. All migration-
specific backend behavior lives here instead of editing the real
`models/`, `routes/`, or `services/`. Why:

- A `git pull` of upstream never touches these files, so migration patches can't be
  silently overwritten (this was a real past failure — Issue 26).
- The live app is unaffected: the migration routes only exist when you boot via
  `migration_ext.serve`. Normal `flask run` / `gunicorn app:app` doesn't load them.

## What's in the package

| File | Role | Doc |
|---|---|---|
| `__init__.py` | `register_migration(app)` — build & mount the `/api/migration` blueprint, ensure the source-map table | `01` |
| `serve.py` | standalone entrypoint: `create_app()` + `register_migration()` + a runtime template-validator patch | `01` |
| `routes.py` | the migration endpoints: request, consent, stakeholder, vendor, source-map, ping | `02`, `03` |
| `source_map.py` | `migration_source_map` table + helpers (idempotency) | `04` |
| `ensure_license.py` | seed/raise license capacity before a load | `05` |
| `attachments/` | decode + store vendor NDA/VRA documents from inline Base64 | `06` |
| `reset.py` | wipe migrated data to a clean baseline (test reset) | `07` |
| `seed_operator.py` | create a migration operator + mint its API token | `07` |

## The endpoints (all under `/api/migration`)

| Method + path | Handler | Purpose |
|---|---|---|
| `POST /request` | `migrate_request` | create a DPGR request (reuses core create, no email) |
| `POST /consent` | `migrate_consent` | direct Consent insert (no template/email) |
| `POST /stakeholder` | `migrate_stakeholder` | create a PA-manager backend user (no welcome/reset mail) |
| `POST /vendor` | `migrate_vendor` | create a Vendor + contact user (no invite mail) |
| `POST /source-map` | `migrate_source_map` | ledger ids for entities created via native routes (PA/template) |
| `GET /ping` | `migrate_ping` | liveness check |

## How the two halves connect

```
migration/ (ETL, its own venv)            dpdp_python/ (Flask app, its own venv)
  FlaskLoader  ──POST /api/migration/*──▶  migration_ext.routes  ──▶ real models/services
                                            (below the email/OTP layer)
```

Auth/tenant work exactly like the real routes: **JWT** for identity, **`Host`
header → `g.tenant`** for the tenant (resolved by the app's `before_request` hook).

## Boot contract (critical)

The routes exist **only** when the app is started through `migration_ext.serve`:

```bash
cd dpdp_python
./venv/bin/python -m migration_ext.serve          # dev, port 5000
# or: ./venv/bin/gunicorn 'migration_ext.serve:app' -b 0.0.0.0:5000
```

Booting plain `python app.py` / `gunicorn app:app` gives a working app whose
`/api/migration/*` routes return **404** — the #1 "loads all 404" gotcha.
