"""GET /analytics/leaderboard/late (window-function ranking). See test_analytics_monthly.py for the `adb` fixture."""
import random
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import app.main as m
from tests.analytics_helpers import employee, log
from tests.analytics_oracle import oracle_leaderboard

UTC = timezone.utc
ITEM_KEYS = {"rank", "emp_code", "name", "department", "total_late_minutes", "late_count"}


@pytest.fixture
def client(adb):
    with TestClient(m.app) as c:
        yield c


def get(client, month="2026-07", **params):
    return client.get("/analytics/leaderboard/late", params={"month": month, **params})


def ranks(response):
    return [(i["rank"], i["emp_code"], i["total_late_minutes"]) for i in response.json()["items"]]


# totals: E1=120 (2 records), E2=E3=100, E4=80, E5=E6=E7=50, E8=10  ->  ranks 1, 2, 2, 4, 5, 5, 5, 8
TOTALS = {"E1": [70, 50], "E2": [100], "E3": [60, 40], "E4": [80], "E5": [50], "E6": [20, 30], "E7": [25, 25], "E8": [10]}
EXPECTED = [(1, "E1", 120), (2, "E2", 100), (2, "E3", 100), (4, "E4", 80), (5, "E5", 50), (5, "E6", 50), (5, "E7", 50), (8, "E8", 10)]


@pytest.fixture
def board(adb):
    codes = list(TOTALS)
    random.Random(4).shuffle(codes)  # inserted out of order: the order must come from the pipeline
    for code in codes:
        employee(adb, code, department="Eng" if int(code[1:]) % 2 else "Ops", name=f"Name {code}")
    entries = [(code, late) for code, parts in TOTALS.items() for late in parts]
    random.Random(5).shuffle(entries)
    for i, (code, late) in enumerate(entries):
        log(adb, code, f"2026-07-{i + 1:02d}", late=late)  # a different date for every record
    return adb


# --------------------------------------------------------------------------- ranking with ties
def test_competition_ranking_shares_ties_and_skips_the_next_rank(client, board):
    assert ranks(get(client)) == EXPECTED  # 1, 2, 2, 4, 5, 5, 5, 8: never 1, 2, 3, 4 (the sort-and-index trap)


def test_hand_computed_response_fields(client, board):
    body = get(client, limit=1).json()
    assert body == {"month": "2026-07", "items": [{"rank": 1, "emp_code": "E1", "name": "Name E1", "department": "Eng", "total_late_minutes": 120, "late_count": 2}]}


@pytest.mark.parametrize("limit, expected_codes", [
    (1, ["E1"]),
    (2, ["E1", "E2", "E3"]),                      # both employees tied at rank 2 are returned: the limit applies to the RANK
    (3, ["E1", "E2", "E3"]),                      # there is no rank 3
    (4, ["E1", "E2", "E3", "E4"]),
    (5, ["E1", "E2", "E3", "E4", "E5", "E6", "E7"]),  # all three tied at rank 5
    (7, ["E1", "E2", "E3", "E4", "E5", "E6", "E7"]),
    (8, ["E1", "E2", "E3", "E4", "E5", "E6", "E7", "E8"]),
    (50, ["E1", "E2", "E3", "E4", "E5", "E6", "E7", "E8"]),
])
def test_limit_applies_to_the_rank_after_ranking(client, board, limit, expected_codes):
    assert [i["emp_code"] for i in get(client, limit=limit).json()["items"]] == expected_codes


def test_default_limit_is_ten(client, adb):
    for i in range(1, 13):
        employee(adb, f"E{i:02d}")
        log(adb, f"E{i:02d}", "2026-07-06", late=i * 10)  # 12 distinct totals
    assert len(get(client).json()["items"]) == 10
    assert len(get(client, limit=12).json()["items"]) == 12


def test_ties_are_ordered_by_emp_code_ascending(client, board):
    assert [i["emp_code"] for i in get(client).json()["items"] if i["rank"] == 5] == ["E5", "E6", "E7"]


def test_a_tie_at_the_top_gives_rank_1_1_3(client, adb):
    for code, late in [("B", 90), ("A", 90), ("C", 40), ("D", 10)]:
        employee(adb, code)
        log(adb, code, "2026-07-06", late=late)
    assert ranks(get(client)) == [(1, "A", 90), (1, "B", 90), (3, "C", 40), (4, "D", 10)]
    assert [i["emp_code"] for i in get(client, limit=1).json()["items"]] == ["A", "B"]


def test_everyone_tied_shares_rank_1(client, adb):
    for code in "ABC":
        employee(adb, code)
        log(adb, code, "2026-07-06", late=15)
    assert [i["rank"] for i in get(client).json()["items"]] == [1, 1, 1]


# --------------------------------------------------------------------------- what counts
def test_only_employees_with_a_positive_total_appear(client, adb):
    employee(adb, "A")
    employee(adb, "B")
    employee(adb, "C")
    log(adb, "A", "2026-07-06", late=5)
    log(adb, "B", "2026-07-06", late=0)
    adb.attendance_logs.insert_one({"emp_code": "C", "date": "2026-07-06", "status": "PRESENT"})  # legacy: no late_minutes
    assert ranks(get(client)) == [(1, "A", 5)]
    assert get(client, month="2026-01").json()["items"] == []


def test_late_count_counts_late_records_and_all_days_and_statuses_contribute(client, adb):
    employee(adb, "A")
    log(adb, "A", "2026-07-04", late=10)                 # Saturday
    log(adb, "A", "2026-07-05", "WFH", late=5)           # Sunday
    log(adb, "A", "2026-07-06", "ON_DUTY", late=1)
    log(adb, "A", "2026-07-07", late=0)                  # on time: not a late record
    item = get(client).json()["items"][0]
    assert (item["total_late_minutes"], item["late_count"]) == (16, 3)


@pytest.mark.parametrize("date, counted", [("2026-06-30", False), ("2026-07-01", True), ("2026-07-31", True), ("2026-08-01", False)])
def test_month_boundaries(client, adb, date, counted):
    employee(adb, "A")
    log(adb, "A", date, late=7)
    assert (get(client).json()["items"] != []) == counted


def test_logs_of_employees_that_do_not_exist_are_ignored_and_do_not_shift_ranks(client, board):
    log(board, "GHOST", "2026-07-20", late=9999)  # would be rank 1 if counted
    log(board, "GHOST2", "2026-07-21", late=110)
    assert ranks(get(client)) == EXPECTED


def test_total_is_summed_over_the_whole_month(client, adb):
    employee(adb, "A")
    for d in range(1, 29):
        log(adb, "A", f"2026-07-{d:02d}", late=d)
    assert get(client).json()["items"][0]["total_late_minutes"] == sum(range(1, 29))


# --------------------------------------------------------------------------- department filter
def test_department_filter_ranks_inside_the_department_only(client, board):
    # Eng = odd codes: E1 120, E3 100, E5 50, E7 50.  Ops = even codes: E2 100, E4 80, E6 50, E8 10.
    assert ranks(get(client, department="Eng")) == [(1, "E1", 120), (2, "E3", 100), (3, "E5", 50), (3, "E7", 50)]
    assert ranks(get(client, department="Ops")) == [(1, "E2", 100), (2, "E4", 80), (3, "E6", 50), (4, "E8", 10)]  # E2 is globally rank 2, here 1


def test_department_filter_and_limit_combine_on_the_departments_ranks(client, board):
    assert [i["emp_code"] for i in get(client, department="Eng", limit=3).json()["items"]] == ["E1", "E3", "E5", "E7"]  # E5/E7 tie at rank 3
    assert [i["emp_code"] for i in get(client, department="Eng", limit=2).json()["items"]] == ["E1", "E3"]


def test_department_filter_is_exact_case_sensitive_and_unknown_gives_empty(client, board):
    assert get(client, department="eng").json()["items"] == []
    assert get(client, department="Nope").json() == {"month": "2026-07", "items": []}
    assert get(client, department="").json()["items"] == []


# --------------------------------------------------------------------------- validation and shape
@pytest.mark.parametrize("limit", [0, -1, 51, 1000, "abc", "1.5", ""])
def test_invalid_limit_is_422(client, board, limit):
    r = get(client, limit=limit)
    assert r.status_code == 422 and isinstance(r.json()["detail"], list)


@pytest.mark.parametrize("limit", [1, 50])
def test_limit_boundaries_are_accepted(client, board, limit):
    assert get(client, limit=limit).status_code == 200


@pytest.mark.parametrize("month", ["2026-13", "2026-7", "garbage", "", "2026-07-01", "٢٠٢٦-٠٧", "2026-07\n"])
def test_invalid_month_is_422(client, board, month):
    assert get(client, month).status_code == 422


def test_missing_month_is_422(client, board):
    assert client.get("/analytics/leaderboard/late").status_code == 422


def test_empty_database(client, adb):
    assert get(client).json() == {"month": "2026-07", "items": []}


def test_response_shape_and_types(client, board):
    body = get(client).json()
    assert set(body) == {"month", "items"}
    for item in body["items"]:
        assert set(item) == ITEM_KEYS and "_id" not in item and "emp" not in item
        assert all(type(item[f]) is int for f in ("rank", "total_late_minutes", "late_count"))


def test_pipeline_ranks_with_a_window_function_before_applying_the_limit():
    pipeline = m.late_leaderboard_pipeline("2026-07", 5, "Eng")
    names = [next(iter(stage)) for stage in pipeline]
    assert names == ["$match", "$group", "$lookup", "$unwind", "$match", "$setWindowFields", "$match", "$sort", "$project"]
    assert pipeline[0]["$match"] == {"date": {"$gte": "2026-07-01", "$lt": "2026-08-01"}, "late_minutes": {"$gt": 0}}  # (date, emp_code) index
    assert pipeline[4] == {"$match": {"emp.department": "Eng"}}  # the department filter comes BEFORE ranking
    assert pipeline[5]["$setWindowFields"] == {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}
    assert pipeline[6] == {"$match": {"rank": {"$lte": 5}}}  # the limit is a condition on the rank, not a $limit stage
    assert "$limit" not in names and "$skip" not in names
    assert [next(iter(s)) for s in m.late_leaderboard_pipeline("2026-07", 5)].count("$match") == 2  # no department stage without a filter


# --------------------------------------------------------------------------- differential: endpoint vs independent oracle
def random_board(seed):
    rnd = random.Random(seed)
    month = rnd.choice(["2026-07", "2026-02", "2024-02", "2026-12"])
    year, mon = map(int, month.split("-"))
    first = datetime(year, mon, 1)
    employees_ = [{"emp_code": f"EMP{i:04d}", "name": f"N{i}", "email": f"e{i}@x.com", "department": rnd.choice(["Eng", "Ops", "HR"]),
                   "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01", "created_at": datetime(2026, 1, 5, tzinfo=UTC)} for i in range(1, 21)]
    logs = []
    for e in employees_ + [{"emp_code": "EMP9998"}, {"emp_code": "EMP9999"}]:
        for offset in range(-3, 35):
            if rnd.random() < 0.35:
                day = (first + timedelta(days=offset)).strftime("%Y-%m-%d")
                doc = {"emp_code": e["emp_code"], "date": day, "status": rnd.choice(["PRESENT", "PRESENT", "WFH", "ON_DUTY", "LEAVE", "ABSENT"])}
                if rnd.random() > 0.1:
                    doc["late_minutes"] = rnd.choice([0, 0, 0, 10, 10, 20, 30, 45])  # small value set: ties everywhere
                logs.append(doc)
    return month, employees_, logs


@pytest.mark.parametrize("seed", range(30))
def test_endpoint_matches_the_independent_oracle_on_random_data(client, adb, seed):
    month, employees_, logs = random_board(seed)
    adb.employees.insert_many([dict(e) for e in employees_])
    adb.attendance_logs.insert_many([dict(l) for l in logs])
    for limit in (1, 2, 3, 5, 10, 50):
        assert get(client, month, limit=limit).json() == oracle_leaderboard(employees_, logs, month, limit), (seed, limit)
    for department in ("Eng", "Ops", "HR"):
        for limit in (2, 10):
            assert get(client, month, limit=limit, department=department).json() == oracle_leaderboard(employees_, logs, month, limit, department), (seed, department, limit)
