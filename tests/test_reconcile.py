"""Unit tests for the migration reconciliation audit.

These are DB-free: they exercise the counters, the ledger accounting (verdict /
unexplained / accepted-overlap) and the renderer against synthetic fixtures, so
they run in CI without Postgres or docker.
"""

import collections

import pytest

from scripts.report import reconcile as R


# --------------------------------------------------------------------------- #
# counters
# --------------------------------------------------------------------------- #
def test_count_csv_rows(tmp_path):
    p = tmp_path / "x.csv"
    p.write_text("id,name\n1,a\n2,b\n3,c\n", encoding="utf-8")
    assert R.count_csv_rows(str(p)) == 3
    assert R.count_csv_rows(str(tmp_path / "missing.csv")) is None


def test_count_json_list_and_nested(tmp_path):
    flat = tmp_path / "v.json"
    flat.write_text('{"vendors":[{"id":1},{"id":2}]}', encoding="utf-8")
    assert R.count_json_list(str(flat), "vendors") == 2

    nested = tmp_path / "t.json"
    nested.write_text('{"data":{"templates":[{"id":1},{"id":2},{"id":3}]}}', encoding="utf-8")
    assert R.count_json_list(str(nested), "data", "templates") == 3


def test_count_tree_nodes(tmp_path):
    p = tmp_path / "pa.json"
    p.write_text(
        '{"processingActivities":[{"id":1,"name":"root","children":'
        '[{"id":2,"name":"a"},{"id":3,"name":"b"}]}]}',
        encoding="utf-8",
    )
    assert R.count_tree_nodes(str(p), "processingActivities") == 3


def test_error_breakdown_groups_and_ids(tmp_path):
    p = tmp_path / "err.csv"
    p.write_text(
        "odoo_source_id,error\n"
        '10,"{""message"":""No active license available""}"\n'
        '11,"{""message"":""No active license available""}"\n'
        '12,"plain text error"\n',
        encoding="utf-8",
    )
    total, reasons, ids = R.error_breakdown(str(p))
    assert total == 3
    assert reasons["No active license available"] == 2
    assert ids == {10, 11, 12}


def test_csv_and_json_ids(tmp_path):
    c = tmp_path / "c.csv"
    c.write_text("id,name\n23,x\n28,y\n", encoding="utf-8")
    assert R.csv_ids(str(c)) == {"23", "28"}

    j = tmp_path / "j.json"
    j.write_text('{"stakeholders":[{"id":6},{"id":38}]}', encoding="utf-8")
    assert R.json_ids(str(j), "stakeholders") == {"6", "38"}


# --------------------------------------------------------------------------- #
# accounting / verdict logic
# --------------------------------------------------------------------------- #
def _spec(key="consent", sourcemap_key="consent"):
    return R.EntitySpec(
        key=key, title=key.title(), source_fn=lambda: None, source_desc="x",
        staged=[], sourcemap_key=sourcemap_key, errors_path=None, db_table=None,
    )


def _result(**kw):
    base = dict(
        spec=_spec(), source=None, staged=[], migrated=None, db_rows=None,
        failed=0, fail_reasons=collections.Counter(), failed_ids=set(),
        accepted=[], missing_ids=None,
    )
    base.update(kw)
    return R.EntityResult(**base)


def test_verdict_pass_full():
    r = _result(source=8, migrated=8)
    assert r.verdict == "PASS"
    assert r.pct == 100.0
    assert r.unexplained == 0


def test_verdict_recoverable_operational_failures():
    # all shortfall is operational (license) failures, none accepted, none missing
    r = _result(
        source=10, migrated=7, failed=3,
        fail_reasons=collections.Counter({"No active license available": 3}),
        failed_ids={1, 2, 3}, missing_ids=set(),
    )
    assert r.unaccepted_failed == 3
    assert r.verdict == "RECOVERABLE"


def test_verdict_gap_on_unexplained():
    r = _result(source=460, migrated=331, failed=123,
                failed_ids=set(range(1000, 1123)),
                missing_ids={"23", "28", "382", "383", "384", "385"})
    assert r.unexplained == 6
    assert r.verdict == "GAP"


def test_accepted_overlapping_failure_counts_once():
    # id 4 is both in the errors file AND the accepted-loss registry -> count once
    r = _result(
        source=12, migrated=11, failed=1,
        fail_reasons=collections.Counter({"already exists as DataPrincipal": 1}),
        failed_ids={4}, accepted=[{"odoo_id": 4, "reason": "test"}],
        missing_ids=set(),
    )
    assert r.accepted_extra == 0          # not double counted
    assert r.unaccepted_failed == 0       # the one failure is signed off
    assert r.unexplained == 0
    assert r.verdict == "PASS*"


def test_untracked_entity():
    r = _result(spec=_spec(key="template", sourcemap_key=None), source=28, migrated=None)
    assert r.verdict == "UNTRACKED"
    assert r.pct is None


# --------------------------------------------------------------------------- #
# renderer
# --------------------------------------------------------------------------- #
def test_render_is_nontrivial_and_safe():
    results = [
        _result(source=8, migrated=8),
        _result(spec=_spec("vendor", "vendor"), source=12, migrated=11, failed=1,
                fail_reasons=collections.Counter({"dup": 1}), failed_ids={4},
                accepted=[{"odoo_id": 4, "reason": "test"}], missing_ids=set()),
    ]
    out = R.render(results)
    assert "RECONCILIATION AUDIT" in out
    assert "REMAINING REQUIREMENTS" in out
    assert len(out) > 500


def test_self_test_runs():
    # may PASS or report problems depending on live data, but must never raise
    problems = R.self_test()
    assert isinstance(problems, list)


# --------------------------------------------------------------------------- #
# live-API mode (opt-in): envelope parsing, drift verdict, drift rendering
# --------------------------------------------------------------------------- #
def test_find_list_of_dicts_envelope_shapes():
    f = R._find_list_of_dicts
    assert f([{"id": 1}], ()) == [{"id": 1}]                       # bare list
    assert f({"vendors": [{"id": 1}]}, ("vendors",)) == [{"id": 1}]  # preferred key
    assert f({"data": {"items": [{"id": 9}]}}, ("items",)) == [{"id": 9}]  # nested
    assert f({"meta": 1, "rows": [{"id": 3}]}, ()) == [{"id": 3}]  # first list-of-dicts
    assert f({"nope": 5}, ()) == []                                # nothing usable


def test_count_tree_live_helper():
    payload = {"processingActivities": [
        {"id": 1, "name": "r", "children": [{"id": 2, "name": "a"}]}]}
    assert R._count_tree(payload, "processingActivities") == 2


def test_drift_verdict_and_count():
    # ledger claims 8 migrated; live app is missing flask ids 5 and 8 -> DRIFT
    r = _result(source=8, migrated=8, drift_ids={"5", "8"},
                live_dest_ids={"1", "2", "3", "4", "6", "7"})
    assert r.drift == 2
    assert r.verdict == "DRIFT"          # drift overrides an otherwise-PASS


def test_no_drift_keeps_pass():
    r = _result(source=8, migrated=8, drift_ids=set(), live_dest_ids=set(range(8)))
    assert r.drift == 0
    assert r.verdict == "PASS"


def test_find_pagination_meta_nested():
    payload = {"data": {"records": [{"id": 1}],
                        "pagination": {"page": 1, "totalPages": 3, "hasNext": True}}}
    meta = R._find_pagination_meta(payload)
    assert meta["totalPages"] == 3 and meta["hasNext"] is True
    assert R._find_pagination_meta({"x": 1}) == {}


def test_live_dest_records_walks_all_pages(monkeypatch):
    # server caps per_page at 100 and exposes nested camelCase meta -> reader must
    # follow every page, not stop at page 1 (the bug that faked 231 consent DRIFT)
    pages = {
        1: {"data": {"records": [{"id": i} for i in range(1, 101)],
                     "pagination": {"page": 1, "totalPages": 3, "hasNext": True}}},
        2: {"data": {"records": [{"id": i} for i in range(101, 201)],
                     "pagination": {"page": 2, "totalPages": 3, "hasNext": True}}},
        3: {"data": {"records": [{"id": i} for i in range(201, 251)],
                     "pagination": {"page": 3, "totalPages": 3, "hasNext": False}}},
    }

    def fake_get(url, token, host=None):
        import re
        page = int(re.search(r"[?&]page=(\d+)", url).group(1))
        return pages[page]

    monkeypatch.setattr(R, "FLASK_API_BASE_URL", "http://x")
    monkeypatch.setattr(R, "FLASK_API_KEY", "k")
    monkeypatch.setattr(R, "_http_get_json", fake_get)
    ids = R.live_dest_ids("consent")
    assert len(ids) == 250                      # all three pages, not 100
    assert "250" in ids and "1" in ids


def test_render_surfaces_drift(monkeypatch):
    monkeypatch.setattr(R, "LIVE", True)
    r = _result(source=8, migrated=8, drift_ids={"5", "8"},
                live_dest_ids={"1", "2"})
    out = R.render([r])
    assert "DRIFT" in out
    assert "missing flask ids: 5, 8" in out


# --------------------------------------------------------------------------- #
# fan-out ledger parsing (template 1 -> N) + distinct counts
# --------------------------------------------------------------------------- #
def test_sourcemap_pairs_collects_fanout_sets(monkeypatch):
    # one template (odoo 7) maps to three flask rows -> value must be a set
    monkeypatch.setattr(R, "_psql", lambda sql: (
        "template,7,100\ntemplate,7,101\ntemplate,7,102\nvendor,4,55\n"))
    pairs = R.sourcemap_pairs()
    assert pairs["template"]["7"] == {"100", "101", "102"}
    assert pairs["vendor"]["4"] == {"55"}


def test_sourcemap_counts_uses_distinct(monkeypatch):
    # the SQL must count DISTINCT odoo_source_id (template fan-out safe)
    seen = {}

    def fake_psql(sql):
        seen["sql"] = sql
        return "template,28\nconsent,460\n"
    monkeypatch.setattr(R, "_psql", fake_psql)
    counts = R.sourcemap_counts()
    assert "DISTINCT odoo_source_id" in seen["sql"]
    assert counts == {"template": 28, "consent": 460}


# --------------------------------------------------------------------------- #
# field-level value-equality diff
# --------------------------------------------------------------------------- #
def test_normalizers():
    assert R._n_token("Deemed consent") == R._n_token("Deemed Consent")
    assert R._n_token("legacy") == R._n_token("Legacy")
    assert R._n_digits("+91 (98) 765-43210") == "919876543210"
    proc = R._alias(R._CONSENT_PROC_TYPE)
    assert proc("mandatory") == R._n_token("Mandatory/Regulatory")


def test_compare_record_skips_missing_source_flags_real_diff():
    # source name present but differs -> mismatch; email absent on source -> skipped
    src = {"name": [9, "Asha"], "status": "Consented"}
    dst = {"name": "Asha Kumar", "email": "x@y.com", "status": "Consented"}
    diffs, cov = R.compare_record("consent", src, dst)
    labels = [d[0] for d in diffs]
    assert "name" in labels            # 'asha' != 'ashakumar'
    assert "email" not in labels       # source had no eMail -> not comparable
    assert "status" not in labels      # equal after normalization
    assert "name" in cov["compared_src"]


def test_compare_record_autopairs_unmapped_fields_and_reports_coverage():
    # 'created_at' isn't in FIELD_MAPS but exists on both sides with same name ->
    # auto-paired & value-checked; 'history' is a list -> complex, not checked.
    src = {"name": [1, "X"], "created_at": "2024-01-01", "history": [{"e": 1}],
           "odoo_only_field": "z"}
    dst = {"name": "X", "created_at": "2024-01-02"}     # date differs
    diffs, cov = R.compare_record("consent", src, dst)
    labels = [d[0] for d in diffs]
    assert "created_at" in labels                       # auto-paired mismatch
    assert "created_at" in cov["compared_src"]
    assert "history" in cov["complex_src"]              # list -> surfaced, not checked
    assert "odoo_only_field" in cov["src_keys"]
    assert "odoo_only_field" not in cov["compared_src"]  # no dest match -> unchecked


def test_compare_record_list_length_diff():
    # audit-log / attachment arrays: length-compared (not deep), surfaced on diff
    src = {"name": [1, "X"], "history": [{"e": 1}, {"e": 2}, {"e": 3}]}
    dst = {"name": "X", "history": [{"e": 1}]}            # 3 vs 1
    diffs, cov = R.compare_record("consent", src, dst)
    labels = [d[0] for d in diffs]
    assert "history[len]" in labels
    assert "history" in cov["compared_src"]              # paired -> not "unchecked"


def test_run_field_diff_joins_and_counts(monkeypatch):
    monkeypatch.setattr(R, "ODOO_JWT_TOKEN", "t")
    monkeypatch.setattr(R, "FLASK_API_BASE_URL", "http://x")
    monkeypatch.setattr(R, "FLASK_API_KEY", "k")
    R._LIVE_CACHE.clear()
    # source: odoo 1 (match) and odoo 2 (status differs)
    monkeypatch.setattr(R, "live_source_records", lambda e: {
        "1": {"name": [1, "Asha"], "status": "Consented"},
        "2": {"name": [2, "Ben"], "status": "Rejected"},
    })
    monkeypatch.setattr(R, "live_dest_records", lambda e: [
        {"id": 100, "name": "Asha", "status": "Consented"},
        {"id": 200, "name": "Ben", "status": "Withdrawn"},   # status mismatch
    ])
    sm_pairs = {"consent": {"1": {"100"}, "2": {"200"}}}
    fd = R.run_field_diff("consent", sm_pairs)
    assert fd["compared"] == 2
    assert fd["records_mismatched"] == 1
    assert fd["field_counts"]["status"] == 1


def test_run_field_diff_none_without_live(monkeypatch):
    monkeypatch.setattr(R, "ODOO_JWT_TOKEN", None)
    R._LIVE_CACHE.clear()
    assert R.run_field_diff("consent", {"consent": {"1": {"100"}}}) is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
