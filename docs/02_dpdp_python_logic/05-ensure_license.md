# 05 — `ensure_license.py`: license provisioning

Historical consent/request/vendor migration **consumes a license seat per new data
principal** (a deliberate fidelity decision — migrated principals are real users).
The live license gate (`consume_license` / `Request.create_request`) enforces
*current* business rules and must not block a historical backfill. Since the core
gate can't be edited (glue-only rule), this tool guarantees enough capacity
**before** loading.

Run under the dpdp_python full-deps interpreter (it builds the app to use the ORM):

```bash
cd dpdp_python
./venv/bin/python -m migration_ext.ensure_license --tenant 1
```

## What it does (one idempotent pass, all modules)

For each module the migration touches — **DPCM** (consent), **DPGR** (request),
**DPTPA** (vendor), plus DPAP/DPIA/DDMT future-proofing — it does **both** jobs:

- **SEED if missing** — if the tenant has no active, in-date license for a module,
  it **creates** one with `--seats` capacity (default 100000) and `--expires`
  (default 2030-12-31). *This is the fix that closed the original gap: the old
  version errored "Seed a license first" when no row existed (which is exactly the
  state after a DB reset, since licenses aren't in alembic).*
- **ENSURE headroom** — if a license exists but free seats `< --min-remaining`
  (default 200), it raises `total_users` to that floor.

It never lowers a cap and never touches `used_users`, so re-running is always safe.

## CLI

| Flag | Default | Meaning |
|---|---|---|
| `--tenant` | 1 | target tenant (the live migration tenant) |
| `--modules` | `DPCM DPGR DPTPA DPAP DPIA DDMT` | module codes to provision |
| `--seats` | 100000 | `total_users` for a **newly seeded** license |
| `--min-remaining` | 200 | minimum free seats guaranteed on an **existing** license |
| `--expires` | 2030-12-31 | expiry date (YYYY-MM-DD) for a newly seeded license |

Example single-module with a bigger floor:
```bash
./venv/bin/python -m migration_ext.ensure_license --tenant 1 --modules DPCM --min-remaining 5000
```

## Output (per module)
- `SEEDED DPCM: created license id=… total_users=100000 …`
- `RAISED DPCM: total_users 200 -> 5431 …`
- `OK DPCM: … remaining=… (>= …; no change)`
- exits non-zero only on a hard error (unknown module code).

## Selection semantics
It picks the **oldest active, in-date** license per `(tenant, module)` — the same
row `consume_license` draws on — so headroom is added where the gate actually
reads it.

## Relationship to the live model
Reads/writes `models/licenses.py` (`License`, `LicenseType`). It is **not** in
alembic-managed migration logic — it's an operator tool you run before a load (or
after a DB reset). See `../../docs/migration_runbook.md` Step 2b and
`db_reset_guide.md` for where it fits in the full procedure.
