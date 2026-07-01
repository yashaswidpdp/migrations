# Remaining Migration Validation Work

**Date:** 2026-06-19
**Input:** reconciliation audit 2026-06-18 17:25 (LIVE)
**Question answered:** *What evidence is still missing before we can sign off the Odoo→Flask migration?* — NOT "how do we shrink mismatch counts."

Every reported gap below is classified as **REAL ISSUE** (migration fix), **BLIND SPOT** (reconcile fix), or **EXPECTED TRANSFORMATION** (document + FIELD_MAPS). Evidence inline.

---

## TL;DR

- The dominant "mismatch" noise — **`id` mismatched on every entity (311+22+27+6+54)** — is ONE reconcile blind spot (it value-compares the identity column, which differs by design). Not data.
- The dominant **real** issue is ONE root cause wearing two hats: **a live license gate blocks historical data during migration** → 129 consents + 20 requests rejected `"No active license available"`. Coverage *should* be 460 consent / 71 request once bypassed.
- Genuine smaller issues: 2 identity-less stakeholders (6, 38), DPO/PAManager role fidelity, a missing `accepted_loss.json`.
- Several "drops/mismatches" are reconcile artifacts (email not exposed by endpoint, Expired consents not in paginated list, request errors file lacks source-ids).

---

## 1. Confirmed Migration Issues (require fixes)

### 1.1 License gate blocks historical data — HIGH
- **Evidence:** `errors_processed_consents.csv` = 129 × `"No active license available"`; `errors_processed_requests.csv` = 20 × same. Migration user-creation (`migration_ext/routes.py` → `resolve_principal` → `_get_or_create_user` → `consume_license`, `utils/license_utils.py:36`) enforces **live seat capacity** even for historical backfill.
- **Why it's real:** historical consent/request migration should not be gated by current license inventory. This is "incorrect business-rule enforcement during historical migration."
- **Affected:** 129 consents + 20 requests (request odoo ids 4,5,9,10,11,13,15,16,18,19,20,21,22,28,29,34,35,36,44,71).
- **Fix:** bypass/auto-provision license in the migration create path (flag the migration endpoints to skip `consume_license`, or pre-seed adequate `License.total_users`). Then re-run `consent load` + `request load`.
- **Open decision:** is target coverage **331 or 460** (consent) / **51 or 71** (request)? Evidence says the shortfall is purely this gate → should be 460 / 71.

### 1.2 Identity-less stakeholders dropped — MEDIUM
- **Evidence:** source ids 6 (`DPDPTOOLS`) and 38 (`Raj`) both have `email: None` AND `phone: False`. The other 6 stakeholders carry emails and migrated. `resolve_principal` cannot create a backend user with no email and no phone.
- **Classification:** real, but a source-data limitation (no identity to key a user on).
- **Fix options:** (a) synthesize a placeholder identity (e.g. `stakeholder6@migration.local`) like the request loader already does for phone-only principals; or (b) record both in `accepted_loss.json` with reason "no email/phone in source." Decide which.

### 1.3 DPO vs PA Manager role fidelity — MEDIUM
- **Evidence:** stakeholder endpoint hardcodes `user_role_type=PAManager`; Odoo DPO distinction lost.
- **Fix:** if DPO must persist, backfill via `/stakeholder/<id>/update-roles`. Else document as accepted.

### 1.4 Missing `accepted_loss.json` — LOW (operational)
- **Evidence:** `migration/data/accepted_loss.json` **does not exist** → vendor `acc=0` → Vendor verdict wrongly flipped PASS*→GAP. test02 (odoo#4) is the same known accepted loss.
- **Fix:** restore the file with the vendor#4 entry (and stakeholder 6/38 if accepted). Operational, not data.

---

## 2. Reconciliation Blind Spots (require validation-logic fixes, NOT migration)

### 2.1 `id` "mismatch" on every entity — the big false positive
- **Evidence:** `id: '488' != '1'`, `id: '12' != '89'`, etc. The auto-pair comparator (`reconcile.py:749`) value-compares the identity column; Odoo id ≠ Flask id **by design**, and `dst_by_norm` even picks a nested object's `id` (the `'1'`). Counts: consent 311, vendor 22, PA 27, stakeholder 6, template 54.
- **Fix:** exclude `id` (and the entity join key) from the value-check. Clears the noise in one rule.

### 2.2 Consent 19 "DRIFT" — not loss
- **Evidence:** flask ids 4363–4381 all **exist** in `consents` (status=`Expired`) and all 19 are in the ledger. `flask db rows = 331 = ledger`. The DRIFT comes from `GET /consent/` returning 312 (Expired rows omitted / pagination), not the DB.
- **Fix:** compute DRIFT against DB rows or a fully-paged read, not the lossy list endpoint. Mark Cannot-Validate via that endpoint.

### 2.3 Request 20 "unexplained dropped" — actually license failures
- **Evidence:** all 20 are in `errors_processed_requests.csv` as `"No active license available"`, but reconcile reports `fail=0, unexp=20`. The errors CSV rows carry **no source id** the reconciler can match (0/20 matched), so they fall through to "unexplained."
- **Fix:** (a) make the request loader write the odoo source id into the errors CSV; (b) have reconcile read `errors_processed_requests.csv`. Then these reclassify as failed(license) → RECOVERABLE, same bucket as consents.

### 2.4 Stakeholder email "mismatch" (6) — email IS migrated
- **Evidence:** migrated users 968–972 have `email_encrypted = NOT NULL` AND `email_hash = NOT NULL` in the DB. The email migrated fine. `/auth/backend-users` returns `''` for email (PII not exposed), and reconcile also strips punctuation from the source side (`rahulyopmailcom`). Comparing a real source email to an endpoint that omits email is the artifact.
- **Fix:** classify email as **Cannot Validate** via this endpoint; verify at DB level instead. Not data loss.

---

## 3. Expected Transformations (document + add FIELD_MAPS)

| Transform | Evidence | Action |
|---|---|---|
| Odoo id → Flask id (all entities) | source-map remaps every id | document; exclude id from value-check (2.1) |
| Template `template_type` enum→name | `'privacy'→'privacynoticeemailtemplate'`, `'online'→'liveconsenttemplate'`, `'consent'→'legacyconsentemailtemplate'` | add FIELD_MAPS enum rule (30 rows) |
| Template `language` nested→scalar | source `{id:5,name:english}` flattened to `'id5nameenglish'` vs dest `'english'` — **same value** | fix flattener to take `.name`; artifact, not change (24 rows) |
| Template fan-out | 30 sources → 325 notice rows | already documented (sub_key) |
| Template `isDefault`(3)/`sub_type`(2)/`processingActivities[len]`(7) | minor | investigate small set; likely transform/normalization |

---

## 4. Coverage Review — unchecked fields classified

Legend: **MUST** validate · **NICE** · **CANNOT** (no comparable dest) · **DESIGN** (not migrated intentionally).

### Vendor (23 source, 4 checked, 17 unchecked)
| Field | Class | Reason |
|---|---|---|
| vendor_name | MUST | maps to `company_name` (diff name) — add FIELD_MAP |
| vendor_website_url | MUST | maps to `website` — add FIELD_MAP |
| nda_attachment.fileName / vra_attachment.fileName | MUST | now stored as `nda_document`/`dpa_document` paths (`vendor_<id>_nda/dpa_...`) — validate presence |
| contact_person / vendor_contact | NICE | contact fields; add map if exposed |
| state / current_status | NICE | maps onto vendor `status` — confirm enum mapping |
| nda/vra_attachment.fileContent | CANNOT | base64 blob; compare by stored-file existence, not content |
| bypass_vra, percent_attempted, last_overall_risk, last_overall_risk_dm, date_of_rollout, submission_date, last_updated_on | DESIGN | vendor-risk-assessment fields; vendor migration is profile-only |

### Stakeholder (3 unchecked)
- `phone` → MUST (exists encrypted in DB; validate at DB level). `is_active`, `login_date` → NICE/DESIGN.

### Processing Activity (13 unchecked) / Consent (2: consentRejectOn, deliveredOn) / Template (5)
- PA: mostly config flags (showOnDpgr, isOtpMandatory, consentValidity…) → NICE; `description` MUST if exposed.
- Consent `deliveredOn`/`consentRejectOn` → MUST if a dest date column exists (likely `delivery_on`) — add FIELD_MAP; else CANNOT.

---

## 5. Recommended Next Actions (prioritized)

1. **Expired-license migration (consents + requests)** — decide policy, bypass license gate, re-run. Resolves 129 + 20 in one fix. [HIGH]
2. **Dropped stakeholder records (6, 38)** — decide synthesize-identity vs accept-loss. [HIGH]
3. **Reconcile: exclude `id` from value-check** — clears 311+22+27+6+54 false mismatches. [HIGH, cheap]
4. **Reconcile: read request errors + write source-ids** — reclassify the 20 from "unexplained" to failed(license). [HIGH, cheap]
5. **Restore `accepted_loss.json`** (vendor#4 [+ stakeholder 6/38 if accepted]) — fixes false Vendor GAP. [MED]
6. **Vendor field coverage** — add FIELD_MAPS for `vendor_name→company_name`, `vendor_website_url→website`; validate NDA/VRA attachment presence. [MED]
7. **Template FIELD_MAPS** — `template_type` enum→name; fix `language` flattener (`.name`). [MED]
8. **Consent DRIFT source** — point DRIFT at DB rows / full-page read, not `GET /consent/`. [MED]
9. **Stakeholder email** — mark Cannot-Validate via endpoint; verify at DB. [LOW]
10. **DPO vs PA Manager** — decide role-fidelity backfill. [MED]
11. **Rotate secrets** (Odoo JWT + Flask API key). [LOW]

---

## Sign-off blockers (evidence still missing)

Cannot confidently sign off until:
- **License policy decided + re-run** → proves consent/request coverage is 460/71, not 331/51.
- **Stakeholders 6,38 resolved** → no silent identity drops.
- **Vendor/PA/consent MUST-fields actually validated** (via FIELD_MAPS or DB) → today they're *uncompared*, which is not the same as *correct*.
- **Attachment validation** → confirm migrated NDA/VRA/consent files exist + serve (naming refactor is in; presence not yet asserted by reconcile).
- **Reconcile false-positives removed** (`id`, request errors, email) → so the next report's PASS/GAP reflects data truth, not comparator artifacts.

---

# Migration Decisions Requiring Sign-Off

Business/data-policy decisions (not technical defects). Each needs an explicit owner answer before the related migration work is considered complete. The license-gate decision (D1) is the current blocker; D2–D4 are placeholders for decisions already surfaced elsewhere in this doc.

## D1 — Historical Consents vs Active Licenses — ✅ DECIDED 2026-06-19

**Decision (owner-confirmed):** migrate historical records. Consent target = `460/460`, request target = `71/71`. License status (active/expired/inactive) is NOT a migration blocker; the license gate enforces *current* business rules and applies only post-go-live (new consent/license/user actions), not to historical backfill. Principle: **Preserve Historical Truth**, not Enforce Current Business Rules.

**Mechanism:** ensure license capacity before load (not skip-consume). Rationale: the consent path calls `consume_license` in migration glue (`routes.py:292`, skippable), but the request path consumes inside core `Request.create_request` (uneditable per glue-only rule). Ensuring capacity covers both uniformly, touches no core, keeps seat accounting truthful (migrated principals are real users), and is reversible. Implemented as `dpdp_python/migration_ext/ensure_license.py` (idempotent capacity guard), run as a pre-load step. Endorsed by `reconcile.py:1305` ("Add license capacity then re-run").

**Run result (2026-06-19 11:40):** consent re-run **landed 460/460** (ledger + DB) — D1 consent objective met. The two reconcile flags on consent are endpoint artifacts, NOT loss: DRIFT 21 = 19 Expired + 2 Deemed omitted from `GET /consent/` (all 460 in DB); phone-mismatch 5 = email-only principals (ids 23/28 phone-collision retries, 383-385) where phone is unset by design.

**Request re-run STILL failed 20× `No active license available (tenant_id=1)`** — which exposed the real root cause below (D5). The license decision D1 is sound, but it is downstream of a tenant-targeting defect: the whole migration ran against the wrong tenant. The ensure-capacity mechanism is correct but must be applied to the tenant the data actually lands in, AND only after D5 is resolved. **D1 execution is therefore BLOCKED on D5.** See [[migration-wrong-tenant]].

### Background
Reconciliation shows:

```text
Consent Source Records:      460
Migrated Consents:           331
Failed Consents:             129

Failure Reason:
"No active license available"
```

The migration currently treats an inactive/expired license as a blocker for consent migration.

### Decision needed
Migration objective is:

```text
Migrate the existing system state into Flask.
```

NOT:

```text
Clean, filter, or re-qualify historical records before migration.
```

So consent migration should preserve historical records regardless of whether the associated license is currently active.

### Rationale
An expired license does not make historical consent data invalid.

```text
License Status Today = Expired
```

does not change:

```text
Consent Existed Historically
```

Migration should preserve: active, expired, historical, and inactive-license consents — unless a documented business requirement says otherwise.

### Migration principle

```text
Preserve Historical Truth
```

rather than:

```text
Enforce Current Business Rules
```

Current business validations apply to new consent creation, new license issuance, and user actions after go-live — not to excluding historical records during migration.

### Investigation required
**Current behaviour** — where is `No active license available` enforced?
- File / function / code path: confirmed at `dpdp_python/migration_ext/utils/license_utils.py:36` (`consume_license`), reached via `migration_ext/routes.py` → `resolve_principal` → `_get_or_create_user` → `consume_license`. (See §1.1.)

**Impact analysis** — for the 129 blocked consents, determine:
- how many attach to expired licenses
- how many attach to inactive licenses
- how many have no license relationship at all
- whether any would fail for reasons other than license status

**Migration feasibility** — confirm:
- expired-license consents can be imported safely
- historical license references can be preserved
- source-map integrity remains intact
- no duplicate records created

### Sign-off question
> Should consent migration represent the complete historical state of Odoo, or only the subset that passes today's business validations?

**Recommendation:**

```text
Complete Historical State of Odoo
```

→ consent target becomes `460 / 460`, not `331 / 460`. The 129 license-blocked consents are migration candidates, not accepted loss. Same logic extends to the 20 license-blocked requests (§2.3) → request target `71 / 71`.

## D2 — DPO Role Preservation
See §1.3. Decide whether Odoo DPO distinction must persist (backfill via `/stakeholder/<id>/update-roles`) or is accepted-loss under the hardcoded `PAManager` role.

## D3 — Identity-less Stakeholder Records (6, 38)
See §1.2. Decide synthesize-placeholder-identity vs record in `accepted_loss.json`.

## D4 — Attachment Retention / Template Expansion
Placeholder for future sign-off decisions (NDA/VRA/consent file retention policy, template fan-out behavior) once their MUST-field validation is run.

## D5 — Migration Ran Against the WRONG TENANT — 🚨 BLOCKER, decision PAUSED 2026-06-19

**Finding:** every migrated record — all 608 (460 consent, 51 request, 27 PA, 30 template, 11 vendor, 6 stakeholder, every `migration_source_map` row) — landed in **tenant 1 = "Local Dev" (skfinance.localhost.com)**, NOT the documented target **tenant 3 = "DPDP Consultants" (dodpconsultants.com)**. Cause: `migration/config/.env` `FLASK_API_KEY` is a stale **tenant-1** DPO token (`sub=2`, `tenant_id=1`); `_tenant_id()` returns 1 for every load. `seed_operator.py` was built to mint a tenant-3 token and a tenant-3 DPO (`id=207`) exists but was never used.

**Why it surfaced now:** the request re-run failed `No active license available for module 'DPGR' (tenant_id=1)`. Tenant-1's DPGR (id 2) and DPCM (id 1) are dev licenses at 100/100, auto-deactivated (`active=f`) when fully consumed. Consent's 460 passed only because all consent principals already existed (reused, no new seat); the 20 requests need NEW principals → hit the dead tenant-1 DPGR.

**Why re-pointing is non-trivial in this DB:** tenant 3 is a half-provisioned shell — has licenses (with capacity) + 134 templates, but **0 request types** (tenant 1 has 13). The loader resolves request types by name via `GET /request-types/` (operator-tenant scoped) → tenant 3 returns none → every request would fail `Invalid request_type_id`. Tenant 3 also has 0 PA / 0 vendors / 1 user. Additionally `reconcile.py` does NOT filter `migration_source_map` by tenant → after a tenant-3 run it would double-count tenant-1 + tenant-3 rows.

**Reality:** this is a local dev DB; tenant 1 is the only fully-provisioned tenant and is where the tooling was validated. Tenant correctness is fundamentally a **prod-cutover config concern** (point `FLASK_API_KEY` at a real tenant-3 token against a provisioned prod DB), not necessarily something to hand-build in dev.

**Options (PAUSED — owner to choose):**
- **(A) Document as prod-config, finish D1 in dev tenant 1:** record "migrate against a real tenant-3 token on the provisioned prod DB" as a cutover requirement; in dev, reactivate+expand tenant-1 DPGR and re-run request load → 71/71 to prove the license mechanism. No dev hand-provisioning.
- **(B) Fully provision tenant 3 in dev and re-run:** `create_request_type(3)` (tenant_routes.py:37), re-mint token, bump DPCM, purge tenant-1 data (or tenant-scope reconcile), full ordered re-migration. Faithful rehearsal, multi-step, dup/double-count risks.

**Required prep before EITHER path executes the reconcile:** give `reconcile.py` a tenant filter (or it cannot tell the two tenants apart).

**No further DB writes until this is decided.**
