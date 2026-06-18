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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
