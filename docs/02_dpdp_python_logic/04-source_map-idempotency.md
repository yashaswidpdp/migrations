# 04 — Idempotency: `source_map.py`

`migration_ext/source_map.py` owns the **`migration_source_map`** table — the
ledger that makes every load idempotent and the whole migration auditable, without
adding an `odoo_source_id` column to the real `requests`/`consents`/etc. tables
(keeping the upstream schema untouched).

## The table — `MigrationSourceMap`

| Column | Meaning |
|---|---|
| `entity` | `request` \| `consent` \| `stakeholder` \| `vendor` \| `processing_activity` \| `template` |
| `odoo_source_id` | the Odoo record id |
| `sub_key` | discriminator for **fan-out** (one Odoo source → many Flask rows). `""` for 1:1 entities. |
| `flask_id` | the Flask row this source produced |
| `tenant_id` | tenant scope |
| `created_at` | when recorded |

**Unique constraint:** `(entity, odoo_source_id, sub_key, tenant_id)` —
`uq_migration_source_entity_odoo_sub_tenant`.

### Why `sub_key`
A single Odoo template fans out to many Flask notice/email rows. Each is recorded
under the **same `odoo_source_id`** but a **distinct `sub_key`** (e.g.
`"Live Consent Template|Email|English"`), so the ledger can represent the 1-to-many
relationship. The 1:1 entities (request, consent, stakeholder, vendor) use the
empty sub_key.

## Helpers

| Method | Use |
|---|---|
| `existing(entity, odoo_id, tenant, sub_key="")` | the prior mapping for this source (+ sub_key), or `None` — the idempotency check every endpoint runs first |
| `existing_any(entity, odoo_id, tenant)` | all mappings for a source id across every sub_key (fan-out aware) |
| `record(entity, odoo_id, flask_id, tenant, sub_key="")` | persist a new mapping (no commit — caller commits) |

## How idempotency works at the endpoints

Each handler, before doing work:
```python
if MigrationSourceMap.existing("<entity>", odoo_source_id, tenant_id):
    return api_response("error", "... already migrated", {}, 409)
```
So a re-run of the loader gets **409** for already-migrated rows; the loader counts
those as success/skip and retries only genuine failures. After a successful create,
the handler calls `MigrationSourceMap.record(...)` and commits.

For PA/template (created via native routes), the loader posts the mapping to
`POST /api/migration/source-map` afterward (see `02-routes-migration-endpoints.md`),
so those entities are equally idempotent/auditable.

## `ensure_source_map_table()` — safe on every boot

Called by `register_migration` on startup. It:
1. `create(checkfirst=True)` — creates the table if missing.
2. Idempotently migrates an **older** table to the fan-out shape: `ADD COLUMN IF
   NOT EXISTS sub_key`, drops the old 3-col unique constraint, and adds the new
   4-col one (guarded by a `pg_constraint` catalog lookup, since Postgres lacks
   `ADD CONSTRAINT IF NOT EXISTS`).

Every step is wrapped so it tolerates non-Postgres engines and already-migrated
tables — booting never fails because of this.

## Operational notes
- The reconciler reads MIGRATED counts straight from this table (see
  `../01_migration_logic/07-reconcile-report.md`).
- A DB reset must also clear `migration_source_map` (the `reset.py` tool does), or
  dedup would block a fresh re-run. The table is **not** in alembic, so a schema
  reset wipes it — it's recreated on the next boot via `ensure_source_map_table`.
