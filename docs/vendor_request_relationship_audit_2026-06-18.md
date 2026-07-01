# Vendor Migration Validation — Vendor↔Request Relationship Audit

**Date:** 2026-06-18
**Scope:** Vendor migration, focus on Vendor↔Request linkage + data integrity.
**Source truth:** extracted `raw_*` snapshots (Odoo JWT expired this session; see Caveat).
**Destination:** live Flask Postgres (`privacium_postgres` / `privacium_db`).

---

## Summary

| Check | Result |
|---|---|
| **Overall verdict** | **PASS** (1 known accepted-loss) |
| Vendor counts | Odoo **12** → Flask **11** migrated, **1** missing (accepted), **0** duplicated |
| Request linkage | **N/A — no Vendor↔Request relationship exists in the source data**; 0 expected, 0 present, 0 lost, 0 mis-mapped |
| FK integrity | 0 orphans, 0 dangling, 0 null-where-existed |

**Headline finding:** The audit premise — that requests carry vendor associations which may have been lost in migration — **does not hold for this dataset.** Odoo DSAR/DPGR requests have no vendor linkage in the extracted source. The empty Flask link tables are **correct**, not data loss. Migration of vendors is sound; vendor↔request integrity is trivially preserved because there is nothing to preserve.

---

## Objective 1 — Vendor Count Validation

| Metric | Count | Evidence |
|---|---|---|
| Odoo source | 12 | `raw_vendors.json` length |
| Flask total | 11 | `SELECT count(*) FROM vendors;` → 11 |
| Migrated (source-map distinct) | 11 | `SELECT count(DISTINCT odoo_source_id) FROM migration_source_map WHERE entity='vendor';` → 11 |
| Missing | 1 | odoo#4 (see Findings) |
| Duplicated | 0 | `SELECT vendor_id,count(*) … HAVING count(*)>1` → empty |

Source-map (odoo→flask): `1→88 2→87 3→86 5→85 6→84 7→83 8→82 9→81 10→80 11→79 12→78`
(odoo#4 absent by design — accepted loss.)

---

## Objective 2 — Vendor→Request Link Validation

**Result: NOT APPLICABLE — no linkage in source.**

- `requests` table has **no vendor column**:
  `SELECT column_name FROM information_schema.columns WHERE table_name='requests' AND column_name ILIKE '%vendor%';` → **empty**.
- Only possible link paths are assoc tables `vendor_activities` (`request_id`,`vendor_id`) and `request_assigned_vendor` (`request_id`,`vendor_id`). Both **empty** (0 rows).
- `raw_requests.csv` columns carry **no vendor field**:
  `id, processingActivity, ragStatus, status, pAManager, requestNo, name, eMail, phone, actionDate, …, requestType, assignee_email, consent, dpComment, …`
- Source request types are all Data-Principal rights, **none vendor-assessment**:
  - 31 × `Right to withdraw consent`
  - 28 × `Legacy consent revoke request`
  - 6 × `Right to Nominate`
  - 5 × `Right to access information`
  - 1 × `Right to be informed`

Per-vendor request count = **0 (source) = 0 (dest)** for all 11 vendors. No vendors with differing counts. No requests lost a vendor association (none existed). No requests linked to the wrong vendor.

---

## Objective 3 — Foreign Key Integrity Check

| Check | Result | Evidence |
|---|---|---|
| Null vendor ref where Odoo had one | none | source has no request→vendor link |
| Vendor IDs → non-existent vendor | 0 | `va_orphans=0` |
| Orphaned references | 0 | `rav_orphans=0` |
| Invalid ID-remap mappings | none | validated via source-map identity, not raw ids |

Query:
```sql
SELECT
 (SELECT count(*) FROM vendor_activities va LEFT JOIN vendors v ON va.vendor_id=v.id
    WHERE va.vendor_id IS NOT NULL AND v.id IS NULL) AS va_orphans,
 (SELECT count(*) FROM request_assigned_vendor rav LEFT JOIN vendors v ON rav.vendor_id=v.id
    WHERE v.id IS NULL) AS rav_orphans;
-- => 0 | 0
```
Vendor id mapping validated by `migration_source_map` business identity (odoo_source_id ↔ flask_id), per requirement — not raw DB ids.

---

## Objective 4 — Request Functionality Verification

- **Request detail API:** contains **no vendor field by design** (no linkage). No discrepancy — correctly absent.
- **Vendor detail API:** works. Sample (migrated vendor flask#78 = odoo#12) returns vendor with correctly named, standardized document URLs:
  ```
  uploads/vendors/vendor_78_dpa_active_20260618103547_406b2e.docx
  uploads/vendors/vendor_78_nda_active_20260618103547_40e646.pdf
  ```
- **Vendor list/filter/search:** operates on the `vendors` table; unaffected by request linkage. 11 vendors visible.

No request→vendor display path exists to be wrong.

---

## Objective 5 — Migration Mapping Audit

- **Vendor id translation:** recorded at vendor create.
  `dpdp_python/migration_ext/routes.py:533` — `MigrationSourceMap.record("vendor", odoo_source_id, vendor.id, tenant_id)`
  Vendor created at `routes.py:500` (`create_vendor`), flushed at `:507`.
- **Source-map coverage:** 11/11 migrated vendors have entries; odoo#4 intentionally absent (accepted loss).
- **Request→vendor migration ordering dependency:** **NONE.** Request transform (`migration/scripts/transform/transform_request*.py`) and the request loader/`/migration/request` endpoint reference vendor **nowhere**. Vendor and request migrations are order-independent.
- **Re-run safety:** **SAFE.** Vendor migration is idempotent — a 409 "already migrated" is treated as skip via source-map (`load_flask.py` `load_vendors`), so flask ids stay stable on re-run. And no request relationship depends on vendor ids regardless, so re-running vendor migration cannot break request relationships.

---

## Findings

| # | Issue | Vendor | Src id | Dest id | Affected requests | Root cause | Severity |
|---|---|---|---|---|---|---|---|
| 1 | Vendor not migrated | test02 | odoo#4 | — | none | `test02@yopmail.com` already a DataPrincipal (user 1089); one user cannot be both DataPrincipal and Vendor. Accepted unmigrated 2026-06-17 (test data). | LOW (accepted) |

No relationship or FK findings — none exist.

---

## Recommendations

**Required fixes:** none for vendor↔request linkage — nothing was lost.

**Reconciliation improvements:**
- Add an explicit "linked-entity" assertion to the reconcile report: `vendor↔request: N/A (no source linkage)`, so future audits don't re-flag empty link tables as suspected loss.

**Migration ordering dependencies:**
- None between vendor and request. Document this explicitly so future maintainers don't introduce a false ordering constraint.

**Risks before production migration:**
- This PASS is **scoped to the current extracted dataset**, whose request types contain no vendor-assessment workflow. If *production* Odoo uses request types that populate `vendor_activities` / `request_assigned_vendor`, this audit **must be re-run against that data** — the present source has none, so linkage could not be evaluated for that case.
- `odoo#4`: keep in `accepted_loss.json`; re-evaluate if it represents real (non-test) production data.

---

## Caveat / Methodology

- Odoo JWT expired this session (`401 Token Expired`), so Odoo-side truth = extracted `raw_vendors.json` / `raw_requests.csv` snapshots, not a live recount. Counts are trustworthy for the snapshot; refresh `ODOO_JWT_TOKEN` to live-re-confirm.
- Destination evidence is live Postgres (`privacium_postgres`), queried directly.
- Environment: nested `dpdp_python` checkout; `./venv/bin/python` functional via absolute path; `dpdp_test` DB available; `raw_templates.json` is an error payload and was ignored (unrelated).
