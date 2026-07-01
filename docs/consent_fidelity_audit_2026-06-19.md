# Consent Data-Fidelity Audit — Odoo → Flask

Date: 2026-06-19
Scope: field-level fidelity of the consent migration (status, lifecycle, all dates).
This is a **data-fidelity** audit, separate from the earlier license-execution doc
(`license_enforcement_decision_2026-06-19.md`).

---

## 0. Which path actually runs (important)

There are **two** consent load paths in the repo. Only one is wired into `run_all`:

| Path | Endpoint | Fidelity | Wired in? |
|---|---|---|---|
| `load_consents_via_migration` (`load_flask.py:1322`) | `POST /migration/consent` (direct insert) | preserves status + Odoo dates | **YES** — `main.py:113,134 run_consent_migration_loading` |
| `load_legacy_via_import` / `load_paper_via_import` (`load_flask.py:1049,1129`) | `POST /consent/import` (Excel importer) | **legacy mode FORCES status→"Deemed Consent" and drops the date** (`load_flask.py:1052,1059`) | NO — dead path |

So the active pipeline is the fidelity-preserving one. **If anyone reverts to the
`/consent/import` legacy path, every digital consent silently becomes "Deemed
Consent" with `created_at = now` — total status + date loss.** Keep `run_all` on
`/migration/consent`.

Active chain:
```
Odoo /dpcm/dashboard CSV
  → transform_consent.py  (transform_consent_data)
  → data/processed/processed_consents.csv
  → load_consents_via_migration → POST /migration/consent
  → migrate_consent (migration_ext/routes.py:248) → Consent INSERT
```

---

## 1. Odoo side (source)

Fields the transform reads from the Odoo dashboard row (`transform_consent.py:104-156`):

| Concern | Odoo field |
|---|---|
| Status | `status` (string, e.g. "Expired", "Withdrawn", "Consented", "Deemed") |
| Lifecycle | (none separate — `status` seeds both) |
| Created | `createdOn` |
| Last updated | `lastUpdatedOn` |
| Sent | `sentOn` |
| Delivered | `deliveredOn` |
| Valid till (expiry) | `validTill` |
| Reject date | `consentRejectOn` |
| Closed date | `closedOn` |
| Type flags | `paperType`, `legacyType`, `userActivityType` |
| Evidence | `artifactId`, `iPAddress`, `deviceType`, `dpgrRequestNo` |
| Identity | `eMail`, `phone`, `name`, `pAManager`, `processingActivity` |

**How "expired" is determined in Odoo:** *explicit* — the source carries a `status`
string literally equal to "expired" (`map_status` `transform_consent.py:32`), plus an
explicit `validTill` date. Expiry is a stored value, not computed at read.

---

## 2. Flask side (target)

`Consent` model (`models/consent.py`):

| Concern | Flask column | Notes |
|---|---|---|
| Status | `status` (`ConsentStatus` enum, `:178`) | stored verbatim; has `EXPIRED`, `WITHDRAWN`, … |
| Lifecycle | `consent_lifecycle` (`ConsentLifecycleEnum`, `:86`) | separate stored column |
| Decision | `consent_decision` (`:74`) | PENDING/CONSENTED/REJECTED |
| Created | `created_at` (`:192`, default now) | |
| Updated | `updated_at` (`:194`, default now, **`onupdate=now()`**) | |
| Consented | `consented_on` (`:193`) | |
| Closed | `closed_on` (`:195`) | |
| Valid till | `valid_till` (`:196`, default now) | |
| Sent | `sent_on` (`:197`) | |
| Delivered | `delivery_on` (`:198`) | |

**How "expired" is determined in Flask — two independent mechanisms:**

1. **Stored `status` enum** = source of truth for display. Listing endpoint filters
   `Consent.status == ConsentStatus.EXPIRED` *only* — comment at `routes/consent/listing.py:192`
   explicitly says "valid_till NULL check removed — status is the source of truth".
   `to_dict` serializes `self.status.value` (`models/consent.py:346`) — stored, never
   recomputed. **→ a migrated Expired consent displays Expired regardless of dates.** ✅
2. **Computed `is_valid()`** (`:412`) = `now < valid_till`. Used only for action-gating
   (e.g. `can_withdraw`), **not for display**. Does not flip stored status.

**One cron mutates stored status:** `expire_stale_consents`
(`tasks/cleanup_tasks.py:30`) flips `INITIATED/DEEMED/CONSENTED/DELIVERED → EXPIRED`
where `valid_till < now`. It is **one-directional** — it *never* does EXPIRED→active,
and explicitly skips WITHDRAWN/REJECTED/EXPIRED (`:34-35`). So Requirement "no
expired→active" is structurally guaranteed; but see §4 gap G4 for the *forward* drift
it can cause on migrated rows.

Dates are **stored** (not computed) — every `valid_till`/`sent_on`/etc. is a real
column value written at insert.

---

## 3. Field-by-field migration map

`R?` = does `migrate_consent` (`routes.py`) actually read the payload key.

| Odoo field | transform key | Read by route? | Flask column | Migrated | Verified |
|---|---|---|---|---|---|
| `id` | `odoo_source_id` | ✅ `:256` | `MigrationSourceMap` | ✅ | idempotency key |
| `status` | `status` | ✅ `:295` | `status` | ✅ | **verbatim incl Expired/Withdrawn** |
| `status` | `consent_lifecycle` | ⚠ key ignored; lifecycle **derived** from `status` `:296` | `consent_lifecycle` | ✅ | same source value → equivalent |
| (derived) | — | `:297-302` | `consent_decision` | ✅ | from status |
| `paperType` | `consentType` | ✅ `:320` | `consentType` | ✅ | Digital/Paper |
| `legacyType` | `legacyType` | ✅ `:321` | `legacyType` | ✅ | Legacy/Live |
| `userActivityType` | `processingType` | ✅ `:322` | `processingType` | ✅ | |
| `sentOn` | `sent_on` | ✅ `:307` | `sent_on` | ⚠ | **time-of-day dropped** (G1) |
| `deliveredOn` | `delivered_on` | ✅ `:308` | `delivery_on` | ⚠ | time dropped (G1) |
| `validTill` | `valid_till` | ✅ `:309` | `valid_till` | ⚠ | **fabricated if empty/unparseable** (G2); time dropped |
| `consentRejectOn` | `consent_reject_on` | ✅ `:310` | `closed_on` | ✅ | reject/withdraw date |
| `sentOn`/`deliveredOn` | `consent_date` | ✅ `:306` | `created_at` (`:335`) | ⚠ | **created_at = sentOn, NOT Odoo `createdOn`** (G3) |
| `createdOn` | `created_on` | ❌ **not read** | `created_at` | ❌ | dropped — superseded by sentOn (G3) |
| `lastUpdatedOn` | `last_updated` | ❌ **not read** | `updated_at` | ❌ | **`updated_at` = migration run time** (G4) |
| `closedOn` | `closed_on` | ❌ **not read** | `closed_on` | ❌ | only `consentRejectOn` maps; `closedOn` ignored (G5) |
| (derived) | — | `:336` | `consented_on` | ⚠ | = `consent_date` (sentOn) when decision CONSENTED; no distinct Odoo consentedOn captured (G6) |
| `artifactId` | `artifact` | ✅ `:330` | `artifact` | ✅ | verbatim |
| `iPAddress` | `ip_address` | ✅ `:332` | `ip_address` | ✅ | |
| `deviceType` | `device_type` | ✅ `:333` | `device_type` | ✅ | |
| `dpgrRequestNo` | `request_no` | ✅ `:334` | `request_no` | ✅ | |
| `eMail` | `email` | ✅ `:270` | `user.email` | ✅ | |
| `phone` | `phone` | ✅ `:271` | `user.phone` | ✅ | |
| `pAManager` | `manager_name` | ❌ popped by loader `:1353` | `manager_id` | ❌ | manager link dropped (G7, minor) |

---

## 4. Fidelity gaps (ranked)

**G2 — `valid_till` fabricated when Odoo `validTill` is missing/unparseable.**
`routes.py:311-313`: if `valid_till` parses to None, it sets
`valid_till = now + PA.consent_validity_months` (default 12). For an **expired** consent
whose source `validTill` was blank or in a format `format_consent_date` couldn't parse,
Flask stores a **future** expiry date. The stored `status` stays "Expired" (display
correct ✅), but the **expiry date is wrong/fabricated** → violates "expiry dates
preserved". *Highest-impact gap because it directly hits the user's stated concern.*

**G4 — `updated_at` overwritten to migration run time; `lastUpdatedOn` dropped.**
The route never reads `last_updated`, and `updated_at` has `onupdate=now()`
(`models/consent.py:194`), so every consent's `updated_at` = the migration timestamp,
not Odoo's. The request path explicitly fixes this (`routes.py:205-209`); the **consent
path does not**. Violates "historical timestamps preserved". *Secondary effect:* the
`expire_stale_consents` cron can then flip a migrated `CONSENTED/DEEMED` whose
`valid_till < now` to `EXPIRED` post-load — a *forward* status drift (never the
forbidden reverse), but still an alteration of migrated state.

**G3 — `created_at` sourced from `sentOn`, not Odoo `createdOn`.** `createdOn` is
transformed but never read; `created_at = consent_date(=sentOn) or sent_on or now`
(`routes.py:335`). Usually `sentOn≈createdOn`, but they can differ. Violates "historical
timestamps preserved" strictly.

**G5 — `closedOn` ignored.** Only `consentRejectOn → closed_on`. For a **withdrawn**
consent whose withdrawal time is in Odoo `closedOn` (not `consentRejectOn`), the
withdrawal timestamp is lost. Status "Withdrawn" preserved ✅, but the *when* may be
empty.

**G1 — time-of-day dropped on ALL dates.** `format_consent_date`
(`transform_consent.py:72-90`) normalises every timestamp to `dd/mm/YYYY`, so
`2024-03-15 14:30:00` → midnight UTC. Date preserved, intra-day precision lost across
`sent_on/delivered_on/valid_till/consent_reject_on/created_at`.

**G6 — no distinct `consentedOn` captured.** `consented_on` is derived from `sentOn`.
If Odoo carries a separate consent-granted timestamp, it isn't migrated. *Unverified —
depends on Odoo schema; confirm whether `/dpcm/dashboard` exposes a consentedOn field.*

**G7 — `manager_id` not linked** (loader pops `manager_name`). Minor; not a
status/date concern.

---

## 5. Verdict against the success criteria

| Requirement | Status | Why |
|---|---|---|
| Status preserved | ✅ **PASS** | verbatim; display reads stored `status` (`listing.py:192`, `to_dict:346`) |
| Consent lifecycle preserved | ✅ **PASS** | derived from same source status |
| No expired→active | ✅ **PASS (structural)** | no reverse transition exists anywhere; cron is active→EXPIRED only |
| Expiry dates preserved | ⚠ **PARTIAL** | preserved when source present+parseable; **fabricated future date if missing** (G2); time dropped (G1) |
| Historical timestamps preserved | ❌ **PARTIAL FAIL** | `created_at`=sentOn not createdOn (G3); `updated_at`=run time (G4); `closedOn` dropped (G5); time dropped (G1) |
| No consent dates altered | ❌ **EDGE-CASE FAIL** | `updated_at` rewritten (G4); `valid_till` fabricated when missing (G2); cron may forward-drift CONSENTED+past→EXPIRED |

Bottom line: **the displayed Expired/Withdrawn status is faithfully preserved and can
never silently revert to Active** — the core worry is safe. But several **timestamp**
fidelity gaps remain (G2–G5), and G2 is the one that touches expiry directly.

---

## 6. Recommended fixes (glue-only, ranked)

All land in `migration_ext/routes.py:migrate_consent` + the transform — **no core edits**.

1. **G2:** when `validTill` is absent, do **not** fabricate a future date for records
   whose status is terminal (Expired/Withdrawn/Rejected). Either leave `valid_till`
   NULL or set it to `closed_on`/`consent_reject_on`. Change the fallback at
   `routes.py:311-313` to skip when `status_enum` ∈ {EXPIRED, WITHDRAWN, REJECTED}.
2. **G4:** read `last_updated` and set `consent.updated_at` **last, before commit**
   (mirror the request path `routes.py:205-209`), so `onupdate=now()` can't clobber it.
3. **G3:** prefer Odoo `createdOn` for `created_at`; read the `created_on` key the
   transform already emits, fall back to `consent_date`.
4. **G5:** map `closedOn → closed_on` when `consent_reject_on` is absent.
5. **G1:** carry full `YYYY-MM-DDTHH:MM:SS` through the transform (the route's
   `_parse_date` already accepts ISO `:54`) instead of truncating to `dd/mm/YYYY`.
6. **G6:** if `/dpcm/dashboard` exposes a consentedOn field, add it to the transform
   and read it for `consented_on`.

## 7. Validation (post-fix)

- Pick 5 Odoo consents per status (Expired/Withdrawn/Consented/Deemed/Rejected); diff
  Odoo `status, validTill, createdOn, lastUpdatedOn, closedOn` vs Flask
  `status, valid_till, created_at, updated_at, closed_on` → exact match (date+time).
- `SELECT status, count(*) FROM consents WHERE tenant_id=3 GROUP BY status` vs the Odoo
  status histogram → identical counts (no Expired collapsing into Deemed).
- Assert **zero** Expired consents with `valid_till > now` originating from blank source
  validTill (catches G2 fabrication).
- Run `expire_stale_consents` once, re-diff statuses → confirm no migrated row drifted
  unexpectedly (catches G4 cron interaction).

Cross-refs: `[[consent-type-mapping]]`, `[[consent-runall-delete-reload]]`,
`[[loader-nan-date-trap]]`, `[[migration-endpoints-and-order]]`.
