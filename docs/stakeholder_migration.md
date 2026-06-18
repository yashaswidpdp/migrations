# Internal Stakeholder Migration (Odoo → Flask)

ETL for internal stakeholders, mirroring the vendor/request/consent pipelines.

## Pipeline

```
python main.py stakeholder extract     # GET /api/stakeholders -> data/raw/raw_stakeholders.json
python main.py stakeholder transform   # -> data/processed/processed_stakeholders.csv
python main.py stakeholder load        # -> POST /api/migration/stakeholder (email-free, role-name mapped)
python main.py stakeholder run-all      # all three
```

Files:
- `scripts/extract/extract_odoo.py` — `run_stakeholder_extraction()`
- `scripts/transform/transform_stakeholder.py` — `transform_stakeholder_data()`
- `scripts/load/load_flask.py` — `load_stakeholders()` / `run_stakeholder_loading()`
- `scripts/load/stakeholder_role_mapper.py` — name→id role mapping
- `scripts/load/stakeholder_report.py` — per-record log + CSV/JSON summary

## Source — Odoo `GET /api/stakeholders`

Non-paginated, single shot. Envelope:

```json
{"status":"success","recordType":"Internal Stakeholders",
 "totalInternalStakeholders":8,"stakeholders":[ ... ]}
```

Per record: `id, name, login (email), phone (false when empty), is_active,
role_ids:[{id,name,...}]`.

Findings (verified against live data, 8 records):
- All stakeholders active. `phone` is usually `false`; one real number.
- Role names seen: only `DPO` and `PA Manager`.
- The same role NAME carries several Odoo ids (DPO = 4, 5, 9). One user can even
  list the same name twice (user 6 has two `DPO`). → **map by name, then dedup.**
- No duplicate emails in the sample, but the loader handles them anyway.

## Target — Flask `POST /api/migration/stakeholder` (email-free)

The loader targets the **migration extension** endpoint, NOT the public
`/api/stakeholder/create`, because a historical backfill must not email real
users. `migration_ext/routes.py::migrate_stakeholder` creates a Backend
PAManager user with **no outbound communication** and is idempotent.

Side-effect comparison (traced from source):

| Side effect | public `/stakeholder/create` | `/migration/stakeholder` |
|---|---|---|
| welcome/credential email | yes (`send_email_sync`, synchronous) | **none** |
| SMTP required | yes (500 if unset) | not required |
| password generated | hash only (not emailed) | hash only |
| reset token | DB column | DB column, **no mail** |
| OTP | none | none |
| notifications / in-app | none | none |
| Celery / background jobs | none | none |
| audit log (after_request) | yes (internal) | yes (internal) |
| idempotency | none (400 on dup) | **source-map 409** |

Migration endpoint behaviour:
- **Required:** `name`, `email`. `phone`, `role_ids`, `odoo_source_id` optional.
- **Idempotent:** prior `MigrationSourceMap` (entity `stakeholder`) → 409
  "already migrated"; an existing tenant user with the same email/phone is
  reused and mapped (`created=false`) instead of erroring.
- **Roles:** `role_ids` filtered by tenant; empty allowed.
- **`active`:** always created Active (same as the public route); Odoo
  `is_active` is not applied — all source records are active anyway.
- Success → `201 {data:{id, created}}`.

The public `POST /api/stakeholder/create` (name+email required, phone =
valid-Indian-mobile, dup → 400, sends welcome email) and
`PUT /api/stakeholder/<id>/update-roles` remain unchanged for normal UI use.

## Transform mapping

| Odoo | Flask CSV | Notes |
|------|-----------|-------|
| `name` | `name` | |
| `login` | `email` | lower-cased |
| `phone` | `phone` | `false` → `''` (never a boolean) |
| `is_active` | `is_active` | create can't set it; loader patches only when False |
| `role_ids[].name` | `role_names` | deduped JSON list of NAMES; ids dropped |
| `id` | `odoo_source_id` | audit/dedup key, not sent to Flask |

## Role mapping (the critical part)

Odoo and Flask role ids differ, so ids are **never** carried. `role_names` are
resolved at load time against the live Flask catalogue
(`GET /api/roles/details`, tenant-scoped, `is_system=False`), case-insensitively,
then deduped.

**Unmapped role → the stakeholder fails (logged), migration continues.**

### Known gap

The live Flask tenant currently exposes a single non-system role —
**`Full Access` (id 2)**. It has **no `DPO` / `PA Manager`**, so every Odoo
stakeholder would fail role mapping as-is. Resolve by either:

1. Creating matching roles in Flask (`POST /api/roles/create`), or
2. Supplying an alias file `data/stakeholder_role_aliases.json`
   (override path with `STAKEHOLDER_ROLE_ALIAS_FILE`):

   ```json
   { "DPO": "Full Access", "PA Manager": "Full Access" }
   ```

   Aliases map an Odoo role name onto an existing Flask role name before lookup.
   No ids are ever hardcoded.

## Duplicate / load behaviour

Per stakeholder:
1. Missing email → **FAILED** (skip + log), continue.
2. Resolve roles; any unmapped → **FAILED** (logs the unmapped names), continue.
3. POST `/migration/stakeholder` (email-free):
   - `201 created=true` → **CREATED**
   - `201 created=false` → **UPDATED** (existing user reused/mapped)
   - `409` → **SKIPPED** (source-map says already migrated)
   - other → **FAILED**

Every record yields a report row. Output:
`data/processed/report_processed_stakeholders.csv` + `.json`, plus
`[SUCCESS]/[SKIPPED]/[FAILED]` blocks in `logs/migration.log`. A single failure
never aborts the run.

## Edge cases

| Case | Behaviour |
|------|-----------|
| `login: false` (no email) | FAILED, logged, continue |
| `phone: false` | created (phone omitted) |
| multiple roles | all mapped + assigned |
| duplicate role name (e.g. two `DPO`) | deduped to one |
| unknown role | FAILED, unmapped role logged, continue |
| duplicate email | UPDATED (or SKIPPED if lookup misses) |
