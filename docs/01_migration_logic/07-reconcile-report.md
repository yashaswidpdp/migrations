# 07 — Reconcile: `scripts/report/reconcile.py`

The reconciler produces a single human-readable audit
(`data/processed/reconciliation_report.txt`) that **proves how much data landed and
explains every record that didn't** — instead of eyeballing the migration log.

## Four count layers per entity

| Layer | Source | Meaning |
|---|---|---|
| 1. SOURCE | `data/raw/*` | what Odoo gave us |
| 2. STAGED | `data/processed/*` | what transform produced |
| 3. MIGRATED | `migration_source_map` (live Postgres) | what actually landed in Flask |
| 4. FAILED | `data/processed/errors_*` | rejected rows + grouped reasons |

MIGRATED counts are read from the live `migration_source_map` via
`docker exec … psql` (no extra Python DB dependency). Every external read degrades
to "unknown" rather than raising, so the report always renders. Container/creds
are overridable: `RECON_PG_CONTAINER` (`privacium_postgres`), `RECON_PG_USER`
(`yashaswi`), `RECON_PG_DB` (`privacium_db`).

## Modes

```bash
# offline — counts only, instant, no network:
python main.py reconcile

# live — re-pull Odoo SOURCE + verify each ledger row against the live Flask app,
#        surfacing field-level DRIFT:
python main.py reconcile --live

# live but fast — SOURCE from the data/raw snapshot (skip the ~13min Odoo re-pull):
python main.py reconcile --live --cached-source

# internal consistency check, then exit:
python main.py reconcile --self-test

# print only, don't write the .txt:
python main.py reconcile --no-write
```

Tokens come from `config/.env` (same file the loader uses); never hardcoded.

## Reading the report

- **SUMMARY table** — per entity: `src / migr / fail / acc / unexp / % / verdict`.
  Verdicts: `PASS`, `PASS*` (covered by accepted-loss), `FIX` (recoverable),
  `GAP` (investigate), `??` (unverified/not tracked).
- **PER-ENTITY DETAIL** — count breakdown, a field-coverage note (which source
  fields are value-checked vs unmapped), and **side-by-side field diffs** for
  records that differ source-vs-live. Flask `id` is excluded from comparison by
  design.
- **REMAINING REQUIREMENTS** — ranked, actionable follow-ups.
- **RELATIONSHIP INTEGRITY** — e.g. Vendor↔Request link-row counts.

## Interpreting common diffs

- **`phone` mismatches** — usually normalization (leading zeros / formatting), not
  loss. If a field is a known transform mapping, extend the reconciler's
  `FIELD_MAPS` normalizer rather than re-migrating.
- **Template `template_type` / `processingActivities[len]`** — caused by Odoo
  allowing several templates with the same name but different types; Flask's unique
  name collapses them. Model mismatch / accepted-loss.
- **`accepted_loss.json`** (`data/accepted_loss.json`) — records intentionally
  unmigratable items so they show as `PASS*`/`acc` instead of `GAP`.

## Operating rules

- Don't reconcile **mid-load** — counts/DRIFT are only valid once a load finishes.
- A `LIVE VERIFY FAILED` / `UNVERIFIED` verdict means the Flask token died (401) —
  refresh `FLASK_API_KEY` and re-run; never trust a count-only PASS from a 401 run.
- Use `--cached-source` while iterating; drop it for a final sign-off that re-pulls
  live Odoo.
