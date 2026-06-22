# Backend Deep-Dive Audit — Odoo → Flask Migration Framework

Date: 2026-06-19
Scope: forensic, read-only analysis of the `migration/` repository.
Method: every model, transform, loader, extractor and the reconciler read in full;
the working-tree diff inspected; today's decision docs verified against code.

> **Scope correction (read first).** The audit brief assumes a single backend
> containing SQLAlchemy models, routes, services, auth, tenancy, consent/request
> lifecycle and licensing. **None of that lives in this repo.** This repo is the
> *migration framework* — an ETL client that talks to two remote systems over
> HTTP. The Flask backend (`dpdp_python`) it loads into is referenced
> (`load_flask.py:17` appends `../../../dpdp_python` to `sys.path`) but is **not
> present on disk** (`/home/yashaswi/Developer/dpdp_python` → ABSENT). Therefore
> Phases that target backend internals (routes, services, auth, tenant resolution,
> consent/request/license lifecycle, SQLAlchemy models) are analysed only to the
> extent this repo proves them; backend line-number claims in the docs cannot be
> verified here and are marked UNVERIFIABLE, not accepted.

---

## 1. Executive Summary

The system is a **stage-based ETL pipeline** (Extract → Transform → Load) that
migrates seven entity types from a legacy Odoo DPDP portal into a new Flask
"Privacium" backend, plus a **reconciliation auditor** that proves how much data
landed. It is glue code by design: it never edits the backend, only calls its
REST API. Idempotency and "did it land" are delegated to a server-side ledger
table, `migration_source_map`.

Engineering quality is high: defensive HTTP envelopes, idempotent re-runs,
identity-collision retries, a self-testing reconciler, and unusually candid
inline comments documenting backend limitations. The dominant risks are **not**
crashes — they are **silent fidelity/accounting gaps**:

| # | Risk | Severity | Proven from |
|---|------|----------|-------------|
| R1 | Reconciler does **no tenant filtering** — counts every tenant's rows | HIGH | `reconcile.py:209,222,236,848,1255` (no `WHERE tenant_id`) |
| R2 | `consent_fidelity_audit` doc **G1 is stale** — working tree already fixed it | MED (doc) | working-tree diff vs doc §4 G1 |
| R3 | `map_status` silently defaults unknown consent status → "Deemed Consent" | MED | `transform_consent.py:40` |
| R4 | FAILED (last-run) vs MIGRATED (cumulative) mixed in one ledger identity | MED | `reconcile.py` error_breakdown vs sourcemap |
| R5 | PA/template ledger writes are best-effort → audit can under-count (false GAP) | LOW-MED | `load_flask.py:43-72` |
| R6 | Synthetic identity fabrication (`{phone}@migration.local`) for request rows | LOW | `load_flask.py:141-143` |

Entities consent + request consume backend **license seats** at load time
(documented, external); the license doc's recommended fix (Option D, pre-provision
headroom in `ensure_license.py`) is the correct, glue-only approach.

---

## 2. Repository Architecture

```
Odoo REST API ──(extract)──► data/raw/*.csv|json
                                   │
                              (transform)
                                   ▼
                          data/processed/*.csv|json
                                   │
                                (load)
                                   ▼
                          Flask REST API  ──writes──►  Postgres (privacium_db)
                                   │                         │
                                   └──── migration_source_map ◄── ledger (idempotency + audit truth)
                                                             │
                          reconcile.py ──reads ledger + (--live) re-reads Odoo & Flask──► reconciliation_report.txt
```

### 2.1 Layers / responsibilities

| Layer | Files | Owns | Must never own |
|---|---|---|---|
| Orchestration | `main.py` (623 L) | Click CLI; one group per entity; stage wiring; logging banner | business mapping, HTTP |
| Extract | `scripts/extract/extract_odoo.py` (405 L) | Odoo auth, pagination, by-id enrichment, raw snapshot | field renaming |
| Transform | `scripts/transform/*.py` (7 files) | Odoo→Flask field/enum mapping, date normalisation, split files | network, persistence |
| Load | `scripts/load/load_flask.py` (1458 L) + `stakeholder_role_mapper.py`, `stakeholder_report.py` | POST/PUT to Flask, id resolution, idempotency, retries, ledger writes | source-of-truth counts |
| Schemas | `models/*.py` (dataclasses) | Document the Odoo source shape and Flask payload shape | DB/ORM (there is none here) |
| Report/Audit | `scripts/report/reconcile.py` (1428 L) | 4-layer count ledger, live field-diff, verdicts, requirements | mutating anything |
| Config | `config/.env` (gitignored), `.env.example` | secrets + dirs | — |
| Tests | `tests/test_reconcile.py` | counter/parsers unit coverage (reconcile only) | — |

**Coupling note:** `models/*.py` are **dataclasses**, not SQLAlchemy. They are
documentation + light typing of the wire contract. The loaders do **not**
instantiate them on the hot path — they build plain dicts from CSV rows. So the
dataclasses can silently drift from what the loaders actually send (already
visible: loaders add keys like `request_no`, `risk`, `consent_lifecycle` that the
dataclasses partially track).

### 2.2 Startup flow (`create_app` analogue)

There is no Flask app here. The runtime entry is the Click CLI:

```
main.py:cli()  (load_dotenv config/.env; log banner)
  └─ entity group (consent/request/request-type/processing-activity/template/vendor/stakeholder)
       └─ command (extract|enrich|transform|seed|load|approve|run_all|run-all)
            └─ delegates to scripts.{extract,transform,load} module functions
reconcile is a top-level command (not a group): main.py:601 → scripts.report.reconcile
```

Each entity module builds its own client object at call time
(`OdooExtractor` / `FlaskLoader`) from env — there is no shared app context, DI,
or connection pool. Tokens are read per-process from `config/.env`.

---

## 3. Data Models (wire schemas)

Six dataclass modules under `models/`, each pairing an **Odoo source** shape with
a **Flask payload** shape. Highlights and the real-world entity each represents:

| Model file | Source dataclass | Payload dataclass | Represents |
|---|---|---|---|
| `consent.py` | `OdooConsent` | `FlaskConsentPayload` | DPCM consent record (DPDP) |
| `request.py` | `OdooRequest` | `FlaskRequestPayload` | DPGR data-principal request |
| `vendor.py` | `OdooVendor(+User,+Activity)` | `FlaskCreateVendorPayload(+Activity)` | third-party processor + its users/activities |
| `processing_activity.py` | `OdooProcessingActivity` (tree) | `FlaskCreatePAPayload` | processing activity (master data, hierarchical) |
| `template.py` | `OdooTemplate` | `FlaskCreateTemplatePayload` | notice/consent/SMS template |

Enum allow-lists are encoded as module constants (e.g.
`FLASK_CONSENT_STATUSES`, `FLASK_TEMPLATE_TYPES`, `FLASK_LANGUAGES`) and act as
the contract reference for the transforms. The `to_dict()` methods on the Flask
payloads omit empty/None keys — important because the loaders rely on "absent key
⇒ backend default" semantics throughout.

**Data-integrity observations (this layer):**
- Identity is remapped: Odoo `id` ≠ Flask `id`. The only join is
  `migration_source_map(entity, odoo_source_id → flask_id, sub_key)`.
- `template` is **1-to-many** (one Odoo template fans out to many Flask rows,
  one per type/channel/language) — recorded under one `odoo_source_id` with a
  distinct `sub_key`. The reconciler correctly counts DISTINCT source ids
  (`reconcile.py:209`).
- Processing activities arrive as a **tree**; counted by walking id+name nodes
  (`count_tree_nodes`).

---

## 4. Extract Layer

`OdooExtractor` (`extract_odoo.py`) — Bearer JWT + `session_id` cookie. Three
fetch modes: paginated (`fetch_records`), single-shot (`fetch_simple`), by-id
(`fetch_by_id`). Two enrichers backfill dashboard-omitted fields from per-record
`/dpcm/id` and `/dpgr/id` endpoints.

Strengths:
- **HTTP-200 auth-error envelope guard** on all three fetchers
  (`:70-75,145-150,177-182`) — Odoo wraps `{"status_code":401,"message":"Token
  Expired"}` in a 200; the code aborts loudly instead of saving an empty dataset.
- Pagination uses server `pagination.total_page` when present, else a short-page
  heuristic (`:104-111`).

Proven weaknesses:
- **Best-effort list discovery** (`:82-88`): if known keys miss, it grabs *the
  first list-valued key in the dict*. A response carrying an unrelated array
  first could capture the wrong list. Low likelihood, silent if hit.
- `MAX_RECORDS`/`BATCH_SIZE` from env; `MAX_RECORDS=0` means unlimited — fine,
  but a stale small value in `.env` would silently truncate a real run.

---

## 5. Transform Layer

Pure functions, file-in/file-out, no network. Each maps Odoo fields/enums to the
Flask contract and normalises dates.

Key behaviours and **proven silent-default risks**:

| Transform | Default-on-unknown | Line |
|---|---|---|
| `map_status` (consent) | unknown status → **"Deemed Consent"** | `transform_consent.py:40` |
| `map_processing_type` | non-mandatory → **"Promotional"** | `:46` |
| `map_consent_type` | non-paper → **"Digital"** | `:52` |
| `map_legacy_type` | non-live → **"Legacy"** | `:58` |
| `map_request_status` | unmapped → **"Initiated"** | `transform_request.py:64` |
| `map_rag_status` | unknown → **"Green"** | `:69` |

These are reasonable fallbacks but they **mask source data-quality problems**: an
Odoo status the mapper doesn't recognise is silently rebranded, not flagged. The
reconciler's `--live` field-diff (status compared with `_n_token`) is the
backstop that would surface such a divergence — so the two systems are
complementary by design.

`transform_consent` **splits** output three ways: combined
`processed_consents.csv` (the file actually loaded by the active path) plus
`_paper.csv` / `_legacy.csv` (consumed only by the dead Excel-import paths).

`transform_request_type.py` (new, untracked) enforces two backend constraints
client-side: single `is_revoke=True` per tenant (first wins, rest demoted+logged)
and drops un-cross-mappable Odoo department ids.

---

## 6. Load Layer

`FlaskLoader` (`load_flask.py`) — the heart of the system. Per-entity loaders,
all sharing: NaN→None coercion, id resolution via GET, idempotency via 409
handling, error CSV emission.

### 6.1 Idempotency model (two patterns)

1. **Native-route entities** (PA, template, request-type): created via the real
   create endpoints, idempotent by **name** (400 "already exists" ⇒ skip). The
   ledger is then **back-filled** via a best-effort `POST /migration/source-map`
   (`_record_source_map`, `:43-72`).
2. **Migration-route entities** (consent, request, vendor, stakeholder): created
   via `/migration/*`, idempotent by **source-map** server-side (409 "already
   migrated" ⇒ skip).

### 6.2 Identity-collision handling (a genuinely subtle, well-handled case)

Source data reuses dummy phone numbers. A `409` mentioning "Data Principal" /
"phone" means the carried phone resolves to a *different* existing principal than
the email. The loaders **retry email-only** so the record still attaches to the
right person (`:163-178` requests, `:248-251` vendors, `:1366-1372` consents). A
`409` *without* that phrase is the true idempotent skip. This distinction is
correct and consistent across loaders.

### 6.3 Proven risks here

- **R6 — synthetic identity:** when a row has no email, the generic loader
  fabricates `{phone}@migration.local` (`:141-143`). Documented, but it means
  some principals carry a synthetic email as their key.
- **R5 — ledger best-effort:** `_record_source_map` swallows all failures as
  warnings (`:64-72`). PA/template creation stays idempotent (name-based), but if
  the source-map POST fails, the **reconciler under-counts MIGRATED** → false
  GAP/UNKNOWN verdict for an entity that actually landed.
- **Enum form-name trap (correctly handled):** `_PROCESSING_TYPE_FORM`
  (`:1044-1047`) maps the *value* to the enum *member name* the importer expects,
  with an explanatory comment that sending the value would `KeyError`→silently
  default. Good defensive note.
- **Dead paths retained:** `load_legacy_via_import` / `load_paper_via_import`
  (`:1049,:1129`) are not wired into `run_all`; the consent doc warns that
  reverting to them forces status→"Deemed Consent" and drops dates. Keeping dead,
  lossy paths in the file is a footgun, even if currently inert.

---

## 7. Reconciliation / Audit Layer (`reconcile.py`)

The most sophisticated component. Four count layers per entity (SOURCE / STAGED /
MIGRATED / FAILED), verdicts (PASS / PASS* / RECOVERABLE / GAP / DRIFT / UNTRACKED
/ UNKNOWN), an id-level diff for "unexplained" drops, and an opt-in `--live` mode
that re-reads Odoo (source) and the live Flask app (dest) and runs a **field-level
value-equality diff** joined through the source-map.

Design strengths:
- Every external read **degrades to `None`/"unknown"** rather than raising — the
  report always renders.
- `--live` field diff only flags a field when **both sides carry a value**
  (`compare_record:738,772`) — disciplined against false positives. The
  working-tree diff adds an **id-column skip** (`:751-757`) precisely to kill
  false `'488' != '1'` mismatches from remapped ids — a correct fix.
- Self-test (`--self-test`) asserts ledger identity offline.

### 7.1 R1 — No tenant filtering (HIGH, proven)

Every Postgres read is global:
- `sourcemap_counts` `:209`, `sourcemap_ids` `:222`, `sourcemap_pairs` `:236`,
  `db_table_count` `:848`, relationship `_count` `:1255` — **none** carries a
  `WHERE tenant_id = …`.

If `privacium_db` holds more than one tenant (and the license doc's
`[[migration-wrong-tenant]]` incident proves records have landed in the wrong
tenant before), MIGRATED and `flask db rows` **sum across tenants** and can show a
falsely complete (or inflated) migration. The license decision doc §8 itself flags
this ("reconcile.py lacks one — fix per that memo"). **This is the single most
material correctness gap in the audit tooling.**

### 7.2 R4 — Cumulative vs last-run mixing (MED)

MIGRATED comes from the cumulative ledger; FAILED comes from
`errors_*.csv`, which each load run **overwrites** (and the consent loader even
deletes on a clean run, `:1393-1396`). The ledger-identity arithmetic
(`source = migrated + failed + accepted + unexplained`) therefore mixes a
cumulative term with a last-run term. After a partial re-run, `unexplained` can be
mis-attributed. The id-level `missing_ids` path (authoritative when available)
mitigates this, but the summary arithmetic does not always use it.

### 7.3 Minor

- `--live` drift uses `ledger_fids - present`; if a live page returns empty early
  with no pagination meta, the walker breaks and under-reads → could surface
  **false DRIFT**. Mitigated by meta-driven paging + `None` on exception, but the
  no-meta short-circuit (`:360`) is the soft spot.
- Field-diff auto-pairs by normalised leaf name across flattened nested objects;
  a same-named nested scalar could mis-pair. Mitigated by id-skip + both-present
  rule, but not impossible for fields like `name`.

---

## 8. Documentation Verification (Phase 13)

| Document | Claim | Code reality | Verdict |
|---|---|---|---|
| `consent_fidelity_audit_2026-06-19.md` §4 **G1** | "time-of-day dropped on ALL dates… `format_consent_date` → dd/mm/YYYY" | **Working tree already replaces those calls with `format_consent_datetime` (`%Y-%m-%d %H:%M:%S`)** preserving time | **STALE / CONTRADICTED (R2).** Doc not updated for the uncommitted fix; G1 is effectively closed in code. |
| same, §0 "two paths, only `/migration/consent` wired" | `main.py:113,134` call `run_consent_migration_loading`; Excel paths unused | **MATCH** |
| same, G2–G7 (route reads `created_on`/`last_updated`/`closedOn`?) | references `migration_ext/routes.py` line numbers | backend **ABSENT** | **UNVERIFIABLE** — cannot confirm from this repo; treat as backend TODO, not proven. |
| `license_enforcement_decision_2026-06-19.md` §0 "no per-consent license FK" | `grep license models/*.py` → 0 hits (confirmed here) | **MATCH** (for the dataclasses present) |
| same, §6 "fix lives in `ensure_license.py`" | file is in backend (`migration_ext/`), **ABSENT** here | **UNVERIFIABLE** (sound reasoning, external code) |
| `reconcile.py` docstring "all six entities tracked; migrated = DISTINCT source ids" | matches `sourcemap_counts` DISTINCT query | **MATCH** |
| README / inline "idempotent re-runs" | 409 + name-skip patterns present | **MATCH**, except R5 best-effort ledger caveat |

**Headline:** the only outright doc-vs-code contradiction is **R2 (G1 staleness)**
— and it's because the code got *ahead* of the doc (the fix is uncommitted). The
backend-line-number claims are internally consistent and plausibly correct but
**cannot be checked** because `dpdp_python` is not in this tree.

---

## 9. Runtime Traces (proven, this repo)

**Consent load (active path):**
```
consent load → run_consent_migration_loading → FlaskLoader.load_consents_via_migration
  read processed_consents.csv → per row: NaN→None
  resolve processing_activity_name → pa_id (GET /processing/activities/simple)
  POST /migration/consent
    ├─ 200/201 → ok
    ├─ 409 + "phone" → retry email-only → re-POST
    ├─ 409 + "already migrated" → idempotent skip (counted ok)
    └─ else → errors_processed_consents.csv
  clean run with 0 failures → delete stale errors file
```

**Request load (generic path):** `run_loading("processed_requests.csv",
"/migration/request")` → `load_from_csv` resolves PA names, request-type
name→id (else default), parses `assigned_users`, fabricates email if absent,
posts, with the same 409 identity-retry logic.

**Processing-activity load:** topological sort (roots before children) →
resolve parent_id from a running name→id map → create → record ledger →
`patch_links` second pass backfills template links + effective-from dates that
`create` ignores.

**Template load+approve:** create as Draft (`approval=False`) → ledger with
`sub_key=type|sub_type|language` → PUT `approval=True,status=Active` to activate
and persist `effective_from`.

---

## 10. Proven Risks (consolidated, code-backed only)

1. **R1 (HIGH):** reconciler has no tenant filter → cross-tenant over-count /
   false PASS. `reconcile.py:209,222,236,848,1255`.
2. **R2 (MED, docs):** `consent_fidelity_audit` G1 stale vs uncommitted
   `transform_consent` datetime fix. Update the doc or the doc misleads the next
   engineer into "re-fixing" a closed gap.
3. **R3 (MED):** silent enum defaults across transforms rebrand unknown source
   values (esp. `map_status`→"Deemed Consent"). Source data-quality issues hide.
4. **R4 (MED):** cumulative MIGRATED vs last-run FAILED in one identity →
   `unexplained` mis-attribution after partial re-runs.
5. **R5 (LOW-MED):** best-effort `_record_source_map` → PA/template audit
   under-count (false GAP) if the ledger POST fails though create succeeded.
6. **R6 (LOW):** `{phone}@migration.local` synthetic identity for email-less rows.
7. **R7 (LOW):** dead lossy consent-import paths retained in `load_flask.py`.
8. **R8 (LOW):** extract "first list in dict" fallback could grab the wrong array.

Not bugs (verified, intentional): identity-collision email-retry; reuse consumes
no seat; template fan-out distinct-count; "absent key ⇒ backend default" date
semantics; `config/.env` is gitignored (`.gitignore` `.env` matches `config/.env`;
only `.env.example` is tracked).

---

## 11. Engineering Conclusions

- This is a **mature, defensively-written ETL client**, not a backend. Judge it as
  a data-migration tool: its correctness contract is *fidelity + idempotency +
  auditable completeness*, and it largely meets it.
- The **biggest real exposure is the audit tool itself** (R1): an un-tenant-scoped
  reconciler can declare success that isn't true in a multi-tenant DB. Fix this
  before trusting any PASS verdict in a shared database.
- **Fidelity gaps are timestamp-level, not status-level.** Status/lifecycle are
  preserved verbatim; the open questions (created_at source, updated_at clobber,
  closedOn, valid_till fabrication) live in the **backend** `migrate_consent` route
  and cannot be closed from this repo — they need `dpdp_python` access.
- **Documentation discipline is strong but lagging the code by one commit** (R2).
  The decision docs are unusually rigorous (they even correct the brief's wrong
  mental model of licensing) — keep them, but reconcile G1 with the working tree.
- **Next actions, ranked:** (1) tenant-filter every `_psql` query + add a
  pre-load `g.tenant.id` assertion; (2) update consent G1 in the doc; (3) make
  unknown-enum defaults log a WARN with the raw value; (4) decide whether FAILED
  should be cumulative to match MIGRATED.

### Verifiability ledger
- Verified in-repo: extract, transform, load, reconcile, models, CLI, config,
  gitignore, tests.
- Unverifiable (backend absent): all `migration_ext/routes.py`,
  `license_utils.py`, `ensure_license.py`, SQLAlchemy model, route/service/auth/
  tenant internals. Get `dpdp_python` on disk to close Phases 5–9 properly.
