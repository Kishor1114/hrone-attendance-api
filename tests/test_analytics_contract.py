"""The four analytics responses validated against the schemas of the supplied openapi.yaml, plus a static check that every
pipeline's leading filter is covered by an index declared in app/main.py (so no index changes are needed)."""
import pytest
import yaml
from fastapi.testclient import TestClient
import jsonschema

import app.main as m
from tests.analytics_helpers import employee, log
from tests.test_contract import SPEC_PATH, _to_json_schema


@pytest.fixture(scope="module")
def components():
    return _to_json_schema(yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))["components"])


@pytest.fixture
def check(components):
    def run(name, payload):
        if name in components["schemas"]:
            schema = {"$ref": f"#/components/schemas/{name}", "components": components}
        else:
            schema = {**components["responses"][name]["content"]["application/json"]["schema"], "components": components}
        jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(payload)
    return run


@pytest.fixture
def client(adb):
    with TestClient(m.app) as c:
        yield c


@pytest.fixture
def world(adb):
    employee(adb, "E1", "2026-01-05", "Eng")
    employee(adb, "E2", "2026-07-08", "Eng")
    employee(adb, "E3", "2026-02-01", "Ops")
    log(adb, "E1", "2026-07-06", late=12, overtime=40, hours=9.1)
    log(adb, "E1", "2026-07-07", "WFH", half=True, hours=4.0)
    log(adb, "E2", "2026-07-08", late=30, hours=8.0)
    log(adb, "E3", "2026-07-06", "LEAVE")
    return adb


def test_monthly_conforms(client, world, check):
    r = client.get("/analytics/employees/E1/monthly", params={"month": "2026-07"})
    assert r.status_code == 200
    check("EmployeeMonthly", r.json())
    empty = client.get("/analytics/employees/E3/monthly", params={"month": "2026-01"}).json()
    check("EmployeeMonthly", empty)
    never = client.get("/analytics/employees/E2/monthly", params={"month": "2026-06"}).json()  # joined later: attendance_pct is null
    assert never["attendance_pct"] is None
    check("EmployeeMonthly", never)
    assert client.get("/analytics/employees/NOPE/monthly", params={"month": "2026-07"}).status_code == 404
    check("Error", client.get("/analytics/employees/NOPE/monthly", params={"month": "2026-07"}).json())
    check("ValidationError", client.get("/analytics/employees/E1/monthly", params={"month": "2026-13"}).json())


def test_summary_conforms(client, world, check):
    r = client.get("/analytics/departments/summary", params={"month": "2026-07"})
    assert r.status_code == 200 and len(r.json()["items"]) == 2
    check("DepartmentSummary", r.json())
    check("DepartmentSummary", client.get("/analytics/departments/summary", params={"month": "2020-01"}).json())  # empty items
    check("DepartmentSummary", client.get("/analytics/departments/summary", params={"month": "2026-07", "department": "Ops"}).json())
    check("ValidationError", client.get("/analytics/departments/summary").json())


def test_leaderboard_conforms(client, world, check):
    r = client.get("/analytics/leaderboard/late", params={"month": "2026-07"})
    assert r.status_code == 200 and [i["emp_code"] for i in r.json()["items"]] == ["E2", "E1"]
    check("Leaderboard", r.json())
    check("Leaderboard", client.get("/analytics/leaderboard/late", params={"month": "2020-01"}).json())
    check("ValidationError", client.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 0}).json())


def test_trend_conforms_including_null_rates_and_averages(client, world, check):
    r = client.get("/analytics/departments/Eng/trend", params={"from": "2026-07-04", "to": "2026-07-10"})
    assert r.status_code == 200 and r.json()["items"][0]["attendance_rate"] is None and r.json()["items"][0]["moving_avg_7d"] is None
    check("Trend", r.json())
    check("Error", client.get("/analytics/departments/Nope/trend", params={"from": "2026-07-04", "to": "2026-07-10"}).json())
    check("ValidationError", client.get("/analytics/departments/Eng/trend", params={"from": "2026-07-10", "to": "2026-07-04"}).json())
    check("ValidationError", client.get("/analytics/departments/Eng/trend", params={"from": "2026-01-01", "to": "2026-12-31"}).json())


# --------------------------------------------------------------------------- the pipelines' leading filters are covered by declared indexes
def index_key_lists(collection):
    return [[field for field, _direction in keys] for keys, _options in m.INDEXES[collection]]


def covered(collection, equality, range_field=None):
    """True if some declared index starts with the equality fields (any order) and then the range field, i.e. the filter can be
    answered by an index scan instead of a collection scan."""
    for fields in index_key_lists(collection):
        head = fields[:len(equality)]
        if set(head) == set(equality) and (range_field is None or (len(fields) > len(equality) and fields[len(equality)] == range_field)):
            return True
    return False


@pytest.mark.parametrize("collection, equality, range_field, what", [
    ("attendance_logs", ["emp_code"], "date", "monthly: first stage"),
    ("employees", ["department"], "joined_on", "summary with a department filter: first stage"),
    ("employees", [], "joined_on", "summary without a filter: first stage (range on joined_on)"),
    ("attendance_logs", ["emp_code"], "date", "summary: correlated lookup into the month of logs"),
    ("attendance_logs", [], "date", "leaderboard: first stage (month range)"),
    ("employees", ["emp_code"], None, "leaderboard: lookup of the employee"),
    ("employees", ["department"], None, "trend: seed stage and department codes"),
    ("attendance_logs", ["date"], "emp_code", "trend: per-day lookup (date equality, emp_code in the department)"),
    ("employees", ["department"], "joined_on", "trend: per-day headcount lookup"),
])
def test_every_pipeline_filter_is_covered_by_a_declared_index(collection, equality, range_field, what):
    assert covered(collection, equality, range_field), f"no declared index serves: {what}"


def test_the_filters_in_that_table_are_the_ones_the_pipelines_really_use():
    monthly = m.employee_monthly_pipeline("E", "2026-07")[0]["$match"]
    assert set(monthly) == {"emp_code", "date"}
    summary = m.department_summary_pipeline("2026-07", "Eng")
    assert set(summary[0]["$match"]) == {"joined_on", "department"} and set(summary[1]["$lookup"]["pipeline"][0]["$match"]) == {"date"}
    board = m.late_leaderboard_pipeline("2026-07", 5)[0]["$match"]
    assert set(board) == {"date", "late_minutes"}
    import datetime

    trend = m.department_trend_pipeline("Eng", datetime.date(2026, 7, 1), 7, ["A"])
    lookups = [s["$lookup"] for s in trend if "$lookup" in s]
    assert trend[0] == {"$match": {"department": "Eng"}} and lookups[0]["foreignField"] == "date" and set(lookups[1]["pipeline"][0]["$match"]) == {"department", "$expr"}
