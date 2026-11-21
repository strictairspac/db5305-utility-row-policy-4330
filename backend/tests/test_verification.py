"""Regression tests for dispatch-authorisation reconciliation.

The RLS tests need a PostgreSQL 16 server. Point ``GB5305_TEST_DATABASE_URL``
at a writable database whose login can create schemas, roles and policies
(otherwise the module is skipped). The evidence tests only need SQLite.
"""

import copy
import os
import sys
from datetime import timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.services import domain, workflow  # noqa: E402
from src.services.domain import SPEC, solve  # noqa: E402

TEST_URL = os.getenv("GB5305_TEST_DATABASE_URL")


def _payload(**changes):
    payload = copy.deepcopy(SPEC["sample"])
    payload.update(changes)
    return payload


def _person(result, operator_id):
    return next(row for row in result["sections"][0]["rows"] if row["人员编号"] == operator_id)


def _delegation(operator_id, zone, utility, start, end):
    return {"operator_id": operator_id, "zone": zone, "utility": utility,
            "valid_from": start, "valid_until": end}


# ---------------------------------------------------------------- validation

def test_sample_metrics_match_spec():
    assert SPEC["expect"] == {
        "验签人员数": 4, "核对组合数": 20, "业务授权数": 4, "数据库可见数": 4,
        "越权可见数": 1, "应见未见数": 1, "差异数": 2,
    }


@pytest.mark.parametrize("payload", [
    _payload(audit_at="2026-11-21T09:00:00"),
    _payload(operators=[]),
    _payload(delegations=[_delegation("BD-017", "北闸", "水表",
                                      "2026-11-21T09:00:00+08:00", "2026-11-21T09:00:00+08:00")]),
    _payload(delegations=[_delegation("BD-017", "北闸", "水表",
                                      "2026-11-21T12:00:00+08:00", "2026-11-21T11:00:00+08:00")]),
    _payload(delegations=[_delegation("MISSING", "北闸", "水表",
                                      "2026-11-21T08:00:00+08:00", "2026-11-21T12:00:00+08:00")]),
    _payload(work_orders=[dict(SPEC["sample"]["work_orders"][0], utility="气表")]),
    _payload(business_grants=[{"operator_id": "BD-017", "order_id": "UNKNOWN"}]),
])
def test_ambiguous_or_invalid_input_is_rejected(payload):
    with pytest.raises(ValueError):
        domain.validate(payload)


def test_duplicate_primary_keys_are_rejected():
    duplicated_people = _payload()
    duplicated_people["operators"].append(copy.deepcopy(duplicated_people["operators"][0]))
    with pytest.raises(ValueError):
        domain.validate(duplicated_people)

    duplicated_orders = _payload()
    duplicated_orders["work_orders"].append(copy.deepcopy(duplicated_orders["work_orders"][0]))
    with pytest.raises(ValueError):
        domain.validate(duplicated_orders)


# ---------------------------------------------------------------- evidence

@pytest.fixture()
def memory_engine():
    from sqlalchemy import create_engine
    from src.models.tables import schema

    engine = create_engine("sqlite://")
    schema.create_all(engine)
    original = workflow.engine
    workflow.engine = engine
    try:
        yield engine
    finally:
        workflow.engine = original


def _make_running(engine, title):
    from sqlalchemy import select
    from src.models.tables import runs

    case = workflow.make_case(title, SPEC["sample"], "operator")
    run = workflow.enqueue(case["id"], "operator")
    run_id, token, _ = workflow.claim()
    assert run_id == run["id"]
    return case, run_id, token


def test_failed_attempt_withholds_a_difference_conclusion(memory_engine):
    from sqlalchemy import func, select
    from src.models.tables import evidence, runs

    case, run_id, token = _make_running(memory_engine, "失败批次")
    workflow.finish(run_id, token, result={"summary": "不应写入"}, error="受限角色未生效")
    with memory_engine.connect() as conn:
        row = conn.execute(select(runs.c.status, runs.c.error)).one()
        count = conn.execute(select(func.count()).select_from(evidence)).scalar_one()
    assert row.status == "failed"
    assert row.error == "受限角色未生效"
    assert count == 0


def test_retry_after_failure_keeps_a_single_evidence(memory_engine):
    from sqlalchemy import func, select
    from src.models.tables import evidence

    case, failed_run, failed_token = _make_running(memory_engine, "重试批次")
    workflow.finish(failed_run, failed_token, error="RLS 未生效")
    _, retry_run, retry_token = _make_running(memory_engine, "重试批次")
    workflow.finish(retry_run, retry_token, result={"summary": "通过", "metrics": {}, "sections": []})
    with memory_engine.connect() as conn:
        rows = conn.execute(select(evidence.c.run_id)).all()
    assert rows == [(retry_run,)]


# ---------------------------------------------------------------- RLS replay

needs_db = pytest.mark.skipif(not TEST_URL, reason="GB5305_TEST_DATABASE_URL 未配置")


@pytest.fixture(scope="module")
def engine():
    if not TEST_URL:
        pytest.skip("GB5305_TEST_DATABASE_URL 未配置")
    from sqlalchemy import create_engine
    return create_engine(TEST_URL)


@needs_db
def test_sample_reconciles_excess_and_missing(engine):
    result = solve(SPEC["sample"], engine)
    assert result["metrics"] == SPEC["expect"]
    diffs = {(row["人员编号"], row["工单编号"], row["判定"]) for row in result["sections"][1]["rows"]}
    assert diffs == {("BD-017", "MR-703", "越权可见"), ("BD-026", "MR-703", "应见未见")}


@needs_db
def test_overlapping_delegations_do_not_widen_visibility(engine):
    payload = _payload(audit_at="2026-11-21T09:30:00+08:00", business_grants=[], work_orders=[
        {"order_id": "NW", "meter_id": "m1", "zone": "北闸", "utility": "水表"},
        {"order_id": "SW", "meter_id": "m2", "zone": "南堤", "utility": "水表"},
    ], operators=[{"operator_id": "A", "operator_name": "甲"},
                  {"operator_id": "B", "operator_name": "乙"}], delegations=[
        _delegation("A", "北闸", "水表", "2026-11-21T08:00:00+08:00", "2026-11-21T12:00:00+08:00"),
        _delegation("A", "南堤", "水表", "2026-11-21T09:00:00+08:00", "2026-11-21T11:00:00+08:00"),
        _delegation("B", "南堤", "电表", "2026-11-21T08:00:00+08:00", "2026-11-21T12:00:00+08:00"),
    ])
    result = solve(payload, engine)
    # A's two windows overlap at 09:30; B's 电表 delegation must not leak into A's set.
    assert _person(result, "A")["数据库可见工单"] == "NW、SW"
    assert _person(result, "B")["数据库可见工单"] == ""


@needs_db
def test_orders_without_business_grant_are_reported_not_dropped(engine):
    payload = _payload(business_grants=[])
    result = solve(payload, engine)
    matrix = result["sections"][2]["rows"]
    assert len(matrix) == result["metrics"]["核对组合数"] == 20
    assert result["metrics"]["越权可见数"] == result["metrics"]["数据库可见数"] == 4
    assert result["metrics"]["应见未见数"] == 0


@needs_db
def test_half_open_boundaries_and_timezone_equivalence(engine):
    def visible(audit_at, start, end):
        order = [{"order_id": "O1", "meter_id": "m1", "zone": "北闸", "utility": "水表"}]
        payload = {"audit_at": audit_at, "operators": [{"operator_id": "A", "operator_name": "甲"}],
                   "work_orders": order, "business_grants": [],
                   "delegations": [_delegation("A", "北闸", "水表", start, end)]}
        return _person(solve(payload, engine), "A")["数据库可见工单"]

    assert visible("2026-11-21T08:00:00+08:00",
                   "2026-11-21T08:00:00+08:00", "2026-11-21T12:00:00+08:00") == "O1"
    assert visible("2026-11-21T01:00:00+00:00",
                   "2026-11-21T08:00:00+08:00", "2026-11-21T12:00:00+08:00") == "O1"
    assert visible("2026-11-21T12:00:00+08:00",
                   "2026-11-21T08:00:00+08:00", "2026-11-21T12:00:00+08:00") == ""


@needs_db
def test_empty_batch_returns_empty_visibility(engine):
    result = solve(_payload(work_orders=[], business_grants=[]), engine)
    assert result["metrics"]["核对组合数"] == 0
    assert result["metrics"]["数据库可见数"] == 0
    assert result["summary"] == "本批没有抄表工单，已用受限角色验证空可见集。"


@needs_db
def test_rls_failure_aborts_and_leaves_no_objects(engine, monkeypatch):
    from sqlalchemy import text
    from src.services import row_security

    original = row_security._prepare_audit

    def disable_rls(conn, schema, roles, *args, **kwargs):
        original(conn, schema, roles, *args, **kwargs)
        conn.exec_driver_sql(f"ALTER ROLE {list(roles.values())[0]} BYPASSRLS")

    monkeypatch.setattr(row_security, "_prepare_audit", disable_rls)
    with pytest.raises(row_security.VerificationError):
        solve(SPEC["sample"], engine)
    with engine.connect() as conn:
        roles = conn.execute(text(
            "SELECT count(*) FROM pg_catalog.pg_roles WHERE rolname LIKE 'gb5305_%'")).scalar_one()
        schemas = conn.execute(text(
            "SELECT count(*) FROM pg_catalog.pg_namespace WHERE nspname LIKE 'gb5305_%'")).scalar_one()
    assert (roles, schemas) == (0, 0)
