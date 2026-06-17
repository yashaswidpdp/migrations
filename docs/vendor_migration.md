# Vendor Migration — Odoo → Flask

**Status:** Built & verified 2026-06-17. 8/11 vendors migrate cleanly; 3 blocked by a
shared-contact-user constraint (Issue B). PA links pending PA re-migration (Issue A).

## Source / Target

- **Odoo:** `GET /api/vendors_details` (non-paginated; 11 vendors).
- **Flask:** `POST /api/migration/vendor` (migration_ext) → `Vendor` row + contact `User`.
- **Idempotency:** `MigrationSourceMap` entity `"vendor"` (Odoo id → Flask vendor id).

## Pipeline (CLI)

```
python main.py vendor extract     # /vendors_details -> data/raw/raw_vendors.json
python main.py vendor transform   # -> data/processed/processed_vendors.csv
python main.py vendor load        # -> POST /api/migration/vendor
python main.py vendor run-all     # all three
```

## Field Mapping

| Odoo | Flask | Backing column |
|------|-------|----------------|
| `vendor_name` | company_name | `vendors.company_name` |
| `email` | contact_email | `User.email` (vendor contact) |
| `contact_person` | contact_person | `User.name` |
| `vendor_contact` | contact_phone | `User.phone` |
| `vendor_website_url` | website | `vendors.website` |
| `date_of_rollout` | contract_start | `vendors.contract_start` |
| `last_overall_risk` | risk_level | enum (see below) |
| `state` | vra_status | enum (see below) |
| `department_ids[].name` | processing_activities | M2M, name → Flask PA id |
| `id` | — | source-map only (not `vendor_id`; Flask generates that) |

`status` (Active/Inactive) has **no Odoo source** → defaults **Active**.
`current_status` ignored (overlaps `state`).
Contact identity (email/person/phone) lives on the linked **User**, not the Vendor row.

## Risk Mapping (`last_overall_risk` → `risk_level`)

Odoo values are digit-prefixed colours (`1green`, `2amber`) or `false`.
Transform strips digits, lowercases, matches colour.

| Odoo | Flask |
|------|-------|
| `*green` | Low |
| `*amber` | Medium |
| `*red` | High |
| `false` / empty | **NULL** |

`risk_level` has `default="Low"`; `migrate_vendor` assigns it **post-insert** so a
`false` vendor lands NULL instead of a wrong "Low".

## Status Mapping (`state` → `vra_status`)

| Odoo state | Flask vra_status |
|------------|------------------|
| not_started | Pending |
| submitted | In Progress |
| approval | Completed |

## Code

| File | Role |
|------|------|
| `scripts/extract/extract_odoo.py::run_vendor_extraction` | fetch `/vendors_details` |
| `scripts/transform/transform_vendor.py` | map fields, `map_vendor_risk`, `map_vendor_vra` |
| `scripts/load/load_flask.py::load_vendors` | resolve dept name→PA id, POST, identity retry, 409 classification |
| `migration_ext/routes.py::migrate_vendor` | create Vendor + contact User; risk NULL fix |

## Known Issues / Edge Cases

**A — Processing-activity links empty.** Vendor `department_ids` resolve by **name** to
Flask PA ids via `/processing/activities/simple`. A prior DB reset deleted the 27
Odoo-migrated PAs (tenant 1 now holds 11 seed PAs), so the names don't match → 0 PA
links. **Fix:** re-run `python main.py processing-activity run-all`, then re-run
`vendor load`. The vendor code is correct; this is purely missing target PAs.

**B — One vendor per user.** Flask enforces a single Vendor per contact User. Several
Odoo vendors share one contact person/email (e.g. `test`, `test02`, `test09` all map to
"Anku Singh"), so only the first migrates; the rest get
`409 "Vendor already exists for user '<name>'"` and are now logged as **failures**
(not silently skipped). Decision pending: accept (test data), use unique synthetic
contacts, or relax the Flask one-vendor-per-user rule (production change).

**C — Request → Vendor assignment deferred.** Odoo request `assignToVendor[]` references
**Vendor Users**, not vendor companies (ids don't match `vendors_details`). Request↔vendor
linking (`vendor_activities`) is handled later. Flask's native auto-link
(`_create_vendor_activities`, fires when a request's PA request-type is
`vendor_mandatory`) covers the common path without `assignToVendor`.

**D — Identity collisions.** Vendor contact email/phone go through the same identity
system as request principals. The loader retries **email-only** on an identity-conflict
409 (shared dummy phones), mirroring the request loader.

**E — Attachments.** `vra_attachment` / `nda_attachment` not migrated — same Odoo
`/web/content` download blocker as request attachments (needs a real Odoo web session).

## Verification (DB, tenant 1)

8 vendors created with correct `risk_level` (Low/Medium/NULL), `vra_status`
(Pending/In Progress/Completed), `status=Active`, and a distinct contact User each.
`vendor_id` auto-generated (`VND-######`). PA-link count 0 pending Issue A.
