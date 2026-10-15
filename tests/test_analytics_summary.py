"""GET /analytics/departments/summary (cross-collection aggregation). See test_analytics_monthly.py for how the `adb` fixture
chooses between the emulator and a real MongoDB."""
import random
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import app.main as m
from tests.analytics_helpers import employee, log
from tests.analytics_oracle import oracle_summary

UTC = timezone.utc
ITEM_KEYS = {"department", "headcount", "present_days", "avg_work_hours", "late_count", "total_late_minutes", "leave_count", "on_duty_count"}


@pytest.fixture
def client(adb):
    with TestClient(m.app) as c:
        yield c


def get(client, month="2026-07", **params):
    return client.get("/analytics/departments/summary", params={"month": month, **params})


@pytest.fixture
def company(adb):
    """Eng: E1 (logs), E2 (joined mid-month), E3 (joins in August: must not count), E4 (NO logs: must count). Ops: O1.
    Legal: only L1, who joins in September: the department is omitted."""
    for code, joined, dept in [("E1", "2026-01-05", "Eng"), ("E2", "2026-07-13", "Eng"), ("E3", "2026-08-01", "Eng"), ("E4", "2026-02-01", "Eng"),
                               ("O1", "2026-03-01", "Ops"), ("L1", "2026-09-01", "Legal")]:
        employee(adb, code, joined, dept)
    log(adb, "E1", "2026-07-01", hours=9.0, overtime=30)
    log(adb, "E1", "2026-07-02", "WFH", half=True, hours=4.0, late=10)
    log(adb, "E1", "2026-07-04", hours=6.0, late=20, overtime=45)       # Saturday: no present day, but hours / late count
    log(adb, "E1", "2026-07-06", "LEAVE")
    log(adb, "E1", "2026-07-07", "ON_DUTY")                              # present, but no work_hours: not in the average
    log(adb, "E2", "2026-07-14", hours=8.0, late=5)
    log(adb, "E2", "2026-07-15", "ABSENT", hours=3.0)                    # not presence: its hours must not enter the average
    log(adb, "E3", "2026-07-06", hours=100.0, late=500)                  # has not joined by month end: excluded entirely
    log(adb, "O1", "2026-07-01", hours=9.02)
    log(adb, "O1", "2026-07-02", hours=9.03)                             # mean 9.025: an exact tie
    log(adb, "O1", "2026-06-30", hours=1.0, late=9)                      # outside the month
    log(adb, "O1", "2026-08-01", hours=1.0, late=9)
    log(adb, "GHOST", "2026-07-06", hours=50.0, late=99)                 # employee does not exist: ignored
    return adb


# --------------------------------------------------------------------------- hand-computed
def test_hand_computed_summary(client, company):
    r = get(client)
    assert r.status_code == 200
    assert r.json() == {"month": "2026-07", "items": [
        {"department": "Eng", "headcount": 3, "present_days": 3.5, "avg_work_hours": 6.75, "late_count": 3, "total_late_minutes": 35,
         "leave_count": 1, "on_duty_count": 1},
        {"department": "Ops", "headcount": 1, "present_days": 2.0, "avg_work_hours": 9.03, "late_count": 0, "total_late_minutes": 0,
         "leave_count": 0, "on_duty_count": 0},
    ]}  # Eng: (9 + 4 + 6 + 8) / 4 = 6.75;  Ops: 9.025 rounds HALF-UP to 9.03;  Legal omitted: its only employee joins in September


def test_employees_with_no_logs_count_toward_headcount(client, adb):
    employee(adb, "A", department="Eng")
    employee(adb, "B", department="Eng")
    employee(adb, "C", department="Eng")
    log(adb, "A", "2026-07-06", hours=8.0)
    item = get(client).json()["items"][0]
    assert item["headcount"] == 3 and item["present_days"] == 1.0


def test_a_department_whose_staff_have_no_logs_at_all_still_appears(client, adb):
    employee(adb, "A", department="Quiet")
    assert get(client).json()["items"] == [{"department": "Quiet", "headcount": 1, "present_days": 0.0, "avg_work_hours": None, "late_count": 0,
                                            "total_late_minutes": 0, "leave_count": 0, "on_duty_count": 0}]


@pytest.mark.parametrize("joined, counted", [("2026-01-01", True), ("2026-07-01", True), ("2026-07-31", True), ("2026-08-01", False), ("2027-01-01", False)])
def test_headcount_includes_everyone_who_joined_on_or_before_the_last_day_of_the_month(client, adb, joined, counted):
    employee(adb, "A", joined, "Eng")
    items = get(client).json()["items"]
    assert (items[0]["headcount"] if items else 0) == (1 if counted else 0)


def test_a_department_with_headcount_zero_is_omitted(client, adb):
    employee(adb, "A", "2026-09-01", "Later")
    employee(adb, "B", "2026-01-01", "Now")
    assert [i["department"] for i in get(client).json()["items"]] == ["Now"]


def test_logs_of_an_employee_who_joins_later_do_not_leak_into_the_department(client, adb):
    """Ambiguity (documented): the contract defines the department's figures over its headcount; an employee who has not joined
    by month end is not in it, so even a (nonsensical) log of theirs in the month is not summed."""
    employee(adb, "A", "2026-01-01", "Eng")
    employee(adb, "B", "2026-08-15", "Eng")
    log(adb, "B", "2026-07-06", late=60, hours=9.0)
    item = get(client).json()["items"][0]
    assert (item["headcount"], item["present_days"], item["late_count"], item["avg_work_hours"]) == (1, 0.0, 0, None)


def test_average_is_over_records_not_an_average_of_employee_averages(client, adb):
    employee(adb, "A", department="Eng")
    employee(adb, "B", department="Eng")
    log(adb, "A", "2026-07-06", hours=10.0)
    for d in (6, 7, 8):
        log(adb, "B", f"2026-07-0{d}", hours=4.0)
    assert get(client).json()["items"][0]["avg_work_hours"] == 5.5  # (10 + 4 + 4 + 4) / 4; the average of averages would be 7.0


@pytest.mark.parametrize("hours, expected", [([9.02, 9.03], 9.03), ([8.01, 8.02], 8.02), ([7.0, 7.01], 7.01), ([1.11, 1.12, 1.12], 1.12), ([0.01, 0.02], 0.02)])
def test_avg_work_hours_rounds_half_up(client, adb, hours, expected):
    employee(adb, "A", department="Eng")
    for i, h in enumerate(hours):
        log(adb, "A", f"2026-07-{i + 1:02d}", hours=h)
    assert get(client).json()["items"][0]["avg_work_hours"] == expected


def test_avg_work_hours_only_uses_presence_records_that_have_hours(client, adb):
    employee(adb, "A", department="Eng")
    log(adb, "A", "2026-07-01", "PRESENT", hours=8.0)
    log(adb, "A", "2026-07-02", "PRESENT")                       # open record: no hours
    log(adb, "A", "2026-07-03", "ABSENT", hours=2.0)
    log(adb, "A", "2026-07-04", "LEAVE", hours=2.0)
    log(adb, "A", "2026-07-06", "ON_DUTY", hours=6.0)
    adb.attendance_logs.insert_one({"emp_code": "A", "date": "2026-07-07", "status": "WFH", "work_hours": None})
    assert get(client).json()["items"][0]["avg_work_hours"] == 7.0  # (8 + 6) / 2


def test_avg_work_hours_is_null_when_no_record_has_hours(client, adb):
    employee(adb, "A", department="Eng")
    log(adb, "A", "2026-07-06")
    assert get(client).json()["items"][0]["avg_work_hours"] is None


def test_present_days_count_weekdays_only_and_halve_half_days(client, adb):
    employee(adb, "A", department="Eng")
    log(adb, "A", "2026-07-03", "PRESENT")                       # Fri 1
    log(adb, "A", "2026-07-04", "PRESENT")                       # Sat 0
    log(adb, "A", "2026-07-05", "WFH")                           # Sun 0
    log(adb, "A", "2026-07-06", "WFH", half=True)                # Mon 0.5
    log(adb, "A", "2026-07-07", "ON_DUTY")                       # Tue 1
    log(adb, "A", "2026-07-08", "LEAVE", half=True)              # 0
    assert get(client).json()["items"][0]["present_days"] == 2.5


def test_late_leave_and_on_duty_counts_use_records_on_any_day(client, adb):
    employee(adb, "A", department="Eng")
    log(adb, "A", "2026-07-04", "ON_DUTY", late=12)              # Saturday
    log(adb, "A", "2026-07-05", "LEAVE")                         # Sunday
    log(adb, "A", "2026-07-06", "PRESENT", late=3)
    item = get(client).json()["items"][0]
    assert (item["late_count"], item["total_late_minutes"], item["leave_count"], item["on_duty_count"]) == (2, 15, 1, 1)


def test_records_of_an_employee_before_their_joining_date_are_counted_literally(client, adb):
    """Ambiguity (documented): the contract only restricts working days by joined_on, not the records themselves."""
    employee(adb, "A", "2026-07-20", "Eng")
    log(adb, "A", "2026-07-06", hours=8.0, late=4)
    item = get(client).json()["items"][0]
    assert (item["headcount"], item["present_days"], item["late_count"]) == (1, 1.0, 1)


@pytest.mark.parametrize("date, counted", [("2026-06-30", False), ("2026-07-01", True), ("2026-07-31", True), ("2026-08-01", False)])
def test_month_boundaries(client, adb, date, counted):
    employee(adb, "A", department="Eng")
    log(adb, "A", date, late=7)
    assert get(client).json()["items"][0]["total_late_minutes"] == (7 if counted else 0)


def test_departments_are_sorted_ascending_by_binary_order(client, adb):
    for i, dept in enumerate(["Zeta", "alpha", "Beta", "Mid"]):
        employee(adb, f"E{i}", department=dept)
    assert [i["department"] for i in get(client).json()["items"]] == ["Beta", "Mid", "Zeta", "alpha"]


def test_department_filter_is_exact_and_case_sensitive(client, company):
    assert [i["department"] for i in get(client, department="Ops").json()["items"]] == ["Ops"]
    assert [i["department"] for i in get(client, department="Eng").json()["items"]] == ["Eng"]
    assert get(client, department="eng").json()["items"] == []
    assert get(client, department="En").json()["items"] == []
    assert get(client, department="Legal").json()["items"] == []  # headcount 0 for the month: omitted even when asked for by name
    assert get(client, department="").json()["items"] == []        # an exact match on the empty string
    assert get(client, department="Eng").json()["items"][0]["headcount"] == 3  # unaffected by other departments


def test_empty_database_gives_an_empty_item_list(client, adb):
    assert get(client).json() == {"month": "2026-07", "items": []}


@pytest.mark.parametrize("month", ["2026-13", "2026-7", "202607", "garbage", "", "2026-07-01", "٢٠٢٦-٠٧", "2026-07\n"])
def test_invalid_month_is_422(client, adb, month):
    r = get(client, month)
    assert r.status_code == 422 and isinstance(r.json()["detail"], list)


def test_missing_month_is_422(client, adb):
    assert client.get("/analytics/departments/summary").status_code == 422


def test_response_shape_and_types(client, company):
    body = get(client).json()
    assert set(body) == {"month", "items"}
    for item in body["items"]:
        assert set(item) == ITEM_KEYS and "_id" not in item
        for field in ("headcount", "late_count", "total_late_minutes", "leave_count", "on_duty_count"):
            assert type(item[field]) is int, field
        assert isinstance(item["present_days"], float) and (item["avg_work_hours"] is None or isinstance(item["avg_work_hours"], float))


def test_pipeline_starts_from_employees_with_indexed_filters_and_one_correlated_lookup():
    pipeline = m.department_summary_pipeline("2026-07", "Eng")
    assert [next(iter(stage)) for stage in pipeline] == ["$match", "$lookup", "$group", "$sort"]
    assert pipeline[0]["$match"] == {"joined_on": {"$lte": "2026-07-31"}, "department": "Eng"}  # (department, joined_on) index
    lookup = pipeline[1]["$lookup"]
    assert (lookup["from"], lookup["localField"], lookup["foreignField"]) == ("attendance_logs", "emp_code", "emp_code")  # (emp_code, date) index
    assert lookup["pipeline"][0] == {"$match": {"date": {"$gte": "2026-07-01", "$lt": "2026-08-01"}}}
    assert "department" not in m.department_summary_pipeline("2026-07")[0]["$match"]


# --------------------------------------------------------------------------- differential: endpoint vs independent oracle
def random_company(seed):
    rnd = random.Random(seed)
    month = rnd.choice(["2026-07", "2026-02", "2024-02", "2026-12", "2026-09"])
    year, mon = map(int, month.split("-"))
    first = datetime(year, mon, 1)
    employees_ = [{"emp_code": f"EMP{i:04d}", "name": f"N{i}", "email": f"e{i}@x.com", "department": rnd.choice(["Eng", "Ops", "Sales", "HR"]),
                   "shift_start": "09:30", "shift_end": "18:30", "joined_on": (first + timedelta(days=rnd.randrange(-90, 50))).strftime("%Y-%m-%d"),
                   "created_at": datetime(2026, 1, 5, tzinfo=UTC)} for i in range(1, 15)]
    logs = []
    for e in employees_ + [{"emp_code": "EMP9999"}]:
        if rnd.random() < 0.25:
            continue  # some employees have no logs at all
        for offset in range(-3, 36):
            if rnd.random() < 0.6:
                day = (first + timedelta(days=offset)).strftime("%Y-%m-%d")
                doc = {"emp_code": e["emp_code"], "date": day, "status": rnd.choice(["PRESENT"] * 4 + ["WFH", "ON_DUTY", "ABSENT", "LEAVE"])}
                if rnd.random() > 0.15:
                    doc.update(late_minutes=rnd.choice([0, 0, 11, 35]), overtime_minutes=rnd.choice([0, 30, 41]), half_day=rnd.random() < 0.2,
                               work_hours=rnd.choice([None, 3.5, 4.49, 8.0, 9.01, 9.02, 9.03, 9.12, 10.5]))
                logs.append(doc)
    return month, employees_, logs


@pytest.mark.parametrize("seed", range(30))
def test_endpoint_matches_the_independent_oracle_on_random_data(client, adb, seed):
    month, employees_, logs = random_company(seed)
    adb.employees.insert_many([dict(e) for e in employees_])
    adb.attendance_logs.insert_many([dict(l) for l in logs])
    assert get(client, month).json() == oracle_summary(employees_, logs, month), seed
    for department in ("Eng", "Ops", "Sales", "HR"):
        assert get(client, month, department=department).json() == oracle_summary(employees_, logs, month, department), (seed, department)
