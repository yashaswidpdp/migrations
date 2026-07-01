# 02 — `routes.py`: the migration endpoints

`migration_ext/routes.py` defines every `/api/migration/*` handler. All are
`@jwt_required()` and resolve the tenant from `g.tenant` (set by the app's
`before_request` Host-header hook) via `_tenant_id()`. All are idempotent via
`MigrationSourceMap` and forward Odoo source dates verbatim.

> The "how it avoids emails/OTP/notifications" angle is in
> `03-no-email-no-notification.md`. This file covers what each endpoint *does*.

## Shared helpers (top of file)
- `_parse_date(value)` — parse an optional migration date (`ISO`, `dd/mm/YYYY`,
  `YYYY-MM-DD[ HH:MM:SS]`) to a tz-aware UTC datetime, else `None`. Blank/garbage →
  `None` (caller keeps its own default).
- `_parse_source_date(raw, field, source_id)` — `_parse_date` + **fidelity
  logging**: logs (never fabricates) when a business date is unparseable/absent, so
  a missing value is auditable rather than silently defaulted.
- `_tenant_id()` — `g.tenant.id` or `None`.

## `POST /request` → `migrate_request` (routes.py:97)
Reuses the core `Request.create_request(payload, tenant_id, **extra_dates)`
(routes.py:175) — the same model method the live route uses — but assembles
`extra_dates` to forward Odoo chronology and sets several fields post-insert:
- **Date fidelity:** `resolution_date`, `closed_on`, `raised_on→created_at`,
  `escalated_date`, `updated_at` are forced to naive-UTC and passed through; some
  are set *after* insert to beat Python column defaults / `onupdate=now()`.
- **`risk`** (routes.py:204) and **`rag_status`** are set post-insert because the
  column default `"Low"` would otherwise fire on `None`.
- **Initial assignment track** realigned to the Odoo creation date (routes.py:198).
- **Principal-name reconciliation** (routes.py:214): the request's Odoo name is
  treated as authoritative and overwrites the resolved principal's name (guarded:
  only when non-empty and different; logged old→new).
- **request ↔ PA M2M** (routes.py:232): resolves `processing_activity` ids
  (tenant-scoped) and populates the M2M that `create_request` doesn't.
- **Revoke linking** (routes.py:262): resolves `consent_source_ids` via the
  source-map and sets `consent.is_revoke`/`request_id` (consents must load first;
  unresolved ids are returned in `unlinked_consent_source_ids`).
- **Vendor link** (routes.py:284): resolves `assigned_vendor_names` against
  `Vendor.company_name` (fallback contact `User.name`), appends the M2M, and creates
  a `VendorActivity` (with the Odoo `request_date`). Unresolved → `unlinked_vendor_names`.
- **`action_date`** (routes.py:372): an Odoo field with no mapped ORM column —
  written by raw SQL inside a **savepoint** (`begin_nested`) so a missing column
  aborts only that nested txn, not the whole insert.
- Records the source-map and commits (routes.py:389-391).

## `POST /consent` → `migrate_consent` (routes.py:439)
A **direct `Consent(...)` insert** (routes.py:533) — it does NOT go through the
consent service/template/email machinery a historical backfill doesn't need.
- Requires a resolvable PA (routes.py:453); else 400 `Processing activity not found`.
- **Identity:** `resolve_principal(email, phone, tenant)`; if none exists, creates a
  `DataPrincipal` user directly and `consume_license(... "DPCM" ...)` (routes.py:483).
- **Status/lifecycle/decision** mapped from the Odoo status.
- **Dates (G1–G5):** every business date comes from the Odoo source; absent values
  are forced back to NULL post-insert so the column defaults (`now()`) can't
  fabricate them. `closed_on` only for terminal statuses; `updated_at` set last so
  it beats `onupdate=now()`.
- Records the source-map and commits.

## `POST /stakeholder` → `migrate_stakeholder` (routes.py:597)
Creates a **Backend `PAManager`** user (or reuses an existing tenant user with the
same email/phone — idempotent across re-runs). Issues a reset token so the manager
can set a password later, but **sends no mail**. Maps `role_ids` if provided.
Returns the Flask `user.id` so the loader can resolve PA `manager_ids`.

## `POST /vendor` → `migrate_vendor` (routes.py:682)
Creates a Vendor + its "Vendor" contact user via the shared
`_resolve_or_create_vendor_user` (role "Vendor", consumes a `DPTPA` license for new
users) and `Vendor.create_vendor`, **with no questionnaire/invite email**. Decodes
NDA/VRA attachments via the attachments subsystem (all-or-nothing). Sets
`risk_level` post-insert so a `None` doesn't default to "Low".

## `POST /source-map` → `migrate_source_map` (routes.py:834)
Ledger endpoint for entities created via the **native** routes (processing
activity, template) whose create logic is too large to mirror here. The loader
creates them through the real routes (which return the new id), then posts the
mapping here. Supports a **batch** body (`{"records":[...]}`) — preferred for
template fan-out (one Odoo template → many Flask rows under distinct `sub_key`s).
`_record_one` is idempotent: identical prior mapping = no-op 200; a different
`flask_id` = 409.

## `GET /ping` → `migrate_ping`
Returns `migration_ext alive` (no auth) — quick liveness check after boot.
