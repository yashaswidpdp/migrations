# 04 — Transform: per-entity modules

Each module exposes a `transform_<entity>_data(input_file, output_file, ...)`
function called by the CLI. They all follow the conventions in
`03-transform-overview.md`.

## `transform_consent.py` → `processed_consents.csv`
Maps the DPCM dashboard + by-id enrichment into Flask consent columns.

- **`map_status`** — Odoo status text → `ConsentStatus` value (`"deemed"` →
  `Deemed Consent`, `"reject"` → `Rejected`, etc.; default `Deemed Consent`).
- **`map_processing_type`** — `userActivityType` → `Mandatory/Regulatory` |
  `Promotional`.
- **`map_consent_type`** — `paperType`/`digitalPaper` → `Paper` | `Digital`.
- **`map_legacy_type`** — `legacyType`/`legacyLive` → `Legacy` | `Live`.
- Emits all Odoo dates (consent_date, sent_on, delivered_on, valid_till,
  consent_reject_on, created_on, last_updated, closed_on) with time-of-day, plus
  proof fields (artifact, ip_address, device_type, request_no) and
  `processing_activity_name` (the loader resolves it to a PA id).

## `transform_request.py` → `processed_requests.csv`
Maps the DPGR dashboard + `/dpgr/id` enrichment.

- Extracts the principal (name/email/phone), `request_type_name`, `rag_status`,
  `risk`, and the audit/escalation/close fields.
- **`assigned_user_names`** — from `assignToDM [{id,name}]`: the *real* internal
  allottee. The loader resolves the name(s) to Flask user ids.
- **`assigned_users`** — the optional CLI `--user-id` fallback (a list like `[5]`),
  used only when the source carries no allottee. See `06-orchestrator-main-cli.md`.
- **`assigned_vendor_names` / `assigned_vendor_source_ids`** — from
  `assignToVendor`; the loader resolves names to Flask vendors and the backend
  creates the M2M link + `VendorActivity`.
- **`consent_source_ids`** — for revoke requests, the Odoo consent id(s) to
  withdraw (the backend resolves them via the source-map; consents must load first).

## `transform_request_type.py` → `processed_request_types.json`
Renames Odoo request-type fields to Flask's, validates SLA fields, enforces
single-revoke semantics. Master data — loaded before consents/requests.

## `transform_processing_activity.py` → `processed_processing_activities.csv`
Flattens the Odoo PA **tree** (`/processing_activities`) into rows, preserving
`parent` relationships, department/manager links, and template references. The
loader loads parents-before-children (topological).

## `transform_template.py` → `processed_templates.csv`
Maps Odoo templates to Flask notice/email templates.

- **`_map_template_type`** / **`_map_sub_type`** / **`_map_language`** — Odoo
  `templateType`/`subType`/`language.name` → Flask enums.
- Emits `is_default`, `processing_activity_names` (loader resolves to ids),
  `effective_from`, body, subject.
- **Default-template dedup (added):** Flask allows only ONE active default per
  `(template_type, language)` and rejects a default that has PAs. After building
  all rows, the transform keeps `is_default=True` on only the **first PA-less
  default per group** and demotes the rest to non-default — so the extras load
  **Active** instead of being archived when a second default of the same group is
  approved. (Root cause + fix story: this fixed the "isDefault" reconcile GAP.)

## `transform_vendor.py` → `processed_vendors.csv`
Maps `/vendors_details` to vendor columns: company/contact, contract dates, SLA,
risk, department names (→ PA ids server-side), and NDA/VRA attachments (carried as
inline Base64 for the backend attachments subsystem). Falls back to the
Data-Manager risk assessment when the overall risk is unset; adds audit timestamps.

## `transform_stakeholder.py` → `processed_stakeholders.csv`
Flattens internal stakeholders (`/stakeholders`) to name/email/phone + role names.
Role names are mapped to Flask roles by `stakeholder_role_mapper.py` at load time
(via `data/stakeholder_role_aliases.json`, e.g. `PA Manager → PA Manager Full
Access`). `stakeholder_report.py` writes a per-record created/updated/skipped/failed
report.
