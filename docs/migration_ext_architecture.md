# `migration_ext` — Migration Subsystem Architecture & Notification-Suppression Analysis

**Status:** Reverse-engineered design document (no code changed)
**Scope:** `dpdp_python/migration_ext/` and every core module it reuses or bypasses
**Audience:** Senior backend engineers onboarding to the Odoo → Flask migration

> Reconstruction note: the working tree currently ships only compiled
> `*.pyc` bytecode for `migration_ext` (the `.py` sources were not present at
> the time of writing). This document was reconstructed from the bytecode
> (imports, call graph, string/const tables) plus the live core modules the
> extension imports. Behaviour described here is what the bytecode does; line
> numbers refer to core files that still have sources.

---

## 1. Executive Summary

`migration_ext` is a **glue layer** bolted onto the `dpdp_python` Flask app to
load historical Odoo data into the Flask database **without editing a single
upstream file** and **without firing any user-visible side effect** (emails,
OTPs, SMS, vendor invites, notifications, or background jobs).

It achieves this with one structural decision that drives everything else:

> **Side effects in this codebase live in the *route* and *service* layer, not
> the *model* layer.** `migration_ext` re-enters the application *below* the
> side-effect layer — it calls model constructors (`Request.create_request`),
> shared user/vendor helpers, and in some cases issues direct ORM inserts — so
> the email/OTP/SMS/invite code in the routes is never on the call path.

Everything else (idempotency, tenant-awareness, historical timestamp fidelity,
license headroom) is built around preserving that property while still
producing rows that are byte-for-byte indistinguishable from rows a real user
would have created.

The extension is wired in at boot by `register_migration(app)` after the normal
`create_app()`, exposing a blueprint at `/api/migration/*`. A separate Python
CLI (`migration/main.py` + `scripts/load/load_flask.py`) is the client that
drives those endpoints.

---

## 2. Why `migration_ext` Exists

### 2.1 The problem

The migration must create real `requests`, `consents`, `processing_activity`,
`vendors`, users, request-types and templates in the Flask DB, with their
**original Odoo timestamps and relationships**. But the normal create paths:

- send welcome / credential / questionnaire / request-created emails,
- require OTP verification for data-principal actions,
- send SMS for consent,
- enforce *current* license seat limits (a historical backfill must not be
  blocked by today's license state),
- resolve and render notice templates,
- reject legacy Odoo template placeholders (`{var1}`, `{var2}`…).

A historical backfill must do **none** of those user-facing things — you cannot
email 600 real data principals "please re-consent" three years after the fact.

### 2.2 Why an extension and not a core edit

The original approach patched the real routes/models. Those patches were
**silently overwritten by `git pull`** of upstream (see Issue 26, quoted in
`migration_ext/__init__.py`). The fix: put *all* migration-specific behaviour in
files that **do not exist upstream**, so a pull can never touch them.

This is recorded in project memory as the **migration extension pattern**:
backend glue lives in `dpdp_python/migration_ext`, never in-place edits, so it
survives `git pull`.

### 2.3 Why this is maintainable

- **Zero merge surface.** Upstream and migration code never occupy the same
  file, so there are no merge conflicts and no risk of a pull reverting a fix.
- **Reuse over reimplementation.** Where a core helper has no side effects
  (`Request.create_request`, `_resolve_or_create_vendor_user`,
  `Vendor.create_vendor`, `consume_license`, `upload_file`,
  `resolve_principal`), the extension calls it directly, so behaviour
  (roles, license accounting, file naming, identity dedup) matches the real app
  exactly.
- **Schema stays clean.** Idempotency is owned by a migration-private table
  (`migration_source_map`), so `requests`/`consents` need no `odoo_source_id`
  column.

The two monkey-patches that *do* reach into core (`_patch_template_variable_whitelist`
in `serve.py`, and the `action_date` raw `UPDATE` in `migrate_request`) are done
**at runtime from extension code**, not by editing the core file — same
git-survival property.

---

## 3. Architecture Overview

```
                       ┌─────────────────────────────────────────────┐
                       │  Odoo (source system, READ-ONLY to us)       │
                       └───────────────────────┬─────────────────────┘
                                               │  REST GET only
                                               ▼
   migration/ (separate venv, the CLI client)
   ┌───────────────────────────────────────────────────────────────┐
   │  main.py (click CLI)                                            │
   │     │                                                           │
   │     ▼                                                           │
   │  scripts/extract/extract_odoo.py     → data/raw/*.json         │
   │     ▼                                                           │
   │  scripts/transform/transform_*.py    → data/processed/*.csv    │
   │     ▼                                                           │
   │  scripts/load/load_flask.py (FlaskLoader)  ── HTTP POST ──┐    │
   └───────────────────────────────────────────────────────────┼────┘
                                                                │
            JWT (identity) + Host header (→ g.tenant)           │
                                                                ▼
   dpdp_python/ (the real Flask app, booted via migration_ext.serve)
   ┌───────────────────────────────────────────────────────────────┐
   │  create_app()  ── normal app, before_request resolves tenant   │
   │        + register_migration(app)                               │
   │                                                                │
   │  migration_ext blueprint  /api/migration/*                     │
   │    routes.py ──┬─ migrate_request   → Request.create_request   │
   │                ├─ migrate_consent    → direct Consent insert    │
   │                ├─ migrate_stakeholder→ direct User insert       │
   │                ├─ migrate_vendor     → create_vendor + helper   │
   │                ├─ migrate_source_map → idempotency bookkeeping  │
   │                └─ migrate_ping                                  │
   │                                                                │
   │    source_map.py   MigrationSourceMap  (dedup table)           │
   │    attachments/    decode→validate→upload (reuses upload_file)  │
   │                                                                │
   │    ↓ (model / service layer — NO email/otp/sms here)           │
   └───────────────────────────────┬───────────────────────────────┘
                                    ▼
                       ┌──────────────────────────┐
                       │  Flask Postgres database  │
                       └──────────────────────────┘
```

### Stage-by-stage

| Stage | Where | What |
|---|---|---|
| **Extract** | `scripts/extract/extract_odoo.py` | GET from Odoo → `data/raw/*.json`. Strictly read-only against Odoo. |
| **Transform** | `scripts/transform/transform_*.py` | Normalize → `data/processed/*.csv` (dates, phones-as-str, type mapping). |
| **Load** | `scripts/load/load_flask.py` | `FlaskLoader` POSTs each row to `/api/migration/*`. |
| **migration_ext** | `dpdp_python/migration_ext/routes.py` | Validates, dedups, creates rows **below** the side-effect layer, records the source map, commits. |
| **DB** | Postgres | Rows land identical to UI-created rows, minus the notifications. |

---

## 4. Entity-by-Entity Migration Flow

All endpoints share the same envelope:

1. `request.get_json(silent=True)` → payload.
2. `_tenant_id()` = `getattr(g, "tenant").id`, resolved by the **app's normal
   `before_request` hook** from the `Host` header. JWT (`@jwt_required` on
   `migrate_request`) supplies identity. → Migration uses the *exact same*
   auth/tenant mechanism as real routes; no bypass there.
3. **Idempotency pre-check** via `MigrationSourceMap.existing(...)` → `409 …
   already migrated` if seen before.
4. Build the row(s) below the side-effect layer.
5. `MigrationSourceMap.record(...)` then `db.session.commit()`.
6. `api_response(...)` with the new Flask id(s).

### 4.1 Request — `POST /api/migration/request` (`migrate_request`)

`python main.py request load` → `run_loading("processed_requests.csv",
"/migration/request")` → `FlaskLoader.load_from_csv`, one POST per row.

Server flow (`migrate_request`):

1. Tenant check; `409` if `request` `odoo_source_id` already mapped.
2. Resolve `request_type_id` → `RequestType`; `400 Invalid request type` if absent.
3. **Reuse core**: `Request.create_request(payload, tenant_id, **extra_fields)`.
   This is a *model* method — it builds the Request + `_get_or_create_user`
   (which consumes a license seat for a new principal via
   `request_helpers.consume_license`) but **sends no email and runs no OTP**.
   `IdentityConflict`/`ValueError` → `rollback` + error response.
4. **Forward Odoo dates** instead of letting the model default them:
   `raised_on/created_at`, `closed_on`, `resolution_date`, plus `rag_status`,
   `risk`, `escalated_date`, `escalated_comment`, `is_escalated`, `ip_address`,
   `device_type`. Parsed by `_parse_date` (ISO **and** `dd/mm/YYYY`) and
   `_naive_utc`.
5. **Principal name authority**: the request's Odoo name overrides the user row
   name, logged as `principal id=… name … -> … (request Odoo name authoritative)`.
6. **Relationship restoration:**
   - **Processing activities** — `ast.literal_eval` the id list, filter to
     tenant; PAs not in tenant are logged and skipped (`processing_activity id(s)
     … not in tenant …; not linked`), the row is **not** dropped.
   - **Consents** — `consent_source_ids` resolved through `MigrationSourceMap`
     to existing Flask consents; on a revoke request, history row
     `REVOKE_REQUEST_CREATED` is appended (this is why consents must load
     *before* revoke requests).
   - **Vendors** — `assigned_vendor_names` matched case-insensitively against
     `Vendor`/`User`; unknown vendor contacts are **skipped with a log**
     (`vendor contact '…' not found … skipping vendor activity`), and a
     `VendorActivity` (`state='initiated'`, `is_read=False`) is created for
     matches. Unlinked names are returned to the loader.
7. `action_date` is set via a **raw `UPDATE requests SET action_date`** (the
   column lives outside Alembic; see schema-changes memory). Failure is logged,
   not fatal.
8. `commit`; return `id`, `request_no`, `unlinked_consent_source_ids`,
   `unlinked_vendor_names`.

### 4.2 Consent — `POST /api/migration/consent` (`migrate_consent`)

`python main.py consent load` → `run_consent_migration_loading` →
`FlaskLoader.load_consents_via_migration`.

> Note: consent `run-all` delete-reloads consents; Flask ids are unstable across
> runs, so reconciliation is only valid *after* a completed load (consent
> run-all memory).

Server flow (`migrate_consent`) — **direct insert, no route, no template, no email**:

1. Tenant check; `409` on duplicate `consent` source id.
2. Resolve `ProcessingActivity` by `(id, tenant_id)`; `400` if missing.
3. **User creation / dedup**: `resolve_principal` + `canonical_email` /
   `canonical_phone` to find an existing user; if none, build a `User`
   (`user_type='Portal'`, `user_role_type='DataPrincipal'`), `set_password(
   generate_random_password())` (a random unusable password — **no reset email**),
   `flush`, then `consume_license(tenant_id, 'DPCM', user_id)` for a seat. The
   random password + no email is deliberate: the principal never gets login mail.
4. **Status/lifecycle mapping**: `_enum_by_value` over `ConsentStatus`,
   `_LIFECYCLE_BY_STATUS`, `ConsentLifecycleEnum.LEGACY`, etc. (see consent-type
   mapping memory).
5. **Date fidelity**: every Odoo date (`consent_date`, `sent_on`,
   `delivered_on`, `created_on`, `valid_till`, `consent_reject_on`, `closed_on`,
   `last_updated`) is forwarded via `_parse_date` / `_parse_source_date`. The
   latter **logs** when a source date is unparseable/absent
   (`storing no value`) rather than fabricating one. `updated_at` falls back to
   insert time only when no source date exists, and that fallback is logged.
6. Build `Consent` directly with `ConsentTypeEnum.DIGITAL`,
   `LegacyTypeEnum`, `ProcessingTypeEnum.MANDATORY`, `generate_unique_id`,
   `generate_artifact_no`. `commit`; return `id`.

### 4.3 Stakeholder — `POST /api/migration/stakeholder` (`migrate_stakeholder`)

`python main.py stakeholder load` → `FlaskLoader.load_stakeholders`.

Creates a **Backend PA-Manager user with no welcome/credential email**. Mirrors
`POST /stakeholder/create` but drops the SMTP requirement, welcome email, and
reset-link mail. Reuses an existing same-email tenant user instead of erroring
(re-runs converge to one row). Returns the Flask user id so the loader can
resolve `manager_ids` for `/processing/create`. Idempotent via source map
(entity `stakeholder`). (See stakeholder-migration-endpoint memory.)

### 4.4 Vendor — `POST /api/migration/vendor` (`migrate_vendor`)

`python main.py vendor load` → `FlaskLoader.load_vendors`.

1. Tenant check; `409` on duplicate `vendor` source id.
2. **Vendor contact user**: reuses `services.vendor_service._resolve_or_create_vendor_user`
   — the *shared* helper that assigns role `Vendor` and consumes a DPTPA license
   for new users **but sends no email** (the real vendor route emits the invite
   separately via `_send_vendor_invite` / `send_vendor_questionnaire_email`,
   which migration never calls).
3. **Vendor row**: `Vendor.create_vendor(...)` with dates parsed by
   `_parse_date_only`, defaults `status='Active'`, `vra_status='Pending'`,
   `risk_level='Low'`.
4. **PA assignment**: `assign_processing_activities` for the vendor↔PA M2M.
5. **Attachments**: `process_vendor_attachments(payload, resource_id=vendor.id,
   state_at_upload=vendor.status)` (see §4.7). All-or-nothing — a bad attachment
   raises `MigrationAttachmentError` → `rollback` → `400`.
6. `commit`; return vendor id, user id, and stored doc paths.

> Vendor↔Request linkage: there is no vendor↔request relation in the Odoo
> source, so `vendor_activities`/`request_assigned_vendor` populated from the
> request path are empty by design, not data loss (vendor-request-no-linkage
> memory).

### 4.5 Processing Activity

PAs are loaded by `FlaskLoader.load_processing_activities` against the **native**
`/processing/create` route (not a `migration_ext` endpoint — PA creation has no
problematic side effects), then their Odoo→Flask ids are recorded back through
`POST /api/migration/source-map`. Hierarchy (`parent_id`), department mapping and
manager links are resolved by the loader using `_resolve_user_ids` /
`_resolve_pa_ids`.

> `_resolve_pa_ids` originally read page 1 only and used a `/simple` endpoint
> that excluded inactive PAs, causing consent PA-not-found and template/request
> link gaps; fixed to walk all pages and use the full endpoint
> (pa-resolver-pagination memory).

### 4.6 Template — fan-out

One Odoo template can carry **multiple language variants**, and each becomes a
**separate Flask `NoticeTemplate` row**. The loader creates each row via the
native template route, then records **one source-map entry per Flask row** using
a distinct `sub_key` (the language) — which is exactly why `MigrationSourceMap`
has a `sub_key` column and `migrate_source_map` supports **batch** mode
(`{"records":[…]}`) in a single transaction.

`serve._patch_template_variable_whitelist` runtime-patches
`routes/notice_template/crud.validate_template_variables` so legacy Odoo
placeholders (`{var1}`, `{var2}`, …, regex `^var\d+$`) pass validation that
upstream would reject — done from extension code so it survives `git pull`.

Load order matters: PAs must exist before templates so links resolve; then
`patch_template_pa_links` / `approve_templates` wire and activate them (see
template-pa-loadorder memory).

### 4.7 Attachments pipeline (`migration_ext/attachments/`)

`process_vendor_attachments` orchestrates **decode → validate → upload → map**:

- `validators.validate_entry` — shape check `{fileName, fileContent}`.
- `decoder.decode_attachment` — strict-then-lenient Base64 decode, **mime
  sniffed from byte magic** (`MAGIC_SIGNATURES`) not the extension, wrapped in a
  Werkzeug `FileStorage`; `secure_filename` applied.
- `validators.validate_decoded` / `validate_ext` — non-empty, ≤ 10 MiB
  (`MAX_ATTACHMENT_BYTES`), allowed extension.
- `mapper.vendor_target` — Odoo field → (Vendor column, folder), e.g.
  `nda_attachment → (nda_document, uploads/vendors)`.
- `uploader.store` — **reuses `utils.file_upload.upload_file`** so the stored
  file gets the identical uuid/standardized name (`vendor_<id>_<doc>_<state>_…`)
  and traversal/size guards as a UI upload. There is deliberately **no
  migration-only storage path**.

---

## 5. Notification & Email Suppression Analysis

This is the core guarantee. The investigation method: for each side effect,
locate where it fires in the **normal** flow, then confirm the migration call
path never reaches it.

### 5.1 The structural reason (read this first)

In `dpdp_python`, side effects are emitted by the **route handlers and the
service layer they call**, *not* by the model constructors:

- `Request.create_request` (`models/request.py:297`) builds the Request and (via
  `services/request_helpers.py:427 _get_or_create_user`) the principal +
  license seat. **It sends nothing.** The request-created email is sent by the
  *route* `routes/request/crud.py create_request`, *after* the model call,
  guarded by `auto_send_email` (`send_request_create_email`).
- OTP verification (`verify_context_otp`) is enforced in the *route*, gated on
  `otp_required` / `nominee_otp_required` flags in the payload.
- Consent emails/SMS (`send_email_sync`, `send_sms`, `_resolve_template`) live in
  `routes/consent/crud.py`.
- Vendor invite/questionnaire emails (`_send_vendor_invite`,
  `send_vendor_questionnaire_email`) live in `routes/vendor/crud.py` /
  `services/vendor_service.py`, **separate** from the user-creation helper.

`migration_ext` re-enters the app at the **model/service level**, beneath every
one of those. That single fact is why nothing fires.

### 5.2 Emails — none sent

| Email | Normal trigger | Why migration skips it |
|---|---|---|
| Request created | `routes/request/crud.py` after `create_request`, `if auto_send_email` | `migrate_request` calls the **model** `create_request`, not the route — the email block is never executed. |
| Consent notice | `routes/consent/crud.py` (`send_email_sync` + template) | `migrate_consent` does a **direct `Consent` insert**; the consent route is never invoked. |
| Stakeholder welcome / reset | `routes/stakeholders_routes.py` (`send_email_sync`, `issue_reset_token` mail) | `migrate_stakeholder` inserts the `User` directly and **drops the SMTP requirement and mail** (per its own docstring). |
| Vendor invite / questionnaire | `routes/vendor/crud.py` / `vendor_service` (`_send_vendor_invite`, `send_vendor_questionnaire_email`) | `migrate_vendor` calls only `_resolve_or_create_vendor_user` + `create_vendor` + `assign_processing_activities` — the email helpers are not on the path. |

All email transport in this app is `utils.email_utils.send_email_sync`
(**synchronous, in-request** — not a Celery task). Because the migration never
enters the routes that call it, no SMTP connection is even opened.

### 5.3 In-app notifications / websockets / activity feeds

No record-creation path in core enqueues an in-app notification or emits a
websocket/socketio event on create (no `.delay`/`.apply_async`/socket emit was
found in `routes/request`, `routes/consent`, `routes/vendor`, `services/`,
`models/` create paths). The `VendorActivity` and consent-`history` rows that
migration *does* create are **data rows** (state `initiated`, `is_read=False`),
not notification dispatches — they render in a dashboard when queried, they do
not push anything. So migration neither calls a notification service nor trips
one transitively.

### 5.4 OTP — none generated

OTPs originate in `routes/consent/otp.py` and `routes/request/otp.py`, and are
**verified** (not generated) inside the request route via `verify_context_otp`,
gated on `otp_required`. The migration payloads never set those flags and never
hit those routes, so no `OTP` row is created and no OTP delivery occurs. A
historical record is inserted as already-decided; there is nothing to verify.

### 5.5 SMS — none generated

Consent SMS is sent by `routes/consent/crud.py` via `utils.sms_utils.send_sms` /
`send_consent_sms`. `migrate_consent` bypasses that route entirely → no SMS
provider call.

### 5.6 Vendor emails on migrated VendorActivities — none

A `VendorActivity` created by `migrate_request` is a plain row; there is no email
hook on `VendorActivity` creation. Vendor mail only ever comes from the vendor
*routes*, which migration does not call.

### 5.7 Data-principal emails on migrated consents — none

Covered by 5.2/5.4/5.5: direct insert, random unusable password, no reset link,
no notice email, no SMS, no OTP. The migrated principal gets **zero**
communication.

### 5.8 Request notifications (PA Managers / DPOs / Stakeholders / Vendors / Principals) — none

All of these are produced inside `services/request_email.py`
(`send_request_create_email` and friends, each a `send_email_sync`), invoked by
the request *route*. `migrate_request` stops at the model layer, so **none** of
the five audiences are notified.

### 5.9 Background jobs / Celery / webhooks — none triggered

Celery exists in the project (`celery_app.py`, `celery_worker.py`,
`tasks/email_tasks.py`, `tasks/sla_tasks.py`, `tasks/bounce_tasks.py`,
`tasks/cleanup_tasks.py`, `tasks/report_tasks.py`), **but**:

1. No create path calls `.delay()` / `.apply_async()` — record creation enqueues
   nothing even in the normal flow.
2. Those tasks are **beat/scheduled** (SLA sweeps, bounce processing, cleanup,
   reports), independent of any single insert.
3. `migration_ext.serve` runs the **web app only**; it starts no worker/beat.

Therefore migration triggers no Celery task, webhook, or event listener. (The
scheduled jobs may later operate on migrated rows on their own cadence, but
nothing is *triggered by* the migration itself.)

---

## 6. Comparison with Normal Application Flow

| User action / effect | Normal runtime | Migration (`migration_ext`) |
|---|---|---|
| Create Request | `routes/request/crud.create_request` → model + email + OTP gate | `migrate_request` → **model `create_request` only** |
| Create Consent | `routes/consent/crud` → template + email + SMS | **direct `Consent` insert** |
| Assign Vendor | vendor route + invite/questionnaire email | `create_vendor` + `assign_processing_activities`, **no email** |
| Notify user | `send_*_email` from route/service | **never called** |
| Send email | `send_email_sync` (sync, in route) | **route never entered → no SMTP** |
| Create notification | (no create-time notification in core) | none |
| Generate OTP | `routes/*/otp.py`, verified in route | **route never entered → no OTP** |
| Send SMS | `routes/consent/crud` → `send_sms` | **route never entered → no SMS** |
| Audit / history rows | route + model | only the **data** history rows migration sets explicitly (e.g. `REVOKE_REQUEST_CREATED`) |
| Background tasks | beat-scheduled, not create-driven | none triggered |
| License seat | `consume_license` for new principal | **same** `consume_license` (intentional fidelity); headroom pre-provisioned by `ensure_license` |
| Auth / tenant | JWT + `Host`→`g.tenant` | **identical** mechanism |
| File upload | `utils.file_upload.upload_file` | **same** `upload_file` (identical naming/guards) |

The pattern: migration keeps everything that defines a *correct row*
(identity, license, file naming, tenant, dates, relationships) and drops
everything that is a *live communication or workflow trigger*.

---

## 7. Design Decisions

| Decision | Why chosen | Alternatives | Why this is better |
|---|---|---|---|
| **Extension, no core edits** | Patches were overwritten by `git pull` (Issue 26) | Edit routes/models; fork upstream | Zero merge surface; survives pulls |
| **Re-enter below side-effect layer** | Side effects live in routes/services | Add `skip_email` flags throughout core | No core change; can't be reverted upstream; no flag sprawl |
| **Reuse side-effect-free helpers** (`create_request`, `_resolve_or_create_vendor_user`, `create_vendor`, `consume_license`, `upload_file`, `resolve_principal`) | Behaviour (role, license, naming, dedup) matches real app exactly | Reimplement inserts by hand | Migrated rows indistinguishable from UI rows |
| **Direct insert for consents** | Consent route is thick with template/email/SMS | Call consent route with flags | A historical backfill needs none of that machinery |
| **`migration_source_map` table** | Idempotency without touching core schema | Add `odoo_source_id` to `requests`/`consents` | Upstream tables untouched; fan-out via `sub_key`; per-entity reset |
| **`sub_key` + batch source-map** | One Odoo template → many Flask rows | One mapping per source id | Correctly maps fan-out; one txn instead of hundreds of round-trips |
| **Forward all Odoo dates; log gaps** | Historical fidelity + auditability | Let models default timestamps | Preserves real history; missing dates are auditable, never fabricated |
| **`ensure_license` pre-provisioning** | Can't edit the live license gate (glue rule) | Skip `consume_license` in migration | Seat accounting stays truthful; covers both consent & request paths; reversible |
| **Runtime monkey-patch for `{varN}`** | Upstream validator rejects legacy placeholders | Edit `notice_template/crud.py` | Survives `git pull` |
| **`seed_operator` for auth** | Loader needs a live target-tenant identity | Hardcode a token | DPO bypasses RBAC and resolves as PA manager; fresh JWT mintable |
| **Attachments reuse `upload_file`** | Migrated file must equal a UI upload | Migration-only storage path | Same disk layout, naming, traversal/size guards |

---

## 8. Idempotency & Safety Mechanisms

`MigrationSourceMap` (`source_map.py`) maps
`(entity, odoo_source_id, sub_key, tenant_id) → flask_id`, enforced by a unique
constraint `uq_migration_source_entity_odoo_sub_tenant`.
`ensure_source_map_table()` creates it (and migrates an older single-key shape to
the fan-out shape) idempotently on every boot.

How this delivers each guarantee:

- **No duplicate records / requests / consents / vendors** — every `migrate_*`
  endpoint checks `MigrationSourceMap.existing(...)` first and returns `409 …
  already migrated`; the loader treats that 409 as a clean idempotent skip.
- **No duplicate emails / notifications** — there are none to duplicate (§5); a
  re-run that 409s also short-circuits before doing any work.
- **No duplicate vendor activities** — `migrate_request` checks for an existing
  `VendorActivity` (`filter_by request_id/vendor_id/tenant_id`) before adding.
- **No duplicate users** — `resolve_principal` / canonical email+phone dedup;
  `migrate_stakeholder` reuses an existing same-email tenant user rather than
  erroring.
- **Transactional safety** — each endpoint commits once at the end; failures
  (`IdentityConflict`, `ValueError`, `MigrationAttachmentError`,
  attachment errors) `rollback` the whole row. `migrate_source_map` batch runs in
  one transaction. Attachments are all-or-nothing per vendor.
- **`_record_one`** makes the source map itself idempotent: an identical prior
  mapping is a no-op `200`; a *different* `flask_id` for the same source is a
  `409` (catches accidental double-creation by a different path).

`reset.py` supports repeatable testing: **dry-run by default**, single
transaction, keeps seed rows (users 1–3, PAs 1–11), clears the source map so a
re-run isn't blocked, children-before-parents ordering for FK safety, and
optional `--only <entity>` for per-entity resets.

---

## 9. File-by-File Breakdown

| File | Function(s) | Purpose | Called by |
|---|---|---|---|
| `__init__.py` | `register_migration(app)` | Build `migration_bp` (`/api/migration`), register it, ensure source-map table | `serve.py` after `create_app()` |
| `routes.py` | `migrate_request` | Reuse `Request.create_request`; forward dates; restore PA/consent/vendor links | blueprint `POST /request` |
| | `migrate_consent` | Direct `Consent` insert; create/dedup principal; map enums/dates | `POST /consent` |
| | `migrate_stakeholder` | Backend PA-Manager user, no welcome/reset mail | `POST /stakeholder` |
| | `migrate_vendor` | `create_vendor` + vendor user + PA assign + attachments, no email | `POST /vendor` |
| | `migrate_source_map` / `_record_one` | Record Odoo→Flask id maps (single or batch) for natively-created rows | `POST /source-map` |
| | `migrate_ping` | Liveness | `GET /ping` |
| | `_parse_date`, `_parse_source_date`, `_parse_date_only`, `_enum_by_value`, `_tenant_id`, `_naive_utc` | Date parsing (ISO + dd/mm/YYYY) with fidelity logging; enum mapping; tenant/utc helpers | within `routes.py` |
| `source_map.py` | `MigrationSourceMap` (+ `existing`, `existing_any`, `record`), `ensure_source_map_table` | Idempotency table + boot-time DDL (incl. fan-out migration) | `routes.py`, `__init__.py` |
| `serve.py` | module, `_patch_template_variable_whitelist` (`validate_template_variables`) | Standalone server = `create_app()` + `register_migration()`; runtime patch for `{varN}` | `python -m migration_ext.serve` / gunicorn |
| `ensure_license.py` | `main()` | Raise tenant `total_users` so a load has seat headroom; idempotent, reversible | operator CLI before load |
| `seed_operator.py` | `main()` | Ensure a Backend **DPO** operator exists for the tenant; mint a fresh `FLASK_API_KEY` JWT | operator CLI |
| `reset.py` | `_connect`, `_table_exists`, `_count`, `build_plan`, `main` | Wipe migrated data to a clean baseline for repeat testing (dry-run default) | operator CLI |
| `attachments/__init__.py` | `process_vendor_attachments` | Orchestrate decode→validate→upload→map; all-or-nothing | `migrate_vendor` |
| `attachments/constants.py` | tables | Size cap, allowed ext, magic signatures, field→column/doc-type maps | submodule |
| `attachments/validators.py` | `validate_entry`, `validate_decoded`, `validate_ext`, `MigrationAttachmentError` | Fail-fast payload/content/ext checks → 400 | `__init__`, `decoder` |
| `attachments/decoder.py` | `decode_base64`, `sniff_mime`, `decode_attachment` | Base64→bytes, magic-based mime, `FileStorage` | `__init__` |
| `attachments/mapper.py` | `vendor_target` | Odoo field → (Vendor column, folder) | `__init__` |
| `attachments/uploader.py` | `store` | Thin wrapper over `utils.file_upload.upload_file` | `__init__` |

Client side (not part of `migration_ext`, but drives it): `migration/main.py`
(click CLI) and `migration/scripts/load/load_flask.py` (`FlaskLoader`).

---

## 10. Dependency Graph (external core modules `migration_ext` relies on)

| Core dependency | Used by | Why |
|---|---|---|
| `flask` (`Blueprint`, `request`, `g`) | `__init__`, `routes` | Blueprint + same request/tenant context as core |
| `flask_jwt_extended.jwt_required` | `routes.migrate_request` | Same identity gate as real routes |
| `models.db / Request / RequestType / ProcessingActivity / User / Role` | `routes` | Build rows on the real schema |
| `models.consent.*` (enums + `Consent`) | `routes.migrate_consent` | Direct consent insert with correct enum mapping |
| `models.vendor.Vendor / VendorActivity` | `routes` | Vendor + activity rows |
| `Request.create_request` | `migrate_request` | **Side-effect-free** request+user build (license via `_get_or_create_user`) |
| `services.vendor_service._resolve_or_create_vendor_user` | `migrate_vendor` | Role `Vendor` + DPTPA license, **no email** |
| `Vendor.create_vendor`, `assign_processing_activities` | `migrate_vendor` | Vendor row + PA M2M |
| `utils.license_utils.consume_license` | `migrate_consent` (DPCM) | Truthful seat accounting; headroom from `ensure_license` |
| `utils.identity.resolve_principal / canonical_email / canonical_phone / IdentityConflict / ERROR_MESSAGES` | `routes` | Principal dedup identical to core |
| `utils.encryption.hmac_hash / encrypt` | `routes`, `seed_operator` | Same PII hashing/encryption as core |
| `utils.password_utils.generate_random_password` | `routes`, `seed_operator` | Unusable password → no credential leak, no reset mail |
| `utils.token_utils.issue_reset_token` | `migrate_stakeholder` | Token created **without** sending the reset email |
| `services.consent_visibility.generate_artifact_no / generate_unique_id` | `migrate_consent` | Consent identifiers identical to core |
| `utils.file_upload.upload_file` | `attachments.uploader` | Identical file naming/guards as UI upload |
| `utils.response.api_response` | all routes | Same response envelope as core |
| `routes.notice_template.crud.validate_template_variables` | `serve` (patched) | Relax `{varN}` validation at runtime |
| `create_app` | `serve`, `ensure_license`, `seed_operator` | Build the real app/app-context |
| `sqlalchemy.text` | `routes` (action_date), `source_map`, `reset` | Raw SQL for out-of-Alembic columns/DDL |

Each dependency exists to **match core behaviour exactly** while keeping the
side-effect-emitting route layer off the call path.

---

## 11. Known Limitations

- **No vendor↔request relation in Odoo source** → `vendor_activities` /
  `request_assigned_vendor` populated by the request path are sparse by design;
  re-audit if production uses vendor-assessment request types.
- **DPGR request type / hardcoded `request_type_id=1`** gaps remain (request-type
  memory) — some dashboard request types are not fully mapped.
- **Out-of-Alembic schema** — `requests.odoo_source_id`/index,
  `requests.action_date`, and `migration_source_map` were applied as **raw SQL**,
  not Alembic migrations; `action_date` writes fail soft if the `ALTER TABLE`
  wasn't run.
- **Auth token lifetime** — `FLASK_API_KEY` JWT expires in hours; stale tokens
  produce `401` (dest reads) and, in reconcile, an `UNVERIFIED` verdict. Refresh
  via `seed_operator` before a run.
- **License is pre-provisioned, not bypassed** — if `ensure_license` isn't run
  with enough headroom, new-principal inserts can still hit "No active license".
- **Idempotency is keyed on the source map only** — rows created by a *different*
  path (not recorded in the map) won't be detected as duplicates by the
  pre-check (though `migrate_stakeholder`'s email reuse and the `VendorActivity`
  existence check mitigate the common cases).
- **Reconcile timing** — consent ids are unstable across `run-all`
  delete-reloads; reconcile is only valid after a completed load.
- **Source recovery** — only compiled `.pyc` is present in the tree; the `.py`
  sources should be restored to version control.

## 12. Recommendations

1. **Restore the `.py` sources** for `migration_ext` to the repo; shipping only
   `.pyc` is fragile (this doc had to be reverse-engineered from bytecode).
2. **Fold the raw-SQL schema changes into Alembic** so `action_date` /
   `odoo_source_id` / `migration_source_map` are reproducible and the
   write-fails-soft path can be removed.
3. **Automate the pre-flight**: a single command chaining `seed_operator` →
   `ensure_license` (DPCM + DPGR) before any load, to remove the 401/no-license
   foot-guns.
4. **Generalise attachments**: `mapper.py` already anticipates request
   attachment slots (`attachment/escalated/closed/track`); wire them in when
   request documents are migrated.
5. **Add a post-load reconcile gate** to CI that refuses `PASS` on an expired
   token (already partly done — keep the loud UNVERIFIED banner).
6. **Consider a sanity assertion** in `migrate_*` that SMTP is *not* configured
   in the migration serve process, making "no email" enforced rather than
   merely structural.

---

## 13. Final Architecture Summary

`migration_ext` is a side-effect-free, idempotent, tenant-aware loader that lives
entirely outside upstream so a `git pull` can never disturb it. Its defining
move is **re-entering the application below the route/service layer**, where the
data model is built but no email, OTP, SMS, invite, notification, or background
job is ever dispatched. It reuses every *correctness-defining* core helper
(`create_request`, vendor user creation, `consume_license`, `upload_file`,
identity dedup) so migrated rows are indistinguishable from UI-created rows,
while a private `migration_source_map` table provides idempotency (with `sub_key`
fan-out for templates) without touching the upstream schema. Historical Odoo
timestamps and relationships are faithfully forwarded and any gaps are logged
rather than fabricated. Supporting CLIs (`serve`, `seed_operator`,
`ensure_license`, `reset`) make a run repeatable and pre-provision the
identity/license headroom the historical backfill needs. The net result: a
full historical dataset lands in Flask correctly, and **not one real person
receives a single message.**
