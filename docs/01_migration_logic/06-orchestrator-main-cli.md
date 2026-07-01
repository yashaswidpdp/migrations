# 06 — Orchestrator: `main.py` (the CLI)

`main.py` is a `click` CLI. It defines a command **group per entity** plus a
top-level `reconcile` command and the full-pipeline `migrate-all`. Every command
wraps the `run_*` functions from the extract/transform/load modules in a
try/except that logs and echoes errors.

Run everything from `migration/` with the venv active.

## Per-entity command groups

Each entity group exposes the stage subcommands plus a `run-all`:

```
python main.py <entity> extract      # Odoo → data/raw/
python main.py <entity> transform    # data/raw → data/processed/
python main.py <entity> load         # data/processed → Flask
python main.py <entity> run-all      # extract → transform → load (→ approve, where relevant)
```

Entities: `request-type`, `stakeholder`, `processing-activity`, `template`,
`vendor`, `consent`, `request`.

### Notable per-group extras
- **consent**: `enrich` (by-id backfill) is part of `run-all` (Stage 1.2).
- **request**: `run-all --user-id <id>`; also re-seeds request types inside its
  pipeline (Stage 1.8) so a stand-alone request run still resolves types.
- **processing-activity**: has `load` *and* `patch-links`. `run-all` patches links
  immediately, which **no-ops if templates aren't loaded yet** — so the manual
  order (and `migrate-all`) load PAs, then templates, then backfill links.
- **template**: `load`, `approve`, `load-approve`, `patch-pa-links`. `run-all` does
  extract→transform→**load+approve** (`run_template_load_and_approve`).

## `migrate-all` — the one-shot

```bash
python main.py migrate-all                      # whole migration, dependency order
python main.py migrate-all --user-id <id>       # optional fallback request owner
python main.py migrate-all --continue-on-error  # don't abort on a stage failure
```

It runs 7 stages, reusing the **same `run_*` functions** the per-entity `run-all`
commands use (so it never drifts from them):

1. request-type 2. stakeholder 3. processing-activity (**load only**, no link patch
yet) 4. templates (**load+approve, then `run_pa_link_patch` + `run_template_pa_link_patch`**)
5. vendors 6. consents (extract→enrich→transform→load) 7. requests
(extract→enrich→transform→ idempotent request-type reload → load).

Behavior:
- Prints a stage banner + per-stage timing; logs start/done.
- **Aborts on the first stage failure** (later stages depend on earlier ones)
  unless `--continue-on-error`. Exit code 1 if any stage failed.
- **Idempotent** — re-run after a fix; completed rows 409-skip.

### `--user-id` is optional
It only sets `assigned_users` as a **fallback owner** for requests with no allottee
in the source. Each request's real allottee (`assignToDM`) is resolved regardless.
Omit it and source-unassigned requests migrate **unassigned**.

### Prerequisites `migrate-all` does NOT do
- Flask must be booted via `migration_ext.serve`.
- Licenses must be seeded (`migration_ext.ensure_license --tenant <id>`), or the
  consent/request/vendor stages fail with "No active license".

## `reconcile` command
`python main.py reconcile [--live] [--cached-source] [--no-write] [--self-test]`.
See `07-reconcile-report.md`.
