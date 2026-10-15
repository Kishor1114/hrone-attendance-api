"""GET /analytics/employees/{emp_code}/monthly.

Runs through the `adb` fixture: tests/mini_mongo.py emulates the aggregation by default; with TEST_MONGO_URI the identical tests
run on a real MongoDB (that run is the authority on whether a real server accepts the pipeline).
"""
import random
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import app.main as m
from tests.analytics_oracle import oracle_monthly

UTC = timezone.utc
KEYS = {"emp_code", "month", "working_days", "present_days", "leave_days", "late_count", "total_late_minutes", "total_overtime_minutes", "attendance_pct"}


@pytest.fixture
def client(adb):
    with TestClient(m.app) as c:
        yield c


def employee(adb, code="EMP0001", joined="2026-01-05", department="Engineering"):
    doc = {"emp_code": code, "name": f"Emp {code}", "email": f"{code.lower()}@example.com", "department": department,
           "shift_start": "09:30", "shift_end": "18:30", "joined_on": joined, "created_at": datetime(2026, 1, 5, 4, 30, tzinfo=UTC)}
    adb.employees.insert_one(doc)
    return doc


def log(adb, code, date, status="PRESENT", late=0, overtime=0, half=False, **extra):
    doc = {"emp_code": code, "date": date, "status": status, "punch_in": None, "punch_out": None, "work_hours": None,
           "late_minutes": late, "overtime_minutes": overtime, "half_day": half, "history": []}
    doc.update(extra)
    adb.attendance_logs.insert_one(doc)
    return doc


def get(client, code="EMP0001", month="2026-07"):
    return client.get(f"/analytics/employees/{code}/monthly", params={"month": month})


# --------------------------------------------------------------------------- hand-computed
def test_hand_computed_month_with_every_kind_of_record(client, adb):
    employee(adb)
    employee(adb, "EMP0002")
    log(adb, "EMP0001", "2026-06-30", late=50, overtime=50)           # the day before the month: excluded
    log(adb, "EMP0001", "2026-07-01")                                  # Wed, 1.0
    log(adb, "EMP0001", "2026-07-02", "WFH", half=True)                # Thu, half day -> 0.5
    log(adb, "EMP0001", "2026-07-03", "ON_DUTY", overtime=10)          # Fri, 1.0, 10 overtime
    log(adb, "EMP0001", "2026-07-04", late=20, overtime=45)            # SATURDAY: not a present day, but late + overtime count
    log(adb, "EMP0001", "2026-07-05", "LEAVE")                         # SUNDAY leave still counts as a LEAVE record
    log(adb, "EMP0001", "2026-07-06", "LEAVE")
    log(adb, "EMP0001", "2026-07-07", "ABSENT", half=True)             # absent: worth nothing whatever half_day says
    log(adb, "EMP0001", "2026-07-08", late=15)                         # Wed, 1.0, late
    adb.attendance_logs.insert_one({"emp_code": "EMP0001", "date": "2026-07-09", "status": "PRESENT"})  # legacy: no derived fields at all
    log(adb, "EMP0001", "2026-08-01", late=33)                         # the day after: excluded
    log(adb, "EMP0002", "2026-07-01", late=99)                         # someone else: excluded
    r = get(client)
    assert r.status_code == 200
    assert r.json() == {"emp_code": "EMP0001", "month": "2026-07", "working_days": 23, "present_days": 4.5, "leave_days": 2,
                        "late_count": 2, "total_late_minutes": 35, "total_overtime_minutes": 55, "attendance_pct": 19.57}  # 4.5 / 23 = 19.5652...


@pytest.mark.parametrize("joined, working_days", [
    ("2026-01-05", 23), ("2026-06-15", 23), ("2026-07-01", 23), ("2026-07-02", 22), ("2026-07-10", 16), ("2026-07-13", 15),
    ("2026-07-18", 10),  # a Saturday: counting starts on the next weekday
    ("2026-07-20", 10), ("2026-07-31", 1), ("2026-08-01", 0), ("2027-01-01", 0),
])
def test_working_days_are_counted_from_joined_on(client, adb, joined, working_days):
    employee(adb, joined=joined)
    body = get(client).json()
    assert body["working_days"] == working_days
    assert body["attendance_pct"] == (None if working_days == 0 else 0.0)  # nothing present: 0.0, or null with no working days


def test_attendance_pct_is_null_when_there_are_no_working_days_even_if_a_record_exists(client, adb):
    """Ambiguity (documented): the contract does not say what a record dated before joined_on means. Taken literally it is a
    weekday record of the employee, so present_days counts it while working_days is 0 and attendance_pct is null."""
    employee(adb, joined="2026-08-15")
    log(adb, "EMP0001", "2026-07-06")
    body = get(client).json()
    assert (body["working_days"], body["present_days"], body["attendance_pct"]) == (0, 1.0, None)


@pytest.mark.parametrize("half_days, full_days, expected", [(1, 0, 3.13), (1, 2, 15.63), (3, 0, 9.38), (0, 1, 6.25)])
def test_attendance_pct_rounds_half_up_not_half_even(client, adb, half_days, full_days, expected):
    employee(adb, joined="2026-07-10")  # 16 working days: 0.5/16*100 = 3.125 exactly, 2.5/16*100 = 15.625, 1.5/16*100 = 9.375
    dates = [f"2026-07-{d}" for d in (14, 15, 16, 17)]
    for i in range(half_days):
        log(adb, "EMP0001", dates[i], half=True)
    for i in range(full_days):
        log(adb, "EMP0001", dates[half_days + i])
    assert get(client).json()["attendance_pct"] == expected  # a half-even rounding would give 3.12 and 15.62


def test_weekend_records_feed_late_overtime_and_leave_but_not_present_days(client, adb):
    employee(adb)
    log(adb, "EMP0001", "2026-07-04", late=10, overtime=30)               # Sat
    log(adb, "EMP0001", "2026-07-05", "WFH", late=5, overtime=40)         # Sun
    log(adb, "EMP0001", "2026-07-11", "LEAVE")                            # Sat
    body = get(client).json()
    assert (body["present_days"], body["late_count"], body["total_late_minutes"], body["total_overtime_minutes"], body["leave_days"]) == (0.0, 2, 15, 70, 1)


@pytest.mark.parametrize("date, counted", [("2026-06-30", False), ("2026-07-01", True), ("2026-07-31", True), ("2026-08-01", False)])
def test_month_boundaries_are_inclusive_exclusive(client, adb, date, counted):
    employee(adb)
    log(adb, "EMP0001", date, late=7)
    assert get(client).json()["total_late_minutes"] == (7 if counted else 0)


def test_december_does_not_leak_into_january(client, adb):
    employee(adb)
    for d in ("2026-11-30", "2026-12-01", "2026-12-31", "2027-01-01"):
        log(adb, "EMP0001", d, late=1)
    assert get(client, month="2026-12").json()["total_late_minutes"] == 2


def test_leap_february(client, adb):
    employee(adb, joined="2023-01-02")
    log(adb, "EMP0001", "2024-02-29")  # Thursday
    log(adb, "EMP0001", "2024-03-01")
    body = get(client, month="2024-02").json()
    assert (body["working_days"], body["present_days"], body["attendance_pct"]) == (21, 1.0, 4.76)  # 1/21 = 4.7619...
    assert get(client, month="2023-02").json()["working_days"] == 20


@pytest.mark.parametrize("status, weight", [("PRESENT", 1.0), ("WFH", 1.0), ("ON_DUTY", 1.0), ("ABSENT", 0.0), ("LEAVE", 0.0)])
def test_only_presence_statuses_count_as_present(client, adb, status, weight):
    employee(adb)
    log(adb, "EMP0001", "2026-07-06", status)
    assert get(client).json()["present_days"] == weight


def test_half_day_halves_presence_statuses_only(client, adb):
    employee(adb)
    log(adb, "EMP0001", "2026-07-06", "PRESENT", half=True)
    log(adb, "EMP0001", "2026-07-07", "ON_DUTY", half=True)
    log(adb, "EMP0001", "2026-07-08", "LEAVE", half=True)
    assert get(client).json()["present_days"] == 1.0


def test_an_open_record_counts_as_a_full_day_and_the_stored_half_day_is_authoritative(client, adb):
    employee(adb)
    log(adb, "EMP0001", "2026-07-06", punch_in=datetime(2026, 7, 6, 4, 0, tzinfo=UTC))  # no punch-out yet, half_day false
    log(adb, "EMP0001", "2026-07-07", punch_in=datetime(2026, 7, 7, 4, 0, tzinfo=UTC),
        punch_out=datetime(2026, 7, 7, 13, 0, tzinfo=UTC), work_hours=9.0, half=True)  # inconsistent on purpose: 9 h but stored half_day
    assert get(client).json()["present_days"] == 1.5


def test_stored_derived_values_are_used_not_recomputed_from_punch_times(client, adb):
    employee(adb)
    log(adb, "EMP0001", "2026-07-06", late=99, overtime=77, punch_in=datetime(2026, 7, 6, 4, 0, tzinfo=UTC),
        punch_out=datetime(2026, 7, 6, 13, 0, tzinfo=UTC), work_hours=9.0)  # punch times say on time, no overtime
    body = get(client).json()
    assert (body["total_late_minutes"], body["late_count"], body["total_overtime_minutes"]) == (99, 1, 77)


def test_legacy_documents_with_missing_or_null_fields(client, adb):
    employee(adb)
    adb.attendance_logs.insert_many([
        {"emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT"},
        {"emp_code": "EMP0001", "date": "2026-07-07", "status": "PRESENT", "late_minutes": None, "overtime_minutes": None, "half_day": None},
        {"emp_code": "EMP0001", "date": "2026-07-08", "status": "LEAVE"},
    ])
    body = get(client).json()
    assert (body["present_days"], body["late_count"], body["total_late_minutes"], body["total_overtime_minutes"], body["leave_days"]) == (2.0, 0, 0, 0, 1)


def test_an_employee_with_no_logs_gets_zeros(client, adb):
    employee(adb)
    assert get(client).json() == {"emp_code": "EMP0001", "month": "2026-07", "working_days": 23, "present_days": 0.0, "leave_days": 0,
                                  "late_count": 0, "total_late_minutes": 0, "total_overtime_minutes": 0, "attendance_pct": 0.0}


def test_unknown_employee_is_404_even_with_orphan_logs(client, adb):
    employee(adb)
    log(adb, "EMP9999", "2026-07-06", late=5)
    r = get(client, "EMP9999")
    assert r.status_code == 404 and isinstance(r.json()["detail"], str)


@pytest.mark.parametrize("month", ["2026-13", "2026-00", "2026-7", "202607", "2026-07-01", "garbage", "", "٢٠٢٦-٠٧", "2026-07\n", "26-07"])
def test_invalid_month_is_422(client, adb, month):
    employee(adb)
    r = get(client, month=month)
    assert r.status_code == 422 and isinstance(r.json()["detail"], list)


def test_missing_month_is_422_and_validation_precedes_the_employee_lookup(client, adb):
    employee(adb)
    assert client.get("/analytics/employees/EMP0001/monthly").status_code == 422
    assert get(client, "EMP9999", "2026-13").status_code == 422


def test_response_has_exactly_the_contract_fields_with_the_contract_types(client, adb):
    employee(adb)
    log(adb, "EMP0001", "2026-07-06", late=3, overtime=31)
    body = get(client).json()
    assert set(body) == KEYS
    for field in ("working_days", "leave_days", "late_count", "total_late_minutes", "total_overtime_minutes"):
        assert type(body[field]) is int, field
    assert isinstance(body["present_days"], float) and isinstance(body["attendance_pct"], float)


def test_pipeline_is_a_match_on_the_indexed_fields_followed_by_one_group():
    pipeline = m.employee_monthly_pipeline("EMP0001", "2026-07")
    assert [next(iter(stage)) for stage in pipeline] == ["$match", "$group"]
    assert pipeline[0]["$match"] == {"emp_code": "EMP0001", "date": {"$gte": "2026-07-01", "$lt": "2026-08-01"}}  # the (emp_code, date) index
    assert len(pipeline[1]["$group"]["present_days"]["$sum"]["$cond"][0]["$in"][1]) == 23  # the month's weekdays: a parameter, not a loop


# --------------------------------------------------------------------------- differential: endpoint vs independent oracle
def random_world(seed):
    rnd = random.Random(seed)
    month = rnd.choice(["2026-07", "2026-02", "2024-02", "2026-12", "2026-01", "2026-09"])
    year, mon = map(int, month.split("-"))
    start = datetime(year, mon, 1) - timedelta(days=4)
    employees_ = [{"emp_code": f"EMP{i:04d}", "name": f"N{i}", "email": f"e{i}@x.com", "department": rnd.choice(["Eng", "Ops"]),
                   "shift_start": "09:30", "shift_end": "18:30",
                   "joined_on": (datetime(year, mon, 1) + timedelta(days=rnd.randrange(-60, 45))).strftime("%Y-%m-%d"),
                   "created_at": datetime(2026, 1, 5, tzinfo=UTC)} for i in range(1, 6)]
    logs = []
    for e in employees_ + [{"emp_code": "EMP9999"}]:  # an orphan too
        for offset in range(0, 40):
            if rnd.random() < 0.7:
                day = (start + timedelta(days=offset)).strftime("%Y-%m-%d")
                doc = {"emp_code": e["emp_code"], "date": day, "status": rnd.choice(["PRESENT"] * 4 + ["WFH", "ON_DUTY", "ABSENT", "LEAVE"])}
                if rnd.random() > 0.15:  # the rest are legacy documents without the derived fields
                    doc.update(late_minutes=rnd.choice([0, 0, 0, 11, 35, 150]), overtime_minutes=rnd.choice([0, 0, 30, 41]), half_day=rnd.random() < 0.2)
                logs.append(doc)
    return month, employees_, logs


@pytest.mark.parametrize("seed", range(30))
def test_endpoint_matches_the_independent_oracle_on_random_data(client, adb, seed):
    month, employees_, logs = random_world(seed)
    adb.employees.insert_many([dict(e) for e in employees_])
    adb.attendance_logs.insert_many([dict(l) for l in logs])
    for e in employees_:
        assert get(client, e["emp_code"], month).json() == oracle_monthly(e, logs, month), (seed, e["emp_code"])
