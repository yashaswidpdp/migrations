# License Enforcement vs Historical Migration — Technical Decision Document

Date: 2026-06-19
Author: Migration engineering
Status: Proposed (recommendation in §6)

---

## 0. Executive summary / premise correction (READ FIRST)

The brief is written around a model that does **not exist in this codebase**. Before
the design, the architecture must be stated accurately, because it changes the whole
solution:

1. **There is no per-consent or per-request "license".** Consents (`models/consent.py`)
   and Requests (`models/request.py`) have **no** foreign key, column, or relationship
   to `License`. A record is never "linked to an expired license".
   (`grep -n license models/consent.py models/request.py` → no hits.)

2. **The `License` entity is a tenant-level *seat pool*** for product modules
   (`DPCM` = consent principals, `DPGR` = request principals, `DPTPA` = vendor users).
   A license says "tenant 3 bought 100 DPCM user-seats, has used 80, expires 2027".
   It is consumed **one seat per newly-created `User`**, not per consent/request.

3. **There is no "Suspended" license status anywhere.** A license has only:
   `active` (bool), `expires_at` (datetime|null), `total_users`, `used_users`,
   `expires_users`. Derived validity is `License.is_valid`
   (`models/licenses.py:95`). "Suspended" / "Inactive" as distinct states do not
   exist in the schema.

4. **Therefore the failures are NOT license-status enforcement on historical data.**
   The `No active license available` error fires only when the migration tries to
   **create a brand-new principal User** and the tenant seat pool is exhausted or has
   no active in-date license. It is a *capacity* failure, not a "this record is
   expired so it's rejected" failure.

Consequence for the requirements:

- **Req 1 (all records migrate):** valid and achievable — see recommendation.
- **Req 2 (Expired→Expired), Req 3 (Flask shows Expired):** these describe
  *consent lifecycle* state, which the migration **already preserves** independently
  of licensing (status/lifecycle enums carried verbatim, `routes.py:294-302,323-325`).
  The seat-pool license has nothing to do with whether a consent shows as Expired.
  No change needed and nothing to fix here — the worry is aimed at the wrong subsystem.
- **Req 4 (survive fresh reset):** valid — this is the real engineering problem, and
  it reduces to "guarantee enough DPCM/DPGR seats exist before load".

The honest framing: **this is a seat-capacity provisioning problem, not a
historical-state-preservation problem.** The right long-term fix makes seat capacity
deterministic and self-provisioning at migration time. The current
`migration_ext/ensure_license.py` already does this; §6 hardens it.

---

## 1. Current architecture

### 1.1 How migration creates records

Migration is glue-only (`dpdp_python/migration_ext/`, never edits core — see
`[[migration-ext-pattern]]`). Two record paths:

- **Consent** — `POST /api/migration/consent` → `migration_ext/routes.py:248`
  `migrate_consent()`. Direct `Consent(...)` insert. Resolves/creates the principal
  `User` via `resolve_principal`; **if a new user is created it calls
  `consume_license(module="DPCM")`** at `routes.py:292`.

- **Request** — `POST /api/migration/request` → `routes.py:70` `migrate_request()`.
  Reuses core `Request.create_request` (`models/request.py:296`), which calls
  `_get_or_create_user` → **`_consume_user_license` → `consume_license(module="DPGR")`**
  at `request_helpers.py:478`.

So migration **reuses runtime creation logic** (deliberately, to keep behaviour
identical) and inherits its license gate. There is no "migration mode" flag in core.

### 1.2 Why migration depends on license validation

Because seat consumption is wired into **user creation**, and historical principals
are mostly new users in a fresh target tenant. The gate is correct for live traffic
(don't let a tenant exceed seats they paid for) but it was never meant to bound a
bulk historical backfill. Evidence: `ensure_license.py:1-22` docstring documents
exactly this policy decision (D1).

---

## 2. License enforcement map

Every place a migrated record can hit the gate. Gate impl:
`utils/license_utils.py:9 consume_license` → raises `ValueError("No active license
available for module '<X>' (tenant_id=...)")` at `license_utils.py:68`.

| # | File:line | Function | Module | Entity | On path? |
|---|-----------|----------|--------|--------|----------|
| 1 | `migration_ext/routes.py:292` | `migrate_consent` | DPCM | **Consent** (new principal) | **YES — 129 fails** |
| 2 | `services/request_helpers.py:478` (`:380` impl) | `_get_or_create_user` ← `Request.create_request` ← `migrate_request` | DPGR | **Request** (new principal) | **YES — 20 fails** |
| 3 | `services/consent_import.py:517` | CSV consent import | DPCM | Consent | No (CSV path, not Odoo loader) |
| 4 | `services/consent_import.py:1039` | CSV consent import (2nd branch) | DPCM | Consent | No |
| 5 | `services/vendor_service.py:361` | `_resolve_or_create_vendor_user` | DPTPA | Vendor user | Indirect (via `/migration/vendor`) |
| 6 | `services/user_service.py:51,110` | backend user create | DPCM | User/Stakeholder | No (stakeholder path skips it) |

Call chain (consent, the dominant failure):
```
loader → POST /api/migration/consent → migrate_consent (routes.py:248)
  → resolve_principal (new user) → User(...) + db.session.flush()
  → consume_license(tenant, "DPCM", user.id)   # routes.py:292
      → License.query filter(active, used<total, not-expired).with_for_update()
      → none found → raise ValueError("No active license available…")  # :68
  → (no try/except around it) → 500 / failed row
```

Note the asymmetry worth knowing: **an existing/re-used principal consumes nothing.**
That is why 331 consents already succeeded — they mapped to principals already
created earlier in the same load (or pre-existing). The 129 failures are the
*incremental new principals* that ran the DPCM pool dry. Same logic for the 20
requests on DPGR.

---

## 3. How license status is stored and exposed

| Concern | Location |
|---|---|
| Status columns | `models/licenses.py:76-80` — `total_users, used_users, expires_users, active, expires_at` |
| "Is it usable" | `License.is_valid` `models/licenses.py:95` (active AND not past `expires_at` AND `remaining_users>0`) |
| Expiry handling | `License.handle_expiry` `:112`; `_deactivate_license` `license_route.py:77` (snapshots remaining→`expires_users`, sets `active=False`) |
| Selection at consume | `license_utils.py:45-60` — oldest active, in-date, with free seats (`with_for_update`) |
| API exposure | `routes/license_route.py` GET `/license` → `_serialize_license_summary` `:39` (`active`, `expires_at`, counts). SuperAdmin/DPO only. |
| UI | License admin panel consumes that endpoint; consumed seats listed via `ConsumedLicenseLog` `:62`. |

There is **no status field on consents/requests derived from a license**. Consent
lifecycle/expiry is its own thing: `ConsentStatus`/`ConsentLifecycleEnum`
(`models/consent.py`), set in `migrate_consent` from the Odoo source value and carried
verbatim (`routes.py:295-302, 323-325, valid_till at :309/:339`). So "Flask shows
Expired" is already satisfied by the existing consent mapping and is unaffected by any
licensing decision below.

---

## 4. Candidate solutions

All four make the 460/71 land. They differ in truthfulness of seat accounting,
core-code risk, and reset-survival.

### Option A — Migration-mode bypass in `consume_license`
Add `migration_mode`/skip flag; when set, create user without consuming a seat.
```python
def consume_license(*, tenant_id, module_code, user_id, skip=False):
    if skip: return None
```
- Requires **editing core** (`utils/license_utils.py`) → violates glue-only rule
  (`[[migration-ext-pattern]]`), and core is overwritten on `git pull`.
- **Seat accounting becomes a lie**: 460 principals exist, `used_users` unchanged.
  Post-migration the tenant looks like it has 460 free seats it never bought; the
  license panel under-reports real usage. Audit/reporting risk.
- Lowest immediate effort, worst long-term integrity.

### Option B — Migration service-account bypass
`if current_user.is_migration_user: skip`.
- Still edits core; still a flag on the gate.
- Same accounting lie as A.
- Adds an identity dependency ("must run as user X") — fragile on fresh reset where
  that user may not exist yet. Rejected by Req 4 ("does not depend on pre-created
  users").

### Option C — Separate historical import path that doesn't touch users-as-seats
Insert `Consent`/`Request` + principal `User` rows directly, skipping
`consume_license` entirely, in a dedicated loader.
- For consents this is *almost already true* — `migrate_consent` is a direct insert;
  only line 292 ties it to the gate. We could drop that one call in glue.
- But requests go through core `Request.create_request`; bypassing means
  **reimplementing** request creation in glue (hundreds of lines, drift risk) — the
  exact thing `routes.py:554-562` says we deliberately avoid.
- Same seat-accounting lie unless we also backfill `used_users`.

### Option D — Pre-provision seat capacity (headroom), then load normally  ← current `ensure_license.py`
Before load, raise the active license's `total_users` to cover the whole source set,
then run the **unmodified** path so every principal consumes a real seat.
```python
floor = lic.used_users + min_remaining
if lic.total_users < floor: lic.total_users = floor   # idempotent, never lowers
```
- **No core edit.** Pure glue + a data update on the licenses table.
- **Seat accounting stays truthful**: migrated principals are real users and really
  consume seats; `used_users`/`ConsumedLicenseLog` reflect reality; the license panel
  is correct.
- Idempotent and reset-safe **iff** a seedable active license exists (see §6 gap).

---

## 5. Historical license-state preservation per option

Restating §0: the seat-pool license's state is independent of every consent/request,
so "preserve Expired→Expired on records" is automatically satisfied by all options —
the consent's own `status`/`valid_till` carry the Odoo state regardless.

What differs is **the seat-pool license's own truthfulness**:

| Option | DB impact on licenses | API/panel impact | UI impact |
|---|---|---|---|
| A / B (bypass) | `used_users` stays low despite 460 real users → **under-counts** | `available_users` overstated; consumed-log missing 460 rows | Panel shows phantom free seats; audit can't tie users→seats |
| C (separate path) | Same as A unless `used_users` manually backfilled | Same risk | Same |
| **D (headroom)** | `total_users` raised once; `used_users` grows correctly to +460; `expires_users` untouched; `active`/`expires_at` untouched | `purchased_users`↑, `consumed_users` correct, `available_users` correct | Panel truthful; every seat has a `ConsumedLicenseLog` |

Consent-state preservation (all options, already working): Odoo `status` →
`ConsentStatus` enum → `consent_lifecycle` (`routes.py:295-296`), `valid_till` from
source (`:309`). Expired stays Expired in DB, API, UI. No reactivation path exists in
the migration code.

---

## 6. Recommended solution

**Adopt Option D (pre-provision headroom) as the mechanism, and harden it into a
deterministic, self-seeding migration preflight.** It already exists as
`migration_ext/ensure_license.py`; the recommendation is to close its one gap.

Why D over A/B/C against the stated criteria:

| Criterion | D |
|---|---|
| Preserves historical truth | ✅ consent state untouched; seat counts stay real |
| Survives fresh reset | ✅ once §6.1 gap closed (auto-seed) |
| No pre-created users | ✅ operates on licenses table only |
| No oversized licenses | ✅ raises to `used + min_remaining`, a tight floor, not a blank cheque |
| Minimizes code risk | ✅ **zero core edits**; glue + data only |
| Normal runtime behaviour | ✅ live traffic still gated exactly as before |
| Idempotent | ✅ only ever raises to floor; re-run = no-op (`ensure_license.py:90-95`) |

A and B fail "no core edit" + "audit truth". C fails "code risk" (reimplement
requests) and "audit truth". D wins on every axis.

### 6.1 The one real gap to fix
`ensure_license.py:75-81` **errors out if no active in-date license exists** — exactly
the fresh-reset case (Req 4). Today that needs a manual `POST /license/create` first.
Fix: have the preflight **seed a license when none exists** (or extend `expires_at` if
the only one is expired), so a bare DB self-provisions. This is the difference between
"survives reset with a manual step" and "survives reset, full stop".

---

## 7. Fresh-reset scenario walk-through

Start: empty DB, no users/consents/requests, no DPCM/DPGR license (or expired ones),
zero consumption.

1. **Preflight seed+headroom (DPCM):** run hardened `ensure_license --tenant 3 --module
   DPCM --min-remaining 500`. No active license → **seed one** (`total_users=500,
   used=0, active=True, expires_at=now+1y`) instead of erroring. (Source set 460 +
   buffer.)
2. **Preflight (DPGR):** same for requests, `--min-remaining 100` (source 71 + buffer).
3. **Load order** (`[[migration-endpoints-and-order]]`): stakeholders → PA → vendor →
   **consent** → request.
4. **Consents:** 460 `POST /migration/consent`. Each new principal consumes 1 DPCM seat;
   `used_users` climbs 0→~460, all within the 500 floor → **no `No active license`**.
   Re-used principals consume nothing. Result **460/460**.
5. **Requests:** 71 `POST /migration/request`; new principals consume DPGR within the
   100 floor. Result **71/71**.
6. **State preserved:** each consent's `status`/`consent_lifecycle`/`valid_till` come
   from Odoo → Expired stays Expired, etc. The license's own `active`/`expires_at`/
   `expires_users` are never touched by the loader; only `used_users` rises truthfully.

Zero manual workaround. Idempotent: a second full run hits `MigrationSourceMap`
(409 "already migrated", `routes.py:79,257`) and `ensure_license` no-ops.

### 7.1 Important sequencing caveat
If `expires_at` could pass **mid-load** (very long load + short license), `consume_license`
(`license_utils.py:52-55`) would start rejecting. Seed `expires_at` comfortably in the
future (≥1y) in the preflight. Cheap insurance.

## 7.2 Tenant guard (carry over the known prior incident)
`[[migration-wrong-tenant]]`: a prior run loaded all 608 records into tenant 1 via a
stale `FLASK_API_KEY`. The preflight provisions tenant 3; if the loader's JWT/Host
resolves to tenant 1, seats get added to 3 but records land in 1 → confusing partial
failures. **Assert the loader's resolved `g.tenant.id == 3` before loading** (and that
`ensure_license --tenant` matches). See §9 validation.

---

## 8. Risk assessment

| Risk | Detail | Mitigation |
|---|---|---|
| **Licensing/commercial** | Raising `total_users` inflates apparent entitlement vs contract | Raise to a tight floor (`used+min_remaining`), record the bump in run log; optionally lower `total_users` back to contract after load (`ensure_license.py:13` notes it's reversible). Document the historical-backfill carve-out. |
| **Reporting** | License panel shows higher `purchased_users` | Truthful (seats really used); annotate the license row / changelog. Far better than A/B phantom seats. |
| **Audit** | "Why did seats jump 500?" | `ConsumedLicenseLog` ties every seat to a real migrated user; preflight prints `RAISED total_users X→Y` — keep that in the run artifact. |
| **Tenant isolation** | Wrong-tenant repeat of `[[migration-wrong-tenant]]` | Pre-load assertion on `g.tenant.id`; `ensure_license --tenant` must equal loader tenant; reconcile **with tenant filter** (today's `reconcile.py` lacks one — fix per that memo). |
| **Rollback** | Need to undo a bad load | All `/migration/*` rows tracked in `MigrationSourceMap`; delete-by-map + decrement `used_users`/remove `ConsumedLicenseLog` + lower `total_users`. See §9.5. |
| **Data integrity / dup** | Re-run double-inserts | Guarded: `MigrationSourceMap.existing` 409s (`routes.py:79,257`); consents are delete-reloaded by run-all (`[[consent-runall-delete-reload]]`) so reconcile only post-load. |
| **Mid-load expiry** | License lapses during load | Seed `expires_at` ≥1y (§7.1). |

---

## 9. Implementation plan

### 9.1 Files to modify
- `dpdp_python/migration_ext/ensure_license.py` — add seed-if-missing / extend-if-expired.
- (optional) `dpdp_python/migration_ext/__init__.py` or loader driver — pre-load tenant
  assertion.
- **No core files.** (`license_utils.py`, `request_helpers.py`, `routes/license_route.py`
  untouched.)

### 9.2 Functions to modify
- `ensure_license.main()` — replace the hard `sys.exit(1)` at `:75-81` with a seed path.

### 9.3 Code change outline (glue only)
```python
# ensure_license.main(), replacing the "no active license -> exit" block
if not lic:
    if not args.seed_if_missing:
        print("ERROR: no active license; pass --seed-if-missing", file=sys.stderr); sys.exit(1)
    lic = License(
        tenant_id=args.tenant,
        license_type_id=lt.id,
        total_users=args.min_remaining,
        used_users=0,
        expires_users=0,
        active=True,
        expires_at=datetime.now(timezone.utc).replace(tzinfo=None)
                   + relativedelta(years=args.seed_years),  # default 1
    )
    db.session.add(lic); db.session.flush()
    print(f"SEEDED license id={lic.id} module={args.module} total={lic.total_users}")

# if an only-license is expired, extend instead of erroring:
elif lic.expires_at and lic.expires_at < now_utc and args.extend_expired:
    lic.expires_at = now_utc + relativedelta(years=args.seed_years)
    lic.active = True

# then existing floor logic (unchanged):
floor = lic.used_users + args.min_remaining
if lic.total_users < floor:
    lic.total_users = floor
db.session.commit()
```
Reuses the live model so seeded licenses are indistinguishable from `POST /license/create`
ones.

### 9.4 Migration steps (rollout)
1. Confirm target tenant (=3) on both loader JWT/Host and `--tenant`.
2. `python -m migration_ext.ensure_license --tenant 3 --module DPCM --min-remaining 500 --seed-if-missing`
3. `python -m migration_ext.ensure_license --tenant 3 --module DPGR --min-remaining 100 --seed-if-missing`
4. Run loader in order: stakeholder → PA → vendor → consent → request
   (`[[migration-endpoints-and-order]]`).
5. Reconcile (tenant-filtered).

### 9.5 Validation steps
- **Counts:** `reconcile.py` with a **tenant_id=3 filter** → `Consents 460/460`,
  `Requests 71/71`, `Failed 0`.
- **No dup:** `SELECT entity,odoo_source_id,count(*) FROM migration_source_map GROUP BY 1,2 HAVING count(*)>1` → empty.
- **Consent state preserved:** sample Odoo Expired/Withdrawn consents →
  `GET` consent → `status`/`consent_lifecycle` match source; `valid_till` matches.
- **Seat truth:** `GET /license?tenant=3` → DPCM `consumed_users` rose by #new principals;
  `ConsumedLicenseLog` count == #new principals; `expires_users` unchanged; `active`
  still true.
- **Tenant isolation:** `SELECT tenant_id,count(*) FROM consents GROUP BY 1` → only 3.

### 9.6 Rollback plan
1. Delete migrated rows by `MigrationSourceMap` (entity in consent/request/...) for
   tenant 3; remove the map rows.
2. Delete the orphaned principal `User` rows created by the load (those with a
   `ConsumedLicenseLog` from this run / no other references).
3. Decrement `License.used_users` by the number removed and delete their
   `ConsumedLicenseLog` rows.
4. Optionally lower `total_users` back to contract value (reverse of the bump; the
   preflight printed old→new).
5. If a license was **seeded** this run, delete it (no other consumers on fresh DB).
All steps are tenant-scoped and map-driven → no collateral on live data.

---

## Deliverable index
1. Current architecture — §1
2. License enforcement map — §2
3. Historical-state preservation analysis — §0, §3, §5
4. Candidate solutions — §4
5. Recommended solution — §6
6. Risk assessment — §8
7. Implementation plan — §9.1–9.4
8. Validation plan — §9.5
9. Rollback plan — §9.6

Cross-refs: `[[migration-ext-pattern]]`, `[[migration-endpoints-and-order]]`,
`[[migration-wrong-tenant]]`, `[[consent-runall-delete-reload]]`,
`[[loader-nan-date-trap]]`.
