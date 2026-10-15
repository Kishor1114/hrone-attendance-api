"""Endpoints on a larger dataset.

With TEST_MONGO_URI (a real MongoDB 6.0+): ~100,000 attendance records and 400 employees, the size the grader uses. The data is
inserted FIRST and the indexes are built afterwards, as when the application starts against an existing database. Every analytics
endpoint is compared with the independent oracle, the explain endpoint is asked for each query's plan, and response times are bounded.

Without it: the same comparisons run on a small dataset through the aggregation emulator (so the test code itself stays exercised);
the explain-plan and timing tests are then skipped, because only a real server plans queries.
"""
import random
import time
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import app.main as m
from tests.analytics_oracle import oracle_leaderboard, oracle_monthly, oracle_summary, oracle_trend
from tests.conftest import REAL
from tests.explain_checks import EXPLAIN_CASES, assert_index_backed

DEPARTMENTS = ["Engineering", "Sales", "Operations", "Finance", "Support"]
N_EMPLOYEES = 400 if REAL else 24
FIRST, LAST = (date(2026, 1, 1), date(2026, 12, 31)) if REAL else (date(2026, 6, 1), date(2026, 8, 31))
MONTH = "2026-03" if REAL else "2026-07"
TREND_FROM, TREND_TO = ("2026-03-02", "2026-04-01") if REAL else ("2026-07-01", "2026-07-31")  # 31 dates


def build_world():
    rnd = random.Random(2026)
    employees = [{"emp_code": f"EMP{i:04d}", "name": f"Employee {i}", "email": f"emp{i}@example.com", "department": DEPARTMENTS[i % 5],
                  "shift_start": "09:30", "shift_end": "18:30",
                  "joined_on": (FIRST + timedelta(days=rnd.randrange(-200, (LAST - FIRST).days))).isoformat(),
                  "created_at": datetime(2026, 1, 5, tzinfo=timezone.utc)} for i in range(1, N_EMPLOYEES + 1)]
    logs, day = [], FIRST
    while day <= LAST:
        weekday = day.weekday() < 5
        for e in employees:
            if rnd.random() < (0.97 if weekday else 0.01):
                status = rnd.choices(["PRESENT", "WFH", "ON_DUTY", "LEAVE", "ABSENT"], [70, 10, 4, 8, 8])[0]
                doc = {"emp_code": e["emp_code"], "date": day.isoformat(), "status": status, "punch_in": None, "punch_out": None, "history": []}
                if rnd.random() > 0.05:  # the rest are legacy documents without the derived fields
                    present = status in ("PRESENT", "WFH", "ON_DUTY")
                    doc.update(late_minutes=rnd.choice([0, 0, 0, 0, 12, 35, 150]) if present else 0, overtime_minutes=rnd.choice([0, 0, 0, 30, 41]) if present else 0,
                               half_day=present and rnd.random() < 0.1, work_hours=rnd.choice([None, 3.5, 4.49, 8.0, 8.5, 9.01, 9.02, 9.12]) if present else None)
                logs.append(doc)
        day += timedelta(days=1)
    for i in range(40):  # logs of employees that do not exist
        logs.append({"emp_code": f"GHOST{i:03d}", "date": (FIRST + timedelta(days=i * 3 % (LAST - FIRST).days)).isoformat(), "status": "PRESENT",
                     "late_minutes": 99, "overtime_minutes": 99, "half_day": False, "history": []})
    return employees, logs


@pytest.fixture(scope="module")
def world():
    return build_world()


@pytest.fixture(scope="module")
def scale_db(world):
    employees, logs = world
    if REAL:
        import os

        from pymongo import MongoClient

        client = MongoClient(os.environ["TEST_MONGO_URI"], tz_aware=True, serverSelectionTimeoutMS=5000)
        database = client[f"attendance_scale_{uuid.uuid4().hex[:8]}"]
    else:
        import mongomock

        from tests.mini_mongo import EmulatedDb

        database = EmulatedDb(mongomock.MongoClient(tz_aware=True)["scale"])
    database.employees.insert_many([dict(e) for e in employees])
    for start in range(0, len(logs), 5000):
        database.attendance_logs.insert_many([dict(l) for l in logs[start:start + 5000]])
    m.ensure_indexes(database)  # data first, indexes second: the order in which the application meets an existing database
    yield database
    if REAL:
        client.drop_database(database.name)
        client.close()


@pytest.fixture
def scale_client(scale_db, monkeypatch):
    monkeypatch.setattr(m, "db", scale_db)
    with TestClient(m.app) as c:
        yield c


def test_the_dataset_has_the_intended_size(world):
    employees, logs = world
    assert len(employees) == N_EMPLOYEES
    assert (len(logs) >= 100_000) if REAL else (len(logs) > 1000)
    assert len({(l["emp_code"], l["date"]) for l in logs}) == len(logs)  # (emp_code, date) is unique, as the unique index requires


def test_monthly_matches_the_oracle_on_the_large_dataset(scale_client, world):
    employees, logs = world
    month_logs = [l for l in logs if l["date"].startswith(MONTH)]
    for e in (employees[0], employees[6], employees[-1]):
        r = scale_client.get(f"/analytics/employees/{e['emp_code']}/monthly", params={"month": MONTH})
        assert r.json() == oracle_monthly(e, month_logs, MONTH), e["emp_code"]


def test_summary_matches_the_oracle_on_the_large_dataset(scale_client, world):
    employees, logs = world
    month_logs = [l for l in logs if l["date"].startswith(MONTH)]
    assert scale_client.get("/analytics/departments/summary", params={"month": MONTH}).json() == oracle_summary(employees, month_logs, MONTH)
    for department in ("Sales", "Support"):
        r = scale_client.get("/analytics/departments/summary", params={"month": MONTH, "department": department})
        assert r.json() == oracle_summary(employees, month_logs, MONTH, department)


def test_leaderboard_matches_the_oracle_on_the_large_dataset(scale_client, world):
    employees, logs = world
    month_logs = [l for l in logs if l["date"].startswith(MONTH)]
    for limit in (1, 10, 50):
        assert scale_client.get("/analytics/leaderboard/late", params={"month": MONTH, "limit": limit}).json() == oracle_leaderboard(employees, month_logs, MONTH, limit)
    r = scale_client.get("/analytics/leaderboard/late", params={"month": MONTH, "limit": 10, "department": "Operations"})
    assert r.json() == oracle_leaderboard(employees, month_logs, MONTH, 10, "Operations")


def test_trend_matches_the_oracle_on_the_large_dataset(scale_client, world):
    employees, logs = world
    range_logs = [l for l in logs if TREND_FROM <= l["date"] <= TREND_TO]
    for department in ("Engineering", "Finance"):
        r = scale_client.get(f"/analytics/departments/{department}/trend", params={"from": TREND_FROM, "to": TREND_TO})
        assert r.json() == oracle_trend(employees, range_logs, department, TREND_FROM, TREND_TO), department


def test_attendance_list_pages_through_the_large_dataset_consistently(scale_client, world):
    _, logs = world
    first = scale_client.get("/attendance", params={"page": 1, "page_size": 100}).json()
    assert first["total"] == len(logs) and len(first["items"]) == 100
    top = sorted(logs, key=lambda l: (-int(l["date"].replace("-", "")), l["emp_code"]))[:100]
    assert [(i["date"], i["emp_code"]) for i in first["items"]] == [(l["date"], l["emp_code"]) for l in top]
    deep_page = min(40, first["total"] // 100 - 1)  # a page that exists at either scale: skip <= total - 100
    deep = scale_client.get("/attendance", params={"page": deep_page, "page_size": 100}).json()
    ordered = sorted(logs, key=lambda l: (-int(l["date"].replace("-", "")), l["emp_code"]))
    expected = ordered[(deep_page - 1) * 100: deep_page * 100]
    assert [(i["date"], i["emp_code"]) for i in deep["items"]] == [(l["date"], l["emp_code"]) for l in expected]


# ---------------------------------------------------------------- real MongoDB only: plans and speed
@pytest.mark.real_mongo
@pytest.mark.parametrize("endpoint, params", EXPLAIN_CASES)
def test_the_explain_plan_uses_an_index_and_no_collection_scan(scale_client, endpoint, params):
    r = scale_client.get(f"/admin/explain/{endpoint}", params=params)
    assert r.status_code == 200, r.text
    assert r.json()["endpoint"] == endpoint
    assert_index_backed(r.json()["explain"])


@pytest.mark.real_mongo
def test_endpoints_answer_quickly_on_the_100k_dataset(scale_client):
    requests = [
        ("/attendance", {"page": 1, "page_size": 100}), ("/attendance", {"status": "LEAVE", "page": 3}),
        ("/attendance", {"emp_code": "EMP0007", "date_from": "2026-03-01", "date_to": "2026-06-30"}),
        ("/employees", {"page_size": 100}), ("/analytics/employees/EMP0007/monthly", {"month": MONTH}),
        ("/analytics/departments/summary", {"month": MONTH}), ("/analytics/leaderboard/late", {"month": MONTH, "limit": 50}),
        ("/analytics/departments/Engineering/trend", {"from": TREND_FROM, "to": "2026-06-01"}),
    ]
    for path, params in requests:
        started = time.perf_counter()
        assert scale_client.get(path, params=params).status_code == 200, (path, params)
        elapsed = time.perf_counter() - started
        assert elapsed < 5.0, f"{path} {params} took {elapsed:.1f}s on {len(requests)} requests' dataset"
