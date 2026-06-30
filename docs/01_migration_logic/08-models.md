# 08 — `migration/models/`

A small set of local model/reference modules used by the migration project:
`consent.py`, `request.py`, `processing_activity.py`, `template.py`, `vendor.py`,
plus `__init__.py`.

## Purpose

These are **migration-side reference definitions** — the field names, enum
vocabularies, and shapes the transforms target — not the live Flask ORM. The
authoritative database models live in `dpdp_python/models/` (e.g.
`models/consent.py`, `models/vendor.py`, `models/licenses.py`); the migration
mirrors only what it needs to map onto.

Use them to answer "what columns/enums does Flask expect for this entity?" while
writing or auditing a transform. When in doubt about the live schema, the
`dpdp_python/models/*` files are the source of truth (see
`../02_dpdp_python_logic/`).

## Why duplicated here at all

Keeping a thin local reference lets the transform modules import/validate against
the target shape without importing the full Flask app (which needs its env, DB, and
heavy deps). It keeps the ETL project runnable in its own lightweight venv,
separate from the `dpdp_python` venv used by `migration_ext`.

> If a Flask model changes (new enum value, renamed column), update the relevant
> transform's `map_*` helpers and, if used, the local reference here — then
> re-run reconcile to confirm the field still value-checks.
