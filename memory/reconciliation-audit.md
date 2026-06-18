---
name: reconciliation-audit
description: Migration reconciliation tool + the data-completeness findings it surfaced (license-blocked + silently-dropped consents, untracked PA/templates)
metadata:
  type: project
---

Reconciliation audit: `python main.py reconcile` (also `--self-test`, `--no-write`). Code `scripts/report/reconcile.py`, tests `tests/test_reconcile.py` (12, DB-free). Writes `data/processed/reconciliation_report.txt`.

Method: per entity compares SOURCE (data/raw) vs MIGRATED (`migration_source_map` read via `docker exec privacium_postgres psql`) vs FAILED (data/processed/errors_*). Does id-level set-diff (`raw_ids - landed - failed - accepted`) so it names exact dropped ids, not just counts. Accepted-loss registry = `data/accepted_loss.json` (consumed so signed-off drops aren't GAPs).

State after fixes (2026-06-17): tracked entities **550/551 = 99.8%**.
- **request 71/71, stakeholder 8/8, consent 460/460 PASS; vendor 11/12 PASS*** (test02 accepted, see [[vendor-migration]]).
- **Consent recovered to 100%.** Root cause of the original 6 "silently dropped" (ids 23,28,382-385) was NOT status filtering — it was the loader: `load_consents_via_migration` counted EVERY 409 as "already migrated, success" (blanket swallow). ids 23/28 hit 409 phone-collision (another DataPrincipal owns the phone); ids 382-385 a transient PA-resolve/license issue. Fix (no Flask endpoint change): mirror vendor loader — 409 "phone" -> retry email-only; 409 only skips when text says "already migrated"; everything else recorded as a real failure. Also added: clear stale `errors_*.csv` on a 0-failure run (else audits read old failures). The 123 "No active license available" failures also cleared on re-run (capacity recovered).
- **processing_activity + template still UNTRACKED** — no `migration_source_map` rows, so not idempotent and completion can't be proven per-record. PA: 27 source-tree nodes -> 38 db rows. Template: 28 Odoo templates -> 233 notice_templates (fan-out). Add source-map tracking mirroring vendor/stakeholder.

Lesson: blanket `409 -> skip/success` is a recurring bug class in this codebase (hit it in vendor AND consent loaders). Always gate idempotent-skip on the actual "already migrated" message; route other 409s to retry or failure.

Open requirements ranked: (1) source-map tracking for PA+template, (2) DPO-vs-PAManager fidelity [[stakeholder-migration]], (3) rotate exposed Odoo JWT + Flask API key.
