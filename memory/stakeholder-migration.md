---
name: stakeholder-migration
description: Internal Stakeholder (Odoo->Flask) migration endpoints, quirks, and the role-name gap
metadata:
  type: project
---

Internal Stakeholder migration (Odoo -> Flask).

Odoo source: `GET /api/stakeholders` (non-paginated, single shot). Envelope `{status, recordType:"Internal Stakeholders", totalInternalStakeholders, stakeholders:[...]}`. Fields: `id, name, login(email), phone(false when empty), is_active, role_ids:[{id,name,...}]`. Same role NAME has many Odoo ids (DPO=4,5,9; PA Manager=6,10,11,12,15,16) and one user can carry the same name twice -> map by name + dedup.

Flask target = `POST /api/migration/stakeholder` (migration_ext, email-free), NOT the public `/api/stakeholder/create`. Public route sends a synchronous welcome email via send_email_sync + requires SMTP — must never fire for a historical backfill. migration_ext route: no email/SMTP/OTP/notification/celery; password-hash + reset-token are DB-only; idempotent via MigrationSourceMap (409 already migrated); reuses existing user by email/phone (created=false); always Active (Odoo is_active not applied). Roles list for mapping: `GET /api/roles/details` (tenant-scoped, only is_system=False). The migration server (migration_ext.serve on :5000) must be running — `/api/migration/ping` returns 200.

**Gap (RESOLVED 2026-06-17):** the live Flask tenant (skfinance, tenant_id=1) had ONE non-system role only — "Full Access" (id=2). No DPO / PA Manager, so name-based mapping failed for all 8 stakeholders. Resolution applied: (1) `data/stakeholder_role_aliases.json` = `{"DPO":"Full Access"}`; (2) created RBAC role "PA Manager" (id=3) via POST /api/roles/create cloning Full Access permissions. Re-run result: 0 failed (7 updated = existing tenant users reused by email, 1 skipped = source-map). Never hardcode role ids — fetch dynamically. [[role mapper is scripts/load/stakeholder_role_mapper.py]]
