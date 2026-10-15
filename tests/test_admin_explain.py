"""GET /admin/explain/{endpoint}.

These tests run on mongomock with a recording stand-in for `db.command` (mongomock has no explain). They prove the WIRING: which
command is sent, that it is built by the endpoint's own builders, that it is never executed, and how its output is serialized.
They cannot prove what a real server's planner chooses: that is checked by tests/test_scale.py on a real MongoDB (TEST_MONGO_URI).
"""
import copy
import json
from datetime import date, datetime, timezone

import pytest
import yaml
from bson import Decimal128, ObjectId
from bson.int64 import Int64
from bson.son import SON
from fastapi.testclient import TestClient
from pymongo.errors import AutoReconnect, OperationFailure, PyMongoError

import app.main as m
from tests.analytics_helpers import employee
from tests.explain_checks import EXPLAIN_CASES, assert_index_backed
from tests.test_contract import SPEC_PATH

UTC = timezone.utc
SECRET_HOST = "mongo-internal-7.example.net"
# Deliberately fake values, kept as separate pieces and joined at runtime so no credential-shaped string sits in the source.
FAKE_USER, FAKE_PASSWORD, FAKE_DOMAIN, FAKE_DB = "fake" + "_user", "not" + "-a-real-" + "pw", "db.internal" + ".example", "private" + "_db_name"


def canned_explain():
    """A realistic explain document, including every BSON type that could break JSON serialization."""
    return {
        "queryPlanner": {"namespace": "attendance_test.attendance_logs", "indexFilterSet": False,
                         "winningPlan": {"stage": "LIMIT", "inputStage": {"stage": "FETCH", "inputStage": {
                             "stage": "IXSCAN", "indexName": "date_desc_emp_code", "keyPattern": {"date": -1, "emp_code": 1}}}},
                         "rejectedPlans": []},
        "executionStats": {"executionSuccess": True, "nReturned": Int64(20), "totalKeysExamined": Int64(20), "totalDocsExamined": 20,
                           "when": datetime(2026, 7, 6, 4, 35, tzinfo=UTC), "oid": ObjectId("64b7f3c2a1b2c3d4e5f60718"),
                           "dec": Decimal128("1.5"), "weird": float("nan"), "big": float("inf")},
        "serverInfo": {"host": SECRET_HOST, "port": 27017, "version": "7.0.5", "gitVersion": "abc123"},
        "ok": 1.0,
    }


class SpyCollection:
    def __init__(self, coll, name, calls):
        self._coll, self._name, self._calls = coll, name, calls

    def __getattr__(self, attr):
        target = getattr(self._coll, attr)
        if not callable(target):
            return target

        def recorded(*args, **kwargs):
            self._calls.append((self._name, attr))
            return target(*args, **kwargs)

        return recorded


class SpyDb:
    """The real (mongomock) database for ordinary access; `command` is recorded and answered with a canned explain document."""

    def __init__(self, inner, result=None, error=None):
        self._inner, self.result, self.error, self.commands, self.calls = inner, result if result is not None else canned_explain(), error, [], []

    def command(self, command, *args, **kwargs):
        self.commands.append(command)
        if self.error is not None:
            raise self.error
        return copy.deepcopy(self.result)

    def __getitem__(self, name):
        return SpyCollection(self._inner[name], name, self.calls)

    def __getattr__(self, name):
        if name in ("name", "client", "list_collection_names"):
            return getattr(self._inner, name)
        return self[name]


@pytest.fixture
def spy(db, monkeypatch):
    database = SpyDb(db)
    monkeypatch.setattr(m, "db", database)
    return database


@pytest.fixture
def client(spy):
    with TestClient(m.app) as c:
        spy.calls.clear()  # the startup index creation is not part of what the tests look at
        yield c


def explain(client, endpoint, **params):
    return client.get(f"/admin/explain/{endpoint}", params=params)


# ============================================================ which command is sent, and is it the endpoint's own query
def seed_department(spy_db):
    employee(spy_db, "A", "2026-01-05", "Eng")
    employee(spy_db, "B", "2026-07-08", "Eng")


CASES = {
    "list: every parameter, page 3 of 50": ("attendance_list", dict(emp_code="E1", date_from="2026-07-01", date_to="2026-07-31", status="WFH", page=3, page_size=50),
        "attendance_logs", lambda: {"find": "attendance_logs", "filter": m.attendance_filter("E1", "2026-07-01", "2026-07-31", "WFH"),
                                    "sort": SON(m.ATTENDANCE_SORT), "skip": 100, "limit": 50}),
    "list: defaults": ("attendance_list", {}, "attendance_logs",
        lambda: {"find": "attendance_logs", "filter": {}, "sort": SON(m.ATTENDANCE_SORT), "skip": 0, "limit": 20}),
    "monthly": ("employee_monthly", dict(emp_code="E1", month="2026-07"), "attendance_logs",
        lambda: m.aggregate_command("attendance_logs", m.employee_monthly_pipeline("E1", "2026-07"))),
    "summary, all departments": ("department_summary", dict(month="2026-07"), "employees",
        lambda: m.aggregate_command("employees", m.department_summary_pipeline("2026-07", None))),
    "summary, one department": ("department_summary", dict(month="2026-07", department="Eng"), "employees",
        lambda: m.aggregate_command("employees", m.department_summary_pipeline("2026-07", "Eng"))),
    "leaderboard, defaults": ("late_leaderboard", dict(month="2026-07"), "attendance_logs",
        lambda: m.aggregate_command("attendance_logs", m.late_leaderboard_pipeline("2026-07", 10, None))),
    "leaderboard, limit and department": ("late_leaderboard", dict(month="2026-07", limit=5, department="Eng"), "attendance_logs",
        lambda: m.aggregate_command("attendance_logs", m.late_leaderboard_pipeline("2026-07", 5, "Eng"))),
    "trend": ("department_trend", {"department": "Eng", "from": "2026-07-04", "to": "2026-07-10"}, "employees",
        lambda: m.aggregate_command("employees", m.department_trend_pipeline("Eng", date(2026, 7, 4), 7, ["A", "B"]))),
}


@pytest.mark.parametrize("name", list(CASES))
def test_explain_runs_executionstats_on_exactly_the_endpoints_query(client, spy, db, name):
    endpoint, params, collection, expected = CASES[name]
    seed_department(db)
    r = explain(client, endpoint, **params)
    assert r.status_code == 200, r.text
    assert r.json()["endpoint"] == endpoint and r.json()["collection"] == collection
    assert len(spy.commands) == 1
    command = spy.commands[0]
    assert next(iter(command)) == "explain"  # the first key names the command
    assert command["verbosity"] == "executionStats"
    assert command["explain"] == expected()


def test_the_attendance_list_sort_keeps_its_key_order(client, spy):
    explain(client, "attendance_list")
    sort = spy.commands[0]["explain"]["sort"]
    assert list(sort.items()) == [("date", -1), ("emp_code", 1)]  # (date desc, emp_code asc): the order the index serves


def test_explain_uses_the_endpoints_own_builders_not_copies_of_them(client, spy, monkeypatch, db):
    seed_department(db)
    monkeypatch.setattr(m, "employee_monthly_pipeline", lambda *a: [{"$match": {"sentinel": "monthly"}}])
    monkeypatch.setattr(m, "department_summary_pipeline", lambda *a: [{"$match": {"sentinel": "summary"}}])
    monkeypatch.setattr(m, "late_leaderboard_pipeline", lambda *a: [{"$match": {"sentinel": "leaderboard"}}])
    monkeypatch.setattr(m, "department_trend_pipeline", lambda *a: [{"$match": {"sentinel": "trend"}}])
    monkeypatch.setattr(m, "attendance_find_spec", lambda *a: {"filter": {"sentinel": "list"}, "sort": [("x", 1)], "skip": 7, "limit": 9})
    for endpoint, sentinel, params in [("employee_monthly", "monthly", dict(emp_code="E", month="2026-07")), ("department_summary", "summary", dict(month="2026-07")),
                                       ("late_leaderboard", "leaderboard", dict(month="2026-07")),
                                       ("department_trend", "trend", {"department": "Eng", "from": "2026-07-04", "to": "2026-07-05"})]:
        spy.commands.clear()
        explain(client, endpoint, **params)
        assert spy.commands[0]["explain"]["pipeline"] == [{"$match": {"sentinel": sentinel}}], endpoint  # exactly what the builder returned
    spy.commands.clear()
    explain(client, "attendance_list")
    assert spy.commands[0]["explain"] == {"find": "attendance_logs", "filter": {"sentinel": "list"}, "sort": SON([("x", 1)]), "skip": 7, "limit": 9}


@pytest.mark.parametrize("endpoint, params, expected_calls", [
    ("attendance_list", dict(status="WFH"), []),
    ("employee_monthly", dict(emp_code="E1", month="2026-07"), []),
    ("department_summary", dict(month="2026-07"), []),
    ("late_leaderboard", dict(month="2026-07"), []),
    ("department_trend", {"department": "Eng", "from": "2026-07-04", "to": "2026-07-10"}, [("employees", "find")]),  # only the department's codes
])
def test_the_explained_query_itself_is_never_executed(client, spy, db, endpoint, params, expected_calls):
    seed_department(db)
    spy.calls.clear()
    assert explain(client, endpoint, **params).status_code == 200
    data_access = [c for c in spy.calls if c[1] in ("find", "aggregate", "count_documents", "find_one", "estimated_document_count")]
    assert data_access == expected_calls


@pytest.mark.parametrize("endpoint, params", EXPLAIN_CASES)
def test_every_request_the_real_mongodb_tests_send_is_accepted(client, spy, endpoint, params):
    r = explain(client, endpoint, **params)
    assert r.status_code == 200, (endpoint, params, r.text)
    assert len(spy.commands) == 1


def test_an_unknown_employee_or_department_is_still_explained_never_404(client, spy):
    assert explain(client, "employee_monthly", emp_code="NOPE", month="2026-07").status_code == 200
    r = explain(client, "department_trend", department="Nope", **{"from": "2026-07-04", "to": "2026-07-05"})
    assert r.status_code == 200
    lookup = next(s["$lookup"] for s in spy.commands[-1]["explain"]["pipeline"] if "$lookup" in s)
    assert lookup["pipeline"][0] == {"$match": {"emp_code": {"$in": []}}}


# ============================================================ the response
def test_response_shape_and_json_serialization_of_every_bson_type(client, spy):
    r = explain(client, "attendance_list")
    body = r.json()
    assert set(body) == {"endpoint", "collection", "explain"} and isinstance(body["explain"], dict)
    json.dumps(body, allow_nan=False)  # strictly valid JSON
    stats = body["explain"]["executionStats"]
    assert stats["nReturned"] == 20 and stats["totalDocsExamined"] == 20
    assert stats["when"] == {"$date": "2026-07-06T04:35:00Z"} and stats["oid"] == {"$oid": "64b7f3c2a1b2c3d4e5f60718"}
    assert stats["dec"] == {"$numberDecimal": "1.5"}
    assert stats["weird"] == {"$numberDouble": "NaN"} and stats["big"] == {"$numberDouble": "Infinity"}  # JSON cannot carry NaN / Infinity
    assert body["explain"]["queryPlanner"]["winningPlan"]["inputStage"]["inputStage"]["stage"] == "IXSCAN"  # passed through untouched


def test_the_plan_returned_is_whatever_mongodb_said_not_a_fixed_answer(client, spy):
    spy.result = {"queryPlanner": {"winningPlan": {"stage": "COLLSCAN"}}, "executionStats": {"nReturned": 1}, "ok": 1.0}
    body = explain(client, "attendance_list").json()
    assert body["explain"]["queryPlanner"]["winningPlan"]["stage"] == "COLLSCAN"  # a collection scan is reported as such
    with pytest.raises(AssertionError, match="collection scan"):
        assert_index_backed(body["explain"])


def test_server_host_and_port_are_removed_but_the_rest_of_serverinfo_is_kept(client, spy, monkeypatch):
    monkeypatch.setattr(m, "MONGO_URI", "mongodb://" + FAKE_USER + ":" + FAKE_PASSWORD + "@" + FAKE_DOMAIN + ":27017/?authSource=admin")
    monkeypatch.setattr(m, "MONGO_DB", FAKE_DB)
    r = explain(client, "employee_monthly", emp_code="E1", month="2026-07")
    info = r.json()["explain"]["serverInfo"]
    assert "host" not in info and "port" not in info and info["version"] == "7.0.5" and info["gitVersion"] == "abc123"
    for secret in (SECRET_HOST, FAKE_PASSWORD, FAKE_USER, FAKE_DOMAIN, FAKE_DB, "authSource"):
        assert secret not in r.text


# ============================================================ validation
@pytest.mark.parametrize("endpoint", ["attendance", "monthly", "ATTENDANCE_LIST", "attendance_list ", "explain", "late-leaderboard", "department_trends"])
def test_unknown_endpoint_names_are_422_even_with_every_parameter_supplied(client, spy, endpoint):
    """A COMPLETE parameter set, so the only thing wrong is the name: otherwise a bad name could be rejected merely for the
    parameters it happens to lack."""
    everything = {"emp_code": "E1", "month": "2026-07", "department": "Eng", "from": "2026-07-04", "to": "2026-07-10"}
    r = client.get(f"/admin/explain/{endpoint}", params=everything)
    assert r.status_code == 422 and isinstance(r.json()["detail"], list)
    assert "endpoint" in [part for error in r.json()["detail"] for part in error["loc"]]
    assert spy.commands == []


@pytest.mark.parametrize("endpoint, params, missing", [
    ("employee_monthly", {"month": "2026-07"}, "emp_code"),
    ("employee_monthly", {"emp_code": "E1"}, "month"),
    ("employee_monthly", {}, "emp_code"),
    ("department_summary", {}, "month"),
    ("department_summary", {"department": "Eng"}, "month"),
    ("late_leaderboard", {}, "month"),
    ("late_leaderboard", {"limit": 5}, "month"),
    ("department_trend", {"from": "2026-07-04", "to": "2026-07-10"}, "department"),
    ("department_trend", {"department": "Eng", "to": "2026-07-10"}, "from"),
    ("department_trend", {"department": "Eng", "from": "2026-07-04"}, "to"),
    ("department_trend", {}, "department"),
])
def test_a_missing_required_parameter_is_422_and_names_it(client, spy, endpoint, params, missing):
    r = explain(client, endpoint, **params)
    assert r.status_code == 422 and isinstance(r.json()["detail"], list)
    assert missing in [part for error in r.json()["detail"] for part in error["loc"]]
    assert spy.commands == []  # nothing was explained


def test_the_python_name_of_the_trend_parameters_is_not_accepted_only_from_and_to(client, spy):
    assert explain(client, "department_trend", department="Eng", trend_from="2026-07-04", trend_to="2026-07-10").status_code == 422


@pytest.mark.parametrize("endpoint, params", [
    ("employee_monthly", dict(emp_code="E1", month="2026-13")), ("employee_monthly", dict(emp_code="E1", month="2026-7")),
    ("department_summary", dict(month="garbage")), ("late_leaderboard", dict(month="2026-07", limit=0)),
    ("late_leaderboard", dict(month="2026-07", limit=51)), ("late_leaderboard", dict(month="2026-07", limit="x")),
    ("attendance_list", dict(page=0)), ("attendance_list", dict(page_size=101)), ("attendance_list", dict(page_size=0)),
    ("attendance_list", dict(date_from="garbage")), ("attendance_list", dict(date_to="2026-02-30")), ("attendance_list", dict(status="BOGUS")),
    ("attendance_list", dict(date_from="2026-07-10", date_to="2026-07-01")),
    ("department_trend", {"department": "Eng", "from": "2026-02-30", "to": "2026-03-01"}),
    ("department_trend", {"department": "Eng", "from": "2026-07-10", "to": "2026-07-01"}),
    ("department_trend", {"department": "Eng", "from": "2026-01-01", "to": "2026-04-03"}),  # 93 dates
])
def test_invalid_parameter_values_are_422(client, spy, endpoint, params):
    r = explain(client, endpoint, **params)
    assert r.status_code == 422 and isinstance(r.json()["detail"], list)
    assert spy.commands == []


def test_the_ninety_second_date_of_a_trend_is_allowed(client, spy):
    assert explain(client, "department_trend", department="Eng", **{"from": "2026-01-01", "to": "2026-04-02"}).status_code == 200


def test_parameters_that_do_not_apply_to_the_endpoint_are_ignored(client, spy):
    assert explain(client, "department_summary", month="2026-07", emp_code="E1", status="WFH", page=3).status_code == 200


# ============================================================ explain execution errors
SERVER_MESSAGE = f"not authorized on {FAKE_DB} to execute command: user={FAKE_USER} pw={FAKE_PASSWORD} host={SECRET_HOST}"


def test_a_server_error_is_a_500_that_does_not_echo_the_servers_message(client, spy):
    spy.error = OperationFailure(SERVER_MESSAGE, code=13, details={"codeName": "Unauthorized", "errmsg": SERVER_MESSAGE})
    r = explain(client, "employee_monthly", emp_code="E1", month="2026-07")
    assert r.status_code == 500 and "Unauthorized" in r.json()["detail"]
    for secret in (SECRET_HOST, FAKE_PASSWORD, FAKE_USER, FAKE_DB):
        assert secret not in r.text


def test_a_server_error_without_details_is_still_a_clean_500(client, spy):
    spy.error = OperationFailure(SERVER_MESSAGE, code=2)
    r = explain(client, "late_leaderboard", month="2026-07")
    assert r.status_code == 500 and SECRET_HOST not in r.text and isinstance(r.json()["detail"], str)


def test_losing_the_connection_is_a_503(client, spy):
    spy.error = AutoReconnect(f"{SECRET_HOST}:27017: connection closed")
    r = explain(client, "attendance_list")
    assert r.status_code == 503 and r.json() == {"detail": "MongoDB is not reachable"} and SECRET_HOST not in r.text


def test_any_other_driver_error_is_a_clean_500(client, spy):
    spy.error = PyMongoError(SERVER_MESSAGE)
    r = explain(client, "department_summary", month="2026-07")
    assert r.status_code == 500 and SECRET_HOST not in r.text


# ============================================================ contract and the plan assertion
def test_responses_conform_to_the_openapi_contract(client, spy):
    import jsonschema

    spec = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))
    operation = spec["paths"]["/admin/explain/{endpoint}"]["get"]
    ok_schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    jsonschema.validate(explain(client, "attendance_list").json(), ok_schema)
    assert sorted(operation["parameters"][0]["schema"]["enum"]) == sorted(["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"])
    declared = {p["name"] for p in operation["parameters"] if "name" in p}
    assert {"emp_code", "month", "department", "limit", "date_from", "date_to", "status", "from", "to"} <= declared  # the names the endpoint takes


def test_the_plan_assertion_accepts_index_plans_and_rejects_everything_else():
    good = {"queryPlanner": {"winningPlan": {"stage": "IXSCAN"}, "rejectedPlans": []}, "executionStats": {}, "stages": [{"$lookup": {"collectionScans": 0, "indexesUsed": ["i"]}}]}
    assert_index_backed(good)
    bad_cases = {
        "a collection scan": {"queryPlanner": {"winningPlan": {"stage": "COLLSCAN"}}, "executionStats": {}},
        "a collection scan among rejected plans": {"queryPlanner": {"winningPlan": {"stage": "IXSCAN"}, "rejectedPlans": [{"stage": "COLLSCAN"}]}, "executionStats": {}},
        "no index scan": {"queryPlanner": {"winningPlan": {"stage": "EOF"}}, "executionStats": {}},
        "no executionStats": {"queryPlanner": {"winningPlan": {"stage": "IXSCAN"}}},
        "a $lookup that scanned a collection": {"queryPlanner": {"winningPlan": {"stage": "IXSCAN"}}, "executionStats": {}, "stages": [{"$lookup": {"collectionScans": 3}}]},
    }
    for label, explain_output in bad_cases.items():
        with pytest.raises(AssertionError):
            assert_index_backed(explain_output)
