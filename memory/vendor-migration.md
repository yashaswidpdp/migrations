---
name: vendor-migration
description: Vendor (Odoo->Flask) migration outcome, the phone-collision retry, and the irreducible same-email identity conflict
metadata:
  type: project
---

Vendor migration (Odoo `/api/vendors_details` -> Flask `POST /api/migration/vendor`). Idempotent via MigrationSourceMap (entity `vendor`); a 409 containing "already migrated" = skip.

**Identity conflicts.** Flask `_resolve_or_create_vendor_user` (services/vendor_service.py) looks up an existing tenant user by `email_hash OR phone_hash`. If the match is NOT a Vendor it raises 409 "User '<name>' already exists with role '<role>'. Only users with role 'Vendor' are allowed." One human = one user = one role_type; a DataPrincipal can't also be a Vendor.

- **Phone collision (recoverable):** several test vendors shared dummy phone `9999999999`, which hashes to migrated DataPrincipal `Kumkum Singh`. Loader `load_flask.py::load_vendors` retries email-only when the 409 text contains "principal" (fixed 2026-06-17 — guard previously matched "data principal" two-word, but message says "DataPrincipal" one-word, so retry never fired). Email is unique per vendor -> email-only create succeeds.
- **Same-email conflict (irreducible):** vendor `test02` (Odoo id 4) shares its exact email `test02@yopmail.com` with DataPrincipal user 1089. Email-only retry still 409s. Cannot migrate without altering data or changing the endpoint (both off-limits). **Decision 2026-06-17: accept unmigrated** (test data; person already exists). Final vendor result = 11/12 migrated.

Full migration state after 2026-06-17 session: consent 331, request 71, stakeholder 8/8 (see [[stakeholder-migration]]), vendor 11/12. No Flask endpoints were changed.
