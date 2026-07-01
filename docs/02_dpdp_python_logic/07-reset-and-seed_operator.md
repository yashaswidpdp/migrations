# 07 — Operator tools: `reset.py` & `seed_operator.py`

Two standalone CLIs used around a migration run (not part of the request path).
Both run under the dpdp_python interpreter and read DB/JWT creds from `.env`.

## `migration_ext/reset.py` — wipe migrated data to a clean baseline

For **repeated migration testing**: tears down everything the pipeline creates so
it can be re-run from scratch, while keeping seed/admin rows.

```bash
python -m migration_ext.reset                 # DRY RUN (default) — prints, changes nothing
python -m migration_ext.reset --yes           # execute (single txn; rolls back on error)
python -m migration_ext.reset --yes --reset-licenses
python -m migration_ext.reset --keep-users 1,2,3 --keep-pa-max 11 --keep-rt-max 11 --yes
```

**Keeps by default:** users `id ∈ {1,2,3}`, processing activities `id 1..11`.
**Deletes:** all consents; all requests (+ child rows); **all** request types
(default `--keep-rt-max 0` — re-migration recreates them from Odoo); PAs outside
the keep range; users outside the keep set (+ child rows); **all
`migration_source_map` rows** (so dedup doesn't block a re-run); all vendors;
optionally consumed-license logs + reset `licenses.used_users` (`--reset-licenses`).

**Safety:** dry-run by default — you must pass `--yes` to delete. The deletion is a
single transaction that rolls back on any error. Connects with `psycopg2` using
`DB_*` env vars.

> After a reset, re-seed licenses (`ensure_license`) and re-run the load. The
> source-map and licenses are **not** in alembic, so they must be re-established.

## `migration_ext/seed_operator.py` — operator user + API token

The loader authenticates as a **backend user of the target tenant**. If that user
is missing (e.g. wiped by an over-broad reset), every API call returns 401
"Authentication required" even though the token signature is valid — because the
identity behind it no longer exists.

```bash
python -m migration_ext.seed_operator --tenant 3
python -m migration_ext.seed_operator --tenant 1 --email migration-operator@… --days 30
```

It ensures a **Backend DPO** operator exists for the tenant and prints a fresh
access token to paste into `migration/config/.env` as `FLASK_API_KEY`. A DPO
bypasses the RBAC gate (`utils/require_permission`) and is resolvable as a PA
manager, so PA/consent/request loading all work.

| Flag | Default | Meaning |
|---|---|---|
| `--tenant` | 3 | target tenant id |
| `--email` | `migration-operator@<tenant domain>` | operator email |
| `--days` | 30 | token lifetime |

> `SECRET_KEY` is only needed to build the app; JWT signing uses `JWT_SECRET_KEY`
> from `.env`.

## When you reach for these
- **401 on every load** → `seed_operator` (operator missing) or simply refresh
  `FLASK_API_KEY`.
- **Want a clean re-migration** → `reset --yes` → `ensure_license` → re-run the
  load. Use this when a table (e.g. templates) has accumulated duplicates across
  repeated runs and you want a trustworthy fresh pass.
