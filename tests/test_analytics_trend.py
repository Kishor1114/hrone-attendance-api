"""GET /analytics/departments/{department}/trend (generated day rows, correlated lookups, window moving average).
See test_analytics_monthly.py for how the `adb` fixture chooses between the emulator and a real MongoDB."""
import random
import re
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import app.main as m
from tests.analytics_helpers import employee, log
from tests.analytics_oracle import oracle_trend

UTC = timezone.utc
ITEM_KEYS = {"date", "is_working_day", "headcount", "present_count", "late_count", "attendance_rate", "moving_avg_7d"}


@pytest.fixture
def client(adb):
    with TestClient(m.app) as c:
        yield c


def trend(client, department="Eng", start="2026-07-04", end="2026-07-10"):
    return client.get(f"/analytics/departments/{department}/trend", params={"from": start, "to": end})


def column(response, field):
    return [row[field] for row in response.json()["items"]]


@pytest.fixture
def week(adb):
    """Eng: A (joined in January), B (joins Wed 2026-07-08), C (joins 2026-07-20, outside the range). Range Sat 07-04 .. Fri 07-10."""
    employee(adb, "A", "2026-01-05", "Eng")
    employee(adb, "B", "2026-07-08", "Eng")
    employee(adb, "C", "2026-07-20", "Eng")
    employee(adb, "O", "2026-01-05", "Ops")
    log(adb, "A", "2026-07-04", late=10)                  # Sat: counted in present_count and late_count, but not a working day
    log(adb, "A", "2026-07-06")                           # Mon
    log(adb, "A", "2026-07-07", "WFH", half=True)         # Tue, half day
    log(adb, "A", "2026-07-08")                           # Wed
    log(adb, "B", "2026-07-08", late=20)
    log(adb, "A", "2026-07-10", "LEAVE")                  # Fri: not a presence status
    log(adb, "B", "2026-07-10", "ABSENT")
    log(adb, "O", "2026-07-06")                           # another department: excluded
    log(adb, "O", "2026-07-07", late=99)
    log(adb, "GHOST", "2026-07-06", late=99)              # unknown employee: excluded
    return adb


# --------------------------------------------------------------------------- hand-computed
def test_hand_computed_week(client, week):
    r = trend(client)
    assert r.status_code == 200
    rows = r.json()["items"]
    assert r.json()["department"] == "Eng" and len(rows) == 7
    expected = [  # date, working, headcount, present, late, rate, moving
        ("2026-07-04", False, 1, 1.0, 1, None, None),     # Saturday
        ("2026-07-05", False, 1, 0.0, 0, None, None),     # Sunday, no records at all
        ("2026-07-06", True, 1, 1.0, 0, 1.0, 1.0),
        ("2026-07-07", True, 1, 0.5, 0, 0.5, 0.75),       # (1.0 + 0.5) / 2
        ("2026-07-08", True, 2, 2.0, 1, 1.0, 0.8333),     # B has joined: headcount 2;  (1.0 + 0.5 + 1.0) / 3
        ("2026-07-09", True, 2, 0.0, 0, 0.0, 0.625),      # a working day with no records: rate 0.0, not null;  (1+.5+1+0) / 4
        ("2026-07-10", True, 2, 0.0, 0, 0.0, 0.5),        # LEAVE and ABSENT are not present;  (1+.5+1+0+0) / 5
    ]
    assert [(r_["date"], r_["is_working_day"], r_["headcount"], r_["present_count"], r_["late_count"], r_["attendance_rate"], r_["moving_avg_7d"])
            for r_ in rows] == expected


def test_exactly_one_row_per_calendar_day_in_order(client, week):
    assert column(trend(client, start="2026-07-04", end="2026-07-24"), "date") == [(date(2026, 7, 4) + timedelta(days=i)).isoformat() for i in range(21)]


def test_single_day_range(client, week):
    rows = trend(client, start="2026-07-08", end="2026-07-08").json()["items"]
    assert rows == [{"date": "2026-07-08", "is_working_day": True, "headcount": 2, "present_count": 2.0, "late_count": 1, "attendance_rate": 1.0, "moving_avg_7d": 1.0}]


def test_a_range_with_no_records_at_all_still_returns_every_day(client, adb):
    employee(adb, "A", "2026-01-05", "Quiet")
    rows = trend(client, "Quiet", "2026-07-06", "2026-07-12").json()["items"]
    assert len(rows) == 7
    assert [r["attendance_rate"] for r in rows] == [0.0] * 5 + [None, None] and [r["moving_avg_7d"] for r in rows] == [0.0] * 7
    assert all(r["present_count"] == 0.0 and r["late_count"] == 0 and r["headcount"] == 1 for r in rows)


# --------------------------------------------------------------------------- working days, headcount, rate
def test_weekends_are_rows_with_null_rate_but_real_counts(client, week):
    rows = {r["date"]: r for r in trend(client).json()["items"]}
    assert rows["2026-07-04"]["is_working_day"] is False and rows["2026-07-04"]["attendance_rate"] is None
    assert rows["2026-07-04"]["present_count"] == 1.0 and rows["2026-07-04"]["late_count"] == 1  # weekend records still counted


@pytest.mark.parametrize("day, headcount", [("2026-07-07", 1), ("2026-07-08", 2), ("2026-07-19", 2), ("2026-07-20", 3)])
def test_headcount_counts_employees_who_joined_on_or_before_the_day(client, week, day, headcount):
    assert trend(client, start=day, end=day).json()["items"][0]["headcount"] == headcount


def test_rate_is_null_when_headcount_is_zero_even_on_a_working_day_with_records(client, adb):
    employee(adb, "A", "2026-07-20", "Eng")
    log(adb, "A", "2026-07-06")  # a record before the employee's joining date
    rows = trend(client, start="2026-07-06", end="2026-07-10").json()["items"]
    assert all(r["headcount"] == 0 and r["attendance_rate"] is None and r["moving_avg_7d"] is None for r in rows)
    assert rows[0]["present_count"] == 1.0  # Ambiguity (documented): present_count is the day's presence records, taken literally


def test_department_exists_if_it_has_any_employee_even_one_who_has_not_joined(client, adb):
    employee(adb, "A", "2030-01-01", "Future")
    r = trend(client, "Future", "2026-07-06", "2026-07-07")
    assert r.status_code == 200 and column(r, "headcount") == [0, 0]


@pytest.mark.parametrize("rate_case, present, headcount, expected", [(1, 1, 3, 0.3333), (2, 2, 3, 0.6667), (3, 1, 32, 0.0313), (4, 5, 8, 0.625)])
def test_rate_rounds_to_four_decimals_half_up(client, adb, rate_case, present, headcount, expected):
    for i in range(headcount):
        employee(adb, f"E{i:02d}", "2026-01-01", "Eng")
    for i in range(present):
        log(adb, f"E{i:02d}", "2026-07-06")
    assert column(trend(client, start="2026-07-06", end="2026-07-06"), "attendance_rate") == [expected]  # 1/32 = 0.03125 -> 0.0313 (half-even: 0.0312)


# --------------------------------------------------------------------------- the moving average
def test_window_is_the_current_row_plus_six_preceding_ROWS_inside_the_range(client, adb):
    employee(adb, "A", "2026-01-01", "Eng")
    log(adb, "A", "2026-07-06")  # present only on Monday 07-06; every other working day has rate 0.0
    rows = trend(client, start="2026-07-06", end="2026-07-17").json()["items"]
    assert [r["attendance_rate"] for r in rows] == [1.0, 0.0, 0.0, 0.0, 0.0, None, None, 0.0, 0.0, 0.0, 0.0, 0.0]
    assert [r["moving_avg_7d"] for r in rows] == [1.0, 0.5, 0.3333, 0.25, 0.2, 0.2, 0.2, 0.0, 0.0, 0.0, 0.0, 0.0]
    # 07-12 (7th row) still sees Monday; on 07-13 the window is rows 2..8, so Monday has dropped out: a window of 7 ROWS, weekends included


def test_rows_before_from_are_not_considered(client, week):
    rows = trend(client, start="2026-07-08", end="2026-07-10").json()["items"]  # 07-06 and 07-07 have records but are before `from`
    assert [r["moving_avg_7d"] for r in rows] == [1.0, 0.5, 0.3333]  # (1.0), (1.0 + 0.0) / 2, (1.0 + 0.0 + 0.0) / 3


def test_moving_average_is_null_while_the_window_holds_no_rate(client, week):
    rows = trend(client, start="2026-07-04", end="2026-07-05").json()["items"]  # Saturday and Sunday only
    assert [r["attendance_rate"] for r in rows] == [None, None] and [r["moving_avg_7d"] for r in rows] == [None, None]


def test_moving_average_ignores_null_rates_of_weekends(client, adb):
    employee(adb, "A", "2026-01-01", "Eng")
    for d in ("2026-07-10", "2026-07-13"):
        log(adb, "A", d)  # Fri and Mon present; Sat and Sun in between have null rates
    rows = {r["date"]: r for r in trend(client, start="2026-07-10", end="2026-07-13").json()["items"]}
    assert rows["2026-07-12"]["moving_avg_7d"] == 1.0 and rows["2026-07-13"]["moving_avg_7d"] == 1.0  # the two null rows do not dilute the mean


def test_moving_average_uses_unrounded_rates(client, adb):
    """Interpretation (documented): the mean is taken over the exact rates and rounded once. Rates 1/6 and 0 average to
    0.08333 -> 0.0833; averaging the already-rounded 0.1667 and 0.0 would give 0.08335 -> 0.0834."""
    for i in range(6):
        employee(adb, f"E{i}", "2026-01-01", "Eng")
    log(adb, "E0", "2026-07-06")
    rows = trend(client, start="2026-07-06", end="2026-07-07").json()["items"]
    assert [r["attendance_rate"] for r in rows] == [0.1667, 0.0] and rows[1]["moving_avg_7d"] == 0.0833


# --------------------------------------------------------------------------- counts
def test_present_count_halves_half_days_and_only_counts_presence_statuses(client, adb):
    for code in "ABCDE":
        employee(adb, code, "2026-01-01", "Eng")
    log(adb, "A", "2026-07-06", "PRESENT")
    log(adb, "B", "2026-07-06", "WFH", half=True)
    log(adb, "C", "2026-07-06", "ON_DUTY")
    log(adb, "D", "2026-07-06", "LEAVE", half=True)
    log(adb, "E", "2026-07-06", "ABSENT")
    row = trend(client, start="2026-07-06", end="2026-07-06").json()["items"][0]
    assert (row["present_count"], row["attendance_rate"], row["headcount"]) == (2.5, 0.5, 5)


def test_late_count_counts_records_with_late_minutes_on_any_day_and_status(client, adb):
    employee(adb, "A", "2026-01-01", "Eng")
    employee(adb, "B", "2026-01-01", "Eng")
    log(adb, "A", "2026-07-11", "WFH", late=5)    # Saturday
    log(adb, "B", "2026-07-11", "ON_DUTY", late=1)
    adb.attendance_logs.insert_one({"emp_code": "A", "date": "2026-07-12", "status": "PRESENT"})  # legacy: no late_minutes
    rows = trend(client, start="2026-07-11", end="2026-07-12").json()["items"]
    assert [r["late_count"] for r in rows] == [2, 0]


def test_other_departments_and_unknown_employees_are_excluded(client, week):
    row = {r["date"]: r for r in trend(client).json()["items"]}["2026-07-07"]
    assert row["present_count"] == 0.5 and row["late_count"] == 0  # Ops' record with late 99 on 07-07 is not Eng's


def test_legacy_records_without_fields(client, adb):
    employee(adb, "A", "2026-01-01", "Eng")
    adb.attendance_logs.insert_one({"emp_code": "A", "date": "2026-07-06", "status": "PRESENT"})
    adb.attendance_logs.insert_one({"emp_code": "A", "date": "2026-07-07", "status": "WFH", "half_day": None, "late_minutes": None})
    assert column(trend(client, start="2026-07-06", end="2026-07-07"), "present_count") == [1.0, 1.0]


# --------------------------------------------------------------------------- range, 404 and validation
def test_unknown_department_is_404(client, week):
    r = trend(client, "Nope")
    assert r.status_code == 404 and isinstance(r.json()["detail"], str)
    assert trend(client, "eng").status_code == 404  # exact, case-sensitive


def test_department_names_with_spaces_and_symbols(client, adb):
    employee(adb, "A", "2026-01-01", "R&D Team")
    employee(adb, "B", "2026-01-01", "Ünïcode Dept")
    r = client.get("/analytics/departments/R%26D%20Team/trend", params={"from": "2026-07-06", "to": "2026-07-06"})
    assert r.status_code == 200 and r.json()["department"] == "R&D Team"
    assert client.get("/analytics/departments/%C3%9Cn%C3%AFcode%20Dept/trend", params={"from": "2026-07-06", "to": "2026-07-06"}).status_code == 200


def test_range_limit_is_92_calendar_dates_inclusive(client, adb):
    employee(adb, "A", "2026-01-01", "Eng")
    assert len(trend(client, start="2026-01-01", end="2026-04-02").json()["items"]) == 92  # 92 dates: allowed
    r = trend(client, start="2026-01-01", end="2026-04-03")                                  # 93 dates: rejected
    assert r.status_code == 422 and isinstance(r.json()["detail"], list)


def test_to_before_from_is_422_and_equal_is_fine(client, week):
    r = trend(client, start="2026-07-10", end="2026-07-09")
    assert r.status_code == 422 and isinstance(r.json()["detail"], list)
    assert trend(client, start="2026-07-10", end="2026-07-10").status_code == 200


def test_range_validation_comes_before_the_department_lookup(client, adb):
    assert trend(client, "Nope", "2026-07-10", "2026-07-09").status_code == 422


@pytest.mark.parametrize("start, end", [("garbage", "2026-07-10"), ("2026-07-04", "garbage"), ("2026-02-30", "2026-03-01"), ("2026-7-4", "2026-07-10"),
                                         ("٢٠٢٦-٠٧-٠٤", "2026-07-10"), ("2026-07-04\n", "2026-07-10"), ("", "2026-07-10"), ("2026-07-04", "")])
def test_invalid_dates_are_422(client, week, start, end):
    r = trend(client, start=start, end=end)
    assert r.status_code == 422 and isinstance(r.json()["detail"], list)


def test_missing_from_or_to_is_422(client, week):
    assert client.get("/analytics/departments/Eng/trend", params={"to": "2026-07-10"}).status_code == 422
    assert client.get("/analytics/departments/Eng/trend", params={"from": "2026-07-04"}).status_code == 422
    assert client.get("/analytics/departments/Eng/trend").status_code == 422


def test_ranges_across_a_year_boundary_and_a_leap_day(client, adb):
    employee(adb, "A", "2020-01-01", "Eng")
    rows = trend(client, start="2025-12-30", end="2026-01-02").json()["items"]
    assert [r["date"] for r in rows] == ["2025-12-30", "2025-12-31", "2026-01-01", "2026-01-02"] and [r["is_working_day"] for r in rows] == [True, True, True, True]
    rows = trend(client, start="2024-02-28", end="2024-03-01").json()["items"]
    assert [r["date"] for r in rows] == ["2024-02-28", "2024-02-29", "2024-03-01"]
    assert [r["is_working_day"] for r in trend(client, start="2026-07-03", end="2026-07-06").json()["items"]] == [True, False, False, True]  # Fri Sat Sun Mon


def test_response_shape_and_types(client, week):
    body = trend(client).json()
    assert set(body) == {"department", "items"}
    for row in body["items"]:
        assert set(row) == ITEM_KEYS and "_id" not in row and "dt" not in row and "i" not in row and "dow" not in row
        assert type(row["headcount"]) is int and type(row["late_count"]) is int and type(row["is_working_day"]) is bool
        assert isinstance(row["present_count"], float)
        assert all(row[f] is None or isinstance(row[f], float) for f in ("attendance_rate", "moving_avg_7d"))
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", row["date"])


def test_the_pipeline_builds_the_series_in_the_database_not_in_python():
    pipeline = m.department_trend_pipeline("Eng", date(2026, 1, 1), 92, ["A", "B"])
    names = [next(iter(stage)) for stage in pipeline]
    assert names[:4] == ["$match", "$limit", "$project", "$unwind"] and "$setWindowFields" in names
    assert pipeline[0] == {"$match": {"department": "Eng"}}  # the (department, emp_code) index
    assert pipeline[2]["$project"]["i"] == {"$range": [0, 92]}  # the day offsets are generated by $range
    window = next(s for s in pipeline if "$setWindowFields" in s)["$setWindowFields"]
    assert window["output"]["moving_avg_7d"]["window"] == {"documents": [-6, 0]} and window["sortBy"] == {"dt": 1}
    lookups = [s["$lookup"] for s in pipeline if "$lookup" in s]
    assert lookups[0]["foreignField"] == "date" and lookups[0]["pipeline"][0] == {"$match": {"emp_code": {"$in": ["A", "B"]}}}  # (date, emp_code) index

    def leaves(node):
        if isinstance(node, dict):
            for v in node.values():
                yield from leaves(v)
        elif isinstance(node, list):
            for v in node:
                yield from leaves(v)
        else:
            yield node

    assert [x for x in leaves(pipeline) if isinstance(x, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", x)] == []  # no Python-built list of days
    assert [x for x in leaves(pipeline) if isinstance(x, datetime)] == [datetime(2026, 1, 1, tzinfo=UTC)]     # one start value, nothing more


# --------------------------------------------------------------------------- differential: endpoint vs independent oracle
def random_world(seed):
    rnd = random.Random(seed)
    start = date(2026, 1, 1) + timedelta(days=rnd.randrange(0, 330))
    employees_ = [{"emp_code": f"EMP{i:04d}", "name": f"N{i}", "email": f"e{i}@x.com", "department": rnd.choice(["Eng", "Ops"]), "shift_start": "09:30",
                   "shift_end": "18:30", "joined_on": (start + timedelta(days=rnd.randrange(-60, 40))).isoformat(),
                   "created_at": datetime(2026, 1, 5, tzinfo=UTC)} for i in range(1, 13)]
    logs = []
    for e in employees_ + [{"emp_code": "EMP9999"}]:
        for offset in range(-5, 50):
            if rnd.random() < 0.45:
                doc = {"emp_code": e["emp_code"], "date": (start + timedelta(days=offset)).isoformat(),
                       "status": rnd.choice(["PRESENT"] * 4 + ["WFH", "ON_DUTY", "ABSENT", "LEAVE"])}
                if rnd.random() > 0.12:
                    doc.update(late_minutes=rnd.choice([0, 0, 15, 40]), half_day=rnd.random() < 0.2)
                logs.append(doc)
    return start, employees_, logs


@pytest.mark.parametrize("seed", range(25))
def test_endpoint_matches_the_independent_oracle_on_random_data(client, adb, seed):
    start, employees_, logs = random_world(seed)
    adb.employees.insert_many([dict(e) for e in employees_])
    adb.attendance_logs.insert_many([dict(l) for l in logs])
    rnd = random.Random(seed + 1000)
    for department in ("Eng", "Ops"):
        for _ in range(3):
            from_ = start + timedelta(days=rnd.randrange(-3, 20))
            to = from_ + timedelta(days=rnd.choice([0, 1, 6, 7, 8, 20, 44]))
            expected = oracle_trend(employees_, logs, department, from_.isoformat(), to.isoformat())
            assert trend(client, department, from_.isoformat(), to.isoformat()).json() == expected, (seed, department, from_, to)
