"""Migration reconciliation audit (Odoo -> Flask).

Builds a per-entity source-vs-destination ledger across the whole pipeline and
renders a single, human-readable .txt audit report. The goal is to *prove* how
much data landed (and explain every record that did not), instead of eyeballing
the migration log.

Four count layers per entity:
    1. SOURCE    data/raw/*               what Odoo gave us
    2. STAGED    data/processed/*         what transform produced
    3. MIGRATED  migration_source_map     what actually landed in Flask
    4. FAILED    data/processed/errors_*  rejected rows + grouped reasons

MIGRATED counts read the live Postgres `migration_source_map` via
`docker exec ... psql` so this audit needs no new Python DB dependency. Every
external read degrades to "unknown" rather than raising, so the report always
renders. Container / creds are overridable via env:
    RECON_PG_CONTAINER (default privacium_postgres)
    RECON_PG_USER      (default yashaswi)
    RECON_PG_DB        (default privacium_db)
"""

from __future__ import annotations

import collections
import csv
import json
import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

RAW_DIR = os.getenv("DATA_RAW_DIR", "data/raw")
PROC_DIR = os.getenv("DATA_PROCESSED_DIR", "data/processed")
DATA_DIR = os.getenv("DATA_DIR", "data")
REPORT_PATH = os.path.join(PROC_DIR, "reconciliation_report.txt")
ACCEPTED_LOSS_FILE = os.path.join(DATA_DIR, "accepted_loss.json")

PG_CONTAINER = os.getenv("RECON_PG_CONTAINER", "privacium_postgres")
PG_USER = os.getenv("RECON_PG_USER", "yashaswi")
PG_DB = os.getenv("RECON_PG_DB", "privacium_db")

BAR_WIDTH = 28


# --------------------------------------------------------------------------- #
# Low-level counters (all return None on any failure -> "unknown")
# --------------------------------------------------------------------------- #
def count_csv_rows(path: str) -> Optional[int]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, newline="", encoding="utf-8") as f:
            return sum(1 for _ in csv.DictReader(f))
    except Exception:
        return None


def count_json_list(path: str, *list_keys: str) -> Optional[int]:
    """Count items in the first matching top-level list key (e.g. 'vendors')."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, list):
            return len(d)
        for k in list_keys:
            node = d.get(k) if isinstance(d, dict) else None
            if isinstance(node, list):
                return len(node)
            if isinstance(node, dict):  # e.g. {"data": {"templates": [...]}}
                for kk in list_keys:
                    if isinstance(node.get(kk), list):
                        return len(node[kk])
    except Exception:
        return None
    return None


def count_tree_nodes(path: str, key: str) -> Optional[int]:
    """Count every node carrying an 'id' in a nested hierarchy (processing
    activities arrive as a tree, not a flat list)."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return None
    seen = 0

    def walk(node):
        nonlocal seen
        if isinstance(node, dict):
            if "id" in node and "name" in node:
                seen += 1
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(d.get(key) if isinstance(d, dict) else d)
    return seen or None


def error_breakdown(path: str) -> tuple[int, collections.Counter, set]:
    """(total failed rows, Counter of short reason -> count, set of failed odoo ids)."""
    if not os.path.exists(path):
        return 0, collections.Counter(), set()
    try:
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except Exception:
        return 0, collections.Counter(), set()
    counter = collections.Counter()
    ids = set()
    for r in rows:
        msg = r.get("error") or r.get("error_message") or ""
        try:  # error column is often a JSON envelope -> pull .message
            msg = json.loads(msg).get("message", msg)
        except Exception:
            pass
        counter[(str(msg).strip() or "(unspecified)")[:70]] += 1
        oid = r.get("odoo_source_id") or r.get("odoo_id")
        if oid not in (None, ""):
            try:
                ids.add(int(oid))
            except (TypeError, ValueError):
                ids.add(str(oid))
    return len(rows), counter, ids


# --------------------------------------------------------------------------- #
# Live Postgres reads (migration_source_map = source of truth for "landed")
# --------------------------------------------------------------------------- #
def _psql(sql: str) -> Optional[str]:
    try:
        out = subprocess.run(
            ["docker", "exec", PG_CONTAINER, "psql", "-U", PG_USER, "-d", PG_DB,
             "-t", "-A", "-F", ",", "-c", sql],
            capture_output=True, text=True, timeout=15,
        )
        return out.stdout if out.returncode == 0 else None
    except Exception:
        return None


def sourcemap_counts() -> dict:
    out = _psql("SELECT entity, count(*) FROM migration_source_map GROUP BY entity;")
    counts = {}
    if out:
        for line in out.strip().splitlines():
            if "," in line:
                ent, n = line.split(",", 1)
                counts[ent.strip()] = int(n)
    return counts


def sourcemap_ids() -> dict:
    """{entity: {odoo_source_id as str}} — the exact ids that landed in Flask."""
    out = _psql("SELECT entity, odoo_source_id FROM migration_source_map;")
    grouped: dict = collections.defaultdict(set)
    if out:
        for line in out.strip().splitlines():
            if "," in line:
                ent, oid = line.split(",", 1)
                grouped[ent.strip()].add(oid.strip())
    return grouped


def csv_ids(path: str, cols=("id", "odoo_source_id")) -> set:
    if not os.path.exists(path):
        return set()
    try:
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except Exception:
        return set()
    if not rows:
        return set()
    col = next((c for c in cols if c in rows[0]), None)
    if col is None:
        return set()
    return {str(r[col]).strip() for r in rows if str(r.get(col, "")).strip()}


def json_ids(path: str, list_key: str, id_field: str = "id") -> set:
    if not os.path.exists(path):
        return set()
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return set()
    node = d.get(list_key) if isinstance(d, dict) else d
    if not isinstance(node, list):
        return set()
    return {str(x[id_field]).strip() for x in node
            if isinstance(x, dict) and x.get(id_field) is not None}


def db_table_count(table: str) -> Optional[int]:
    out = _psql(f"SELECT count(*) FROM {table};")
    try:
        return int(out.strip()) if out and out.strip() else None
    except Exception:
        return None


def load_accepted_loss() -> dict:
    """{entity: [ {odoo_id, reason}, ... ]} of records intentionally not migrated."""
    if not os.path.exists(ACCEPTED_LOSS_FILE):
        return {}
    try:
        with open(ACCEPTED_LOSS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        grouped: dict = collections.defaultdict(list)
        for rec in data.get("records", []):
            grouped[rec.get("entity")].append(rec)
        return grouped
    except Exception:
        return {}


# --------------------------------------------------------------------------- #
# Entity model
# --------------------------------------------------------------------------- #
@dataclass
class EntitySpec:
    key: str
    title: str
    source_fn: Callable[[], Optional[int]]
    source_desc: str
    staged: list[tuple[str, str]]          # (label, path)
    sourcemap_key: Optional[str]           # None => migration not source-map tracked
    errors_path: Optional[str]
    db_table: Optional[str]
    raw_ids_fn: Optional[Callable[[], set]] = None  # exact source ids, for id-level diff
    notes: str = ""


@dataclass
class EntityResult:
    spec: EntitySpec
    source: Optional[int]
    staged: list[tuple[str, Optional[int]]]
    migrated: Optional[int]
    db_rows: Optional[int]
    failed: int
    fail_reasons: collections.Counter
    failed_ids: set = field(default_factory=set)
    accepted: list = field(default_factory=list)
    missing_ids: Optional[set] = None      # exact source ids that vanished (id-level diff)

    @property
    def accepted_n(self) -> int:
        return len(self.accepted)

    @property
    def _accepted_ids(self) -> set:
        out = set()
        for a in self.accepted:
            oid = a.get("odoo_id")
            try:
                out.add(int(oid))
            except (TypeError, ValueError):
                out.add(oid)
        return out

    @property
    def accepted_extra(self) -> int:
        """Accepted-loss records that are NOT already counted in `failed`."""
        return len(self._accepted_ids - self.failed_ids)

    @property
    def unaccepted_failed(self) -> int:
        """Failures the operator has NOT signed off on."""
        return max(0, self.failed - len(self._accepted_ids & self.failed_ids))

    @property
    def pct(self) -> Optional[float]:
        if not self.source or self.migrated is None:
            return None
        return round(100.0 * self.migrated / self.source, 1)

    @property
    def unexplained(self) -> Optional[int]:
        """Records that vanished without any recorded reason (the dangerous bucket).
        When an id-level diff is available it is authoritative; otherwise fall back
        to arithmetic (source - migrated - failed - accepted_extra)."""
        if self.missing_ids is not None:
            return len(self.missing_ids)
        if self.source is None or self.migrated is None:
            return None
        return self.source - self.migrated - self.failed - self.accepted_extra

    @property
    def verdict(self) -> str:
        if self.spec.sourcemap_key is None:
            return "UNTRACKED"
        if self.source is None or self.migrated is None:
            return "UNKNOWN"
        if self.migrated == self.source:
            return "PASS"
        if (self.unexplained or 0) > 0:
            return "GAP"                      # records vanished unexplained
        if self.unaccepted_failed == 0:
            return "PASS*"                    # shortfall fully accepted-loss
        if not self.fail_reasons_data_quality():
            return "RECOVERABLE"              # remaining failures are operational
        return "GAP"

    def fail_reasons_data_quality(self) -> bool:
        """True if any failure looks like a data problem rather than an
        operational/config limit (license, quota...)."""
        operational = ("license", "quota", "limit")
        for reason in self.fail_reasons:
            if not any(t in reason.lower() for t in operational):
                return True
        return False


def build_results() -> list[EntityResult]:
    sm = sourcemap_counts()
    sm_ids = sourcemap_ids()
    accepted = load_accepted_loss()

    specs = [
        EntitySpec(
            "consent", "Consent",
            lambda: count_csv_rows(f"{RAW_DIR}/raw_consents.csv"),
            "raw_consents.csv",
            [("legacy", f"{PROC_DIR}/processed_consents_legacy.csv"),
             ("paper", f"{PROC_DIR}/processed_consents_paper.csv")],
            "consent", f"{PROC_DIR}/errors_processed_consents.csv", "consents",
            raw_ids_fn=lambda: csv_ids(f"{RAW_DIR}/raw_consents.csv"),
        ),
        EntitySpec(
            "request", "Data Principal Request",
            lambda: count_csv_rows(f"{RAW_DIR}/raw_requests.csv"),
            "raw_requests.csv",
            [("processed", f"{PROC_DIR}/processed_requests.csv")],
            "request", None, "requests",
            raw_ids_fn=lambda: csv_ids(f"{RAW_DIR}/raw_requests.csv"),
        ),
        EntitySpec(
            "vendor", "Vendor",
            lambda: count_json_list(f"{RAW_DIR}/raw_vendors.json", "vendors"),
            "raw_vendors.json",
            [("processed", f"{PROC_DIR}/processed_vendors.csv")],
            "vendor", f"{PROC_DIR}/errors_processed_vendors.csv", "vendors",
            raw_ids_fn=lambda: json_ids(f"{RAW_DIR}/raw_vendors.json", "vendors"),
        ),
        EntitySpec(
            "stakeholder", "Internal Stakeholder",
            lambda: count_json_list(f"{RAW_DIR}/raw_stakeholders.json", "stakeholders"),
            "raw_stakeholders.json",
            [("processed", f"{PROC_DIR}/processed_stakeholders.csv")],
            "stakeholder", None, None,
            raw_ids_fn=lambda: json_ids(f"{RAW_DIR}/raw_stakeholders.json", "stakeholders"),
            notes="Endpoint hardcodes user_role_type=PAManager; Odoo DPO vs PA Manager not preserved.",
        ),
        EntitySpec(
            "processing_activity", "Processing Activity",
            lambda: count_tree_nodes(f"{RAW_DIR}/raw_processing_activities.json",
                                     "processingActivities"),
            "raw_processing_activities.json (tree)",
            [("processed", f"{PROC_DIR}/processed_processing_activities.csv")],
            None, None, "processing_activity",
            notes="No source-map tracking -> not idempotent / not auditable per-record.",
        ),
        EntitySpec(
            "template", "Template",
            lambda: count_json_list(f"{RAW_DIR}/raw_templates.json", "data", "templates"),
            "raw_templates.json",
            [("processed (expanded rows)", f"{PROC_DIR}/processed_templates.csv")],
            None, None, "notice_templates",
            notes="One Odoo template fans out to many notice/email rows; not source-map tracked.",
        ),
    ]

    results = []
    for s in specs:
        failed, reasons, failed_ids = (
            error_breakdown(s.errors_path) if s.errors_path
            else (0, collections.Counter(), set())
        )
        # id-level diff: source ids that are neither migrated, failed, nor accepted.
        missing_ids = None
        if s.raw_ids_fn and s.sourcemap_key:
            raw_ids = s.raw_ids_fn()
            if raw_ids:
                landed = sm_ids.get(s.sourcemap_key, set())
                failed_str = {str(x) for x in failed_ids}
                accepted_str = {str(a.get("odoo_id")) for a in accepted.get(s.key, [])}
                missing_ids = raw_ids - landed - failed_str - accepted_str
        results.append(EntityResult(
            spec=s,
            source=s.source_fn(),
            staged=[(lbl, count_csv_rows(p)) for lbl, p in s.staged],
            migrated=sm.get(s.sourcemap_key) if s.sourcemap_key else None,
            db_rows=db_table_count(s.db_table) if s.db_table else None,
            failed=failed,
            fail_reasons=reasons,
            failed_ids=failed_ids,
            accepted=accepted.get(s.key, []),
            missing_ids=missing_ids,
        ))
    return results


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _bar(pct: Optional[float]) -> str:
    if pct is None:
        return "[" + "?" * BAR_WIDTH + "]"
    filled = int(round(BAR_WIDTH * pct / 100.0))
    return "[" + "#" * filled + "-" * (BAR_WIDTH - filled) + "]"


def _fmt(n: Optional[int]) -> str:
    return "  n/a" if n is None else f"{n:>5}"


VERDICT_GLYPH = {
    "PASS": "+", "PASS*": "+", "RECOVERABLE": "~", "GAP": "x",
    "UNTRACKED": "?", "UNKNOWN": "?",
}


def render(results: list[EntityResult]) -> str:
    L: list[str] = []
    line = "=" * 78
    L.append(line)
    L.append("  ODOO -> FLASK MIGRATION : RECONCILIATION AUDIT".ljust(78))
    L.append(f"  generated {datetime.now():%Y-%m-%d %H:%M:%S}  |  source-map = live Postgres truth")
    L.append(line)
    L.append("")

    # ---- summary table ----
    L.append("SUMMARY  (migrated / source)")
    L.append("-" * 78)
    L.append(f"  {'entity':22} {'src':>5} {'migr':>5} {'fail':>5} {'acc':>4} {'unexp':>6} {'%':>7}  verdict")
    tracked_src = tracked_mig = 0
    for r in results:
        unexp = r.unexplained
        pct = r.pct
        L.append(
            f"  {r.spec.title[:22]:22} {_fmt(r.source)} {_fmt(r.migrated)}"
            f" {r.failed:>5} {r.accepted_n:>4}"
            f" {('   n/a' if unexp is None else f'{unexp:>6}')}"
            f" {('    n/a' if pct is None else f'{pct:>6.1f}%')}"
            f"  {VERDICT_GLYPH.get(r.verdict, ' ')} {r.verdict}"
        )
        if r.spec.sourcemap_key and r.source and r.migrated is not None:
            tracked_src += r.source
            tracked_mig += r.migrated
    L.append("-" * 78)
    overall = round(100.0 * tracked_mig / tracked_src, 1) if tracked_src else 0.0
    L.append(f"  TRACKED ENTITIES OVERALL: {tracked_mig}/{tracked_src} = {overall}%   {_bar(overall)}")
    L.append("")
    L.append("  legend: src=Odoo source  migr=landed in Flask  fail=rejected  "
             "acc=accepted-loss  unexp=unexplained")
    L.append("          OK pass | OK* covered by accepted-loss | FIX recoverable "
             "(operational) | GAP investigate | ?? not tracked")
    L.append("")

    # ---- per-entity detail ----
    L.append("=" * 78)
    L.append("PER-ENTITY DETAIL")
    L.append("=" * 78)
    for r in results:
        L.append("")
        L.append(f"### {r.spec.title}  [{r.verdict}]   {_bar(r.pct)} "
                 f"{'' if r.pct is None else str(r.pct) + '%'}")
        L.append(f"    source        : {_fmt(r.source).strip()}  ({r.spec.source_desc})")
        for lbl, n in r.staged:
            L.append(f"    staged[{lbl}]  : {_fmt(n).strip()}")
        if r.spec.sourcemap_key:
            L.append(f"    migrated      : {_fmt(r.migrated).strip()}  "
                     f"(migration_source_map['{r.spec.sourcemap_key}'])")
        else:
            L.append(f"    migrated      : not source-map tracked")
        if r.db_rows is not None:
            L.append(f"    flask db rows : {r.db_rows}  ({r.spec.db_table})")
        if r.failed:
            L.append(f"    failed        : {r.failed}")
            for reason, n in r.fail_reasons.most_common():
                L.append(f"        - {n:>4} x  {reason}")
        if r.accepted:
            L.append(f"    accepted loss : {r.accepted_n}")
            for a in r.accepted:
                L.append(f"        - odoo#{a.get('odoo_id')}  {a.get('reason')}")
        if r.unexplained and r.unexplained > 0:
            L.append(f"    !! UNEXPLAINED: {r.unexplained}  (in source, but neither migrated, "
                     f"failed, nor accepted) -> INVESTIGATE")
            if r.missing_ids:
                shown = sorted(r.missing_ids, key=lambda x: (len(x), x))[:40]
                L.append(f"        missing odoo ids: {', '.join(shown)}"
                         + (" ..." if len(r.missing_ids) > 40 else ""))
        if r.spec.notes:
            L.append(f"    note          : {r.spec.notes}")

    # ---- remaining requirements ----
    L.append("")
    L.append("=" * 78)
    L.append("REMAINING REQUIREMENTS  (ranked)")
    L.append("=" * 78)
    for i, item in enumerate(derive_requirements(results), 1):
        L.append(f"  {i}. [{item['sev']}] {item['title']}")
        L.append(f"        {item['detail']}")

    # ---- methodology ----
    L.append("")
    L.append("=" * 78)
    L.append("METHODOLOGY / LIMITATIONS")
    L.append("=" * 78)
    L.append("  - 'migrated' = rows in live migration_source_map (idempotency ledger),")
    L.append("    the only authoritative 'landed in Flask' signal. consent/request/")
    L.append("    vendor/stakeholder are tracked; processing_activity + template are NOT,")
    L.append("    so their completion cannot be proven per-record yet.")
    L.append("  - 'failed' counts come from data/processed/errors_*.csv (latest load run).")
    L.append("  - 'accepted loss' is the operator-maintained data/accepted_loss.json.")
    L.append("  - Any read failure renders as n/a rather than aborting the audit.")
    L.append("")
    return "\n".join(L)


def derive_requirements(results: list[EntityResult]) -> list[dict]:
    """Turn the numbers into a ranked action list (the 'what's left' section)."""
    reqs: list[dict] = []
    by_key = {r.spec.key: r for r in results}

    c = by_key.get("consent")
    if c and c.failed:
        lic = sum(n for reason, n in c.fail_reasons.items() if "license" in reason.lower())
        if lic:
            reqs.append({
                "sev": "HIGH", "title": f"Recover {lic} license-blocked consents",
                "detail": "Failures are 'No active license available' (operational, not data). "
                          "Add license capacity then re-run `consent load`; they should land.",
            })

    for r in results:
        if r.unexplained and r.unexplained > 0:
            ids = ""
            if r.missing_ids:
                ids = " odoo ids: " + ", ".join(
                    sorted(r.missing_ids, key=lambda x: (len(x), x))[:40])
            reqs.append({
                "sev": "HIGH", "title": f"Investigate {r.unexplained} dropped {r.spec.title} record(s)",
                "detail": f"In source but neither migrated, failed, nor accepted -> silently "
                          f"dropped at load.{ids}",
            })

    for r in results:
        if r.spec.sourcemap_key is None and r.spec.key in ("processing_activity", "template"):
            reqs.append({
                "sev": "MED", "title": f"Add source-map tracking for {r.spec.title}",
                "detail": "Not recorded in migration_source_map -> re-runs are not idempotent and "
                          "completion cannot be audited per-record. Mirror the vendor/stakeholder pattern.",
            })

    s = by_key.get("stakeholder")
    if s:
        reqs.append({
            "sev": "MED", "title": "Decide DPO vs PA Manager role fidelity",
            "detail": "All stakeholders land as user_role_type=PAManager; Odoo DPO distinction is lost. "
                      "If DPO must persist, backfill via /stakeholder/<id>/update-roles.",
        })

    reqs.append({
        "sev": "LOW", "title": "Rotate shared secrets",
        "detail": "Odoo JWT + Flask API key were exposed during debugging; rotate and confirm "
                  "config/.env stays gitignored.",
    })
    return reqs


# --------------------------------------------------------------------------- #
# Public entry + self-test
# --------------------------------------------------------------------------- #
def run_reconciliation(write: bool = True) -> str:
    results = build_results()
    report = render(results)
    if write:
        os.makedirs(PROC_DIR, exist_ok=True)
        with open(REPORT_PATH, "w", encoding="utf-8") as f:
            f.write(report)
    return report


def self_test() -> list[str]:
    """Internal consistency checks. Returns list of failure strings (empty = ok)."""
    fails: list[str] = []
    results = build_results()
    if not results:
        return ["build_results() returned nothing"]
    for r in results:
        if r.source is not None and r.migrated is not None:
            if r.migrated > r.source:
                fails.append(f"{r.spec.key}: migrated {r.migrated} > source {r.source}")
            if r.pct is not None and not (0 <= r.pct <= 100):
                fails.append(f"{r.spec.key}: pct {r.pct} out of range")
    # ledger identity must hold where computable
    for r in results:
        if r.unexplained is not None:
            lhs = r.source
            rhs = r.migrated + r.failed + r.accepted_extra + r.unexplained
            if lhs != rhs:
                fails.append(f"{r.spec.key}: ledger identity broken {lhs} != {rhs}")
    # render must not raise and must be non-trivial
    rep = render(results)
    if len(rep) < 200 or "RECONCILIATION AUDIT" not in rep:
        fails.append("render() produced a trivial/invalid report")
    return fails


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv:
        problems = self_test()
        print("SELF-TEST:", "PASS" if not problems else "FAIL")
        for p in problems:
            print("  -", p)
        sys.exit(1 if problems else 0)
    print(run_reconciliation())
    print(f"\n[written] {REPORT_PATH}")
