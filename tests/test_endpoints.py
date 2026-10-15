"""Phase 3 tests: the five repaired endpoints (health, POST/GET /employees, POST /attendance/punch-in, GET /attendance)."""
import random
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import app.main as m

UTC, IST = timezone.utc, m.IST


def ms(iso: str) -> int:
    return m.dt_to_ms(datetime.fromisoformat(iso.replace("Z", "+00:00")))


@pytest.fixture
def client(db):
    with TestClient(m.app) as c:  # entering the context runs the startup hook, which creates the indexes
        yield c


def employee_payload(code="EMP0001", **over):
    return {"emp_code": code, "name": "Asha Rao", "email": "asha@example.com", "department": "Engineering",
            "joined_on": "2026-01-05", **over}


def seed_employee(db, code, department="Engineering", shift=("09:30", "18:30"), joined_on="2026-01-05"):
    db.employees.insert_one({"emp_code": code, "name": f"Emp {code}", "email": f"{code.lower()}@example.com",
                             "department": department, "shift_start": shift[0], "shift_end": shift[1],
                             "joined_on": joined_on, "created_at": datetime(2026, 1, 5, 4, 30, tzinfo=UTC)})


def seed_log(db, code, date, status="PRESENT", **extra):
    doc = {"emp_code": code, "date": date, "status": status, "punch_in": None, "punch_out": None, "work_hours": None,
           "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []}
    doc.update(extra)
    db.attendance_logs.insert_one(doc)


def assert_validation_error(resp):
    assert resp.status_code == 422, resp.text
    assert isinstance(resp.json()["detail"], list)  # FastAPI's standard body, as the contract's ValidationError says


# ================================================================ GET /health
def test_health_ok_when_mongodb_answers(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_health_is_503_with_detail_when_mongodb_is_down(monkeypatch):
    from pymongo import MongoClient

    monkeypatch.setattr(m, "db", MongoClient("mongodb://localhost:1", tz_aware=True, serverSelectionTimeoutMS=300)["x"])
    r = TestClient(m.app).get("/health")  # no context manager: skip the startup hook (it would refuse to start)
    assert r.status_code == 503 and isinstance(r.json()["detail"], str)


def test_connectivity_loss_during_a_request_is_503_not_500(monkeypatch):
    from pymongo import MongoClient

    monkeypatch.setattr(m, "db", MongoClient("mongodb://localhost:1", tz_aware=True, serverSelectionTimeoutMS=300)["x"])
    r = TestClient(m.app).get("/employees")
    assert r.status_code == 503 and r.json() == {"detail": "MongoDB is not reachable"}


# ================================================================ POST /employees
def test_create_employee_201_shape_defaults_and_server_created_at(client, db):
    before = m.dt_to_ms(m.utc_now())
    r = client.post("/employees", json=employee_payload())
    after = m.dt_to_ms(m.utc_now())
    assert r.status_code == 201
    body = r.json()
    assert set(body) == {"emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on", "created_at"}
    assert body["shift_start"] == "09:30" and body["shift_end"] == "18:30"  # contract defaults
    assert isinstance(body["created_at"], int) and before - 1 <= body["created_at"] <= after + 1
    stored = db.employees.find_one({"emp_code": "EMP0001"})
    assert stored["created_at"].tzinfo is not None and m.dt_to_ms(stored["created_at"]) == body["created_at"]
    assert "_id" not in body and "id" not in body


def test_create_employee_ignores_client_supplied_created_at_and_unknown_fields(client):
    r = client.post("/employees", json=employee_payload(created_at=5, _id="x", id="y", late_minutes=9, extra="z"))
    assert r.status_code == 201
    assert r.json()["created_at"] > 1_000_000_000_000 and "extra" not in r.json() and "late_minutes" not in r.json()


@pytest.mark.parametrize("override", [
    {"emp_code": "abc"}, {"emp_code": "EMP123"}, {"emp_code": "EMP1234567"}, {"emp_code": "emp0001"},
    {"emp_code": "EMP00١١"}, {"emp_code": "EMP0001\n"}, {"emp_code": " EMP0001"}, {"emp_code": 1234}, {"emp_code": None},
    {"name": ""}, {"name": "x" * 101}, {"name": None},
    {"email": "plain"}, {"email": "a@b"}, {"email": "a b@c.de"}, {"email": "@x.com"}, {"email": "a@@b.co"},
    {"email": "a" * 113 + "@x.co.in"}, {"email": 5},  # 121 chars: over maxLength 120
    {"department": ""}, {"department": "d" * 51},
    {"shift_start": "9:30"}, {"shift_start": "24:00"}, {"shift_start": "09:60"}, {"shift_start": "0٩:30"},
    {"shift_start": "09:30\n"}, {"shift_end": "7pm"}, {"shift_end": "23:60"},
    {"shift_start": "09:30", "shift_end": "09:30"},  # must differ
    {"shift_start": "18:30"},  # equals the default shift_end
    {"shift_end": "09:30"},  # equals the default shift_start
    {"joined_on": "2026-02-30"}, {"joined_on": "2026-7-1"}, {"joined_on": "garbage"}, {"joined_on": ""},
    {"joined_on": 20260101}, {"joined_on": "2026-07-06\n"},
])
def test_create_employee_rejects_invalid_fields_with_422(client, db, override):
    assert_validation_error(client.post("/employees", json=employee_payload(**override)))
    assert db.employees.count_documents({}) == 0


@pytest.mark.parametrize("field", ["emp_code", "name", "email", "department", "joined_on"])
def test_create_employee_requires_each_required_field(client, field):
    payload = employee_payload()
    del payload[field]
    assert_validation_error(client.post("/employees", json=payload))


def test_create_employee_rejects_non_object_bodies(client):
    assert_validation_error(client.post("/employees", json=[]))
    assert client.post("/employees", content=b"not json", headers={"content-type": "application/json"}).status_code == 422


@pytest.mark.parametrize("override", [
    {"emp_code": "EMP000001"}, {"emp_code": "EMP9999"}, {"name": "n" * 100}, {"department": "d" * 50},
    {"email": "a" * 112 + "@x.co.in"},  # 120 chars: exactly maxLength
    {"shift_start": "22:00", "shift_end": "06:00"},
    {"shift_start": "00:00", "shift_end": "23:59"}, {"joined_on": "2024-02-29"}, {"name": "   "},
])
def test_create_employee_accepts_boundary_values(client, override):
    assert client.post("/employees", json=employee_payload(**override)).status_code == 201


def test_duplicate_emp_code_is_409_and_stores_one_document(client, db):
    assert client.post("/employees", json=employee_payload()).status_code == 201
    r = client.post("/employees", json=employee_payload(name="Someone Else"))
    assert r.status_code == 409 and isinstance(r.json()["detail"], str)
    assert db.employees.count_documents({"emp_code": "EMP0001"}) == 1
    assert db.employees.find_one({"emp_code": "EMP0001"})["name"] == "Asha Rao"  # the first one won


def test_concurrent_creates_of_one_emp_code_yield_exactly_one_201(client, db):
    n = 20
    barrier = threading.Barrier(n)

    def go(_):
        barrier.wait()
        return client.post("/employees", json=employee_payload("EMP7777")).status_code

    with ThreadPoolExecutor(n) as pool:
        codes = list(pool.map(go, range(n)))
    assert sorted(codes) == [201] + [409] * (n - 1), codes
    assert db.employees.count_documents({"emp_code": "EMP7777"}) == 1


# ================================================================ GET /employees
@pytest.fixture
def twenty_five(db):
    codes = [f"EMP{i:04d}" for i in range(1, 26)]
    random.Random(3).shuffle(codes)  # inserted out of order: sorting must come from the query
    for code in codes:
        seed_employee(db, code, department="Engineering" if int(code[3:]) % 5 else "Sales")
    return sorted(codes)


def test_list_employees_default_first_page_sorted_by_emp_code(client, twenty_five):
    body = client.get("/employees").json()
    assert body["page"] == 1 and body["page_size"] == 20 and body["total"] == 25
    assert [e["emp_code"] for e in body["items"]] == twenty_five[:20]  # was: empty list (skip = page * page_size)


def test_list_employees_pagination_is_one_based_and_complete(client, twenty_five):
    seen = []
    for page, expected_len in [(1, 10), (2, 10), (3, 5), (4, 0)]:
        body = client.get(f"/employees?page={page}&page_size=10").json()
        assert len(body["items"]) == expected_len and body["total"] == 25
        seen += [e["emp_code"] for e in body["items"]]
    assert seen == twenty_five


def test_list_employees_total_counts_the_filtered_set(client, twenty_five):
    body = client.get("/employees?department=Sales").json()
    assert body["total"] == 5 and [e["emp_code"] for e in body["items"]] == ["EMP0005", "EMP0010", "EMP0015", "EMP0020", "EMP0025"]
    assert client.get("/employees?department=Engineering&page_size=100").json()["total"] == 20


def test_list_employees_department_match_is_exact_and_case_sensitive(client, twenty_five):
    assert client.get("/employees?department=sales").json()["total"] == 0
    assert client.get("/employees?department=Sale").json()["total"] == 0
    assert client.get("/employees?department=Nope").json() == {"items": [], "total": 0, "page": 1, "page_size": 20}


def test_list_employees_page_size_bounds_and_far_pages(client, twenty_five):
    assert len(client.get("/employees?page_size=100").json()["items"]) == 25
    assert client.get("/employees?page=999999999999999999999999").status_code in (200, 422)  # never a 500
    r = client.get(f"/employees?page={10**15}")
    assert r.status_code == 200 and r.json()["items"] == [] and r.json()["total"] == 25


@pytest.mark.parametrize("qs", ["page=0", "page=-1", "page=x", "page=1.5", "page_size=0", "page_size=101", "page_size=-5", "page_size=abc"])
def test_list_employees_rejects_bad_pagination_with_422(client, twenty_five, qs):
    assert_validation_error(client.get("/employees?" + qs))


def test_list_employees_items_follow_the_employee_schema(client, twenty_five):
    item = client.get("/employees").json()["items"][0]
    assert set(item) == {"emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on", "created_at"}
    assert isinstance(item["created_at"], int)


# ================================================================ POST /attendance/punch-in
@pytest.fixture
def staff(db):
    seed_employee(db, "EMP0001")  # 09:30-18:30
    seed_employee(db, "EMP0002")
    seed_employee(db, "EMP0005", department="Operations", shift=("22:00", "06:00"))  # overnight


RECORD_KEYS = {"emp_code", "date", "status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day", "history"}


def test_punch_in_201_record_shape_and_initial_values(client, staff, db):
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06T04:35:00Z")})
    assert r.status_code == 201
    body = r.json()
    assert set(body) == RECORD_KEYS  # in particular: no id, no _id
    assert body == {"emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT", "punch_in": ms("2026-07-06T04:35:00Z"),
                    "punch_out": None, "work_hours": None, "late_minutes": 35, "overtime_minutes": 0, "half_day": False, "history": []}
    stored = db.attendance_logs.find_one({"emp_code": "EMP0001"})
    assert stored["punch_in"] == datetime(2026, 7, 6, 4, 35, tzinfo=UTC) and stored["punch_in"].tzinfo is not None


def test_punch_in_without_punched_at_uses_the_current_time(client, staff):
    before = m.dt_to_ms(m.utc_now())
    body = client.post("/attendance/punch-in", json={"emp_code": "EMP0001"}).json()
    after = m.dt_to_ms(m.utc_now())
    assert before - 1000 <= body["punch_in"] <= after and body["punch_in"] % 1000 == 0
    assert body["date"] == datetime.now(IST).date().isoformat()


def test_punch_in_null_punched_at_is_treated_as_omitted(client, staff):
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": None}).status_code == 201


def test_punch_in_truncates_sub_second_and_late_uses_whole_seconds(client, staff):
    body = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06T04:10:01Z") + 999}).json()
    assert body["punch_in"] == ms("2026-07-06T04:10:01Z")  # 09:40:01 IST -> 10 minutes late (R2: strictly more than 10:00)
    assert body["late_minutes"] == 10
    body = client.post("/attendance/punch-in", json={"emp_code": "EMP0002", "punched_at": ms("2026-07-06T04:10:00Z") + 999}).json()
    assert body["late_minutes"] == 0  # 09:40:00.999 truncates to 09:40:00 -> within grace


@pytest.mark.parametrize("iso, expected_date, expected_late", [
    ("2026-07-10T18:29:59Z", "2026-07-10", 869),  # 23:59:59 IST = 14h29m59s after 09:30
    ("2026-07-10T18:30:00Z", "2026-07-11", 0),  # 00:00:00 IST: next IST day (a UTC-date implementation says 07-10)
    ("2026-07-10T20:00:00Z", "2026-07-11", 0),  # 01:30 IST
])
def test_punch_in_attendance_date_is_the_ist_date(client, staff, iso, expected_date, expected_late):
    body = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(iso)}).json()
    assert body["date"] == expected_date and body["late_minutes"] == expected_late


@pytest.mark.parametrize("iso, expected_date, expected_late", [
    ("2026-07-11T16:25:00Z", "2026-07-11", 0),  # 21:55 IST, before the 22:00 start
    ("2026-07-11T16:40:01Z", "2026-07-11", 10),  # 22:10:01 IST
    ("2026-07-11T19:00:00Z", "2026-07-11", 150),  # 00:30 IST on 07-12 belongs to the shift that began 07-11
    ("2026-07-11T23:59:59Z", "2026-07-11", 449),  # 05:29:59 IST on 07-12 = 7h29m59s after 22:00
    ("2026-07-12T00:29:59Z", "2026-07-11", 479),  # 05:59:59 IST: the last second still earlier than 06:00
    ("2026-07-12T00:30:00Z", "2026-07-12", 0),  # 06:00:00 IST exactly shift_end: its own day, hours before that shift starts
])
def test_punch_in_overnight_shift_uses_previous_day_before_shift_end(client, staff, iso, expected_date, expected_late):
    body = client.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms(iso)}).json()
    assert (body["date"], body["late_minutes"]) == (expected_date, expected_late)


@pytest.mark.parametrize("status", ["PRESENT", "WFH", "ON_DUTY"])
def test_punch_in_accepts_presence_statuses(client, staff, status):
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06T04:35:00Z"), "status": status})
    assert r.status_code == 201 and r.json()["status"] == status and r.json()["late_minutes"] == 35


@pytest.mark.parametrize("override", [
    {"punched_at": 1783312500000.0}, {"punched_at": "1783312500000"}, {"punched_at": 1783312500}, {"punched_at": 0},
    {"punched_at": -1}, {"punched_at": True}, {"punched_at": 99_999_999_999}, {"punched_at": 4_102_444_800_001},
    {"punched_at": []}, {"punched_at": 1783312500000.5},
    {"status": "ABSENT"}, {"status": "LEAVE"}, {"status": "BANANA"}, {"status": "present"}, {"status": ""}, {"status": 5}, {"status": None},
    {"emp_code": 5}, {"emp_code": None},
])
def test_punch_in_rejects_invalid_bodies_with_422_and_stores_nothing(client, staff, db, override):
    body = {"emp_code": "EMP0001", "punched_at": ms("2026-07-06T04:35:00Z"), **override}
    assert_validation_error(client.post("/attendance/punch-in", json=body))
    assert db.attendance_logs.count_documents({}) == 0


def test_punch_in_requires_emp_code_and_an_object_body(client, staff):
    assert_validation_error(client.post("/attendance/punch-in", json={"punched_at": ms("2026-07-06T04:35:00Z")}))
    assert_validation_error(client.post("/attendance/punch-in", json=[]))


def test_punch_in_unknown_employee_is_404_not_500(client, staff, db):
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP9999", "punched_at": ms("2026-07-06T04:35:00Z")})
    assert r.status_code == 404 and isinstance(r.json()["detail"], str)
    assert db.attendance_logs.count_documents({}) == 0


def test_punch_in_404_takes_precedence_over_409_for_unknown_employee(client, staff, db):
    seed_log(db, "EMP9999", "2026-07-06")  # an orphan log (the data model allows them)
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP9999", "punched_at": ms("2026-07-06T04:35:00Z")})
    assert r.status_code == 404


def test_punch_in_twice_for_the_same_attendance_date_is_409_and_keeps_the_first(client, staff, db):
    first = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06T04:35:00Z")})
    second = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06T09:00:00Z")})
    assert first.status_code == 201 and second.status_code == 409 and isinstance(second.json()["detail"], str)
    assert db.attendance_logs.count_documents({"emp_code": "EMP0001"}) == 1
    assert m.dt_to_ms(db.attendance_logs.find_one({"emp_code": "EMP0001"})["punch_in"]) == ms("2026-07-06T04:35:00Z")


def test_same_date_for_another_employee_or_another_date_for_the_same_employee_is_fine(client, staff):
    at = lambda iso, code: client.post("/attendance/punch-in", json={"emp_code": code, "punched_at": ms(iso)}).status_code
    assert at("2026-07-06T04:35:00Z", "EMP0001") == 201
    assert at("2026-07-06T04:35:00Z", "EMP0002") == 201
    assert at("2026-07-07T04:35:00Z", "EMP0001") == 201


def test_overnight_second_punch_in_after_midnight_conflicts_with_the_same_shift_record(client, staff):
    at = lambda iso: client.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms(iso)}).status_code
    assert at("2026-07-11T16:30:00Z") == 201  # 22:00 IST on 07-11 -> date 07-11
    assert at("2026-07-11T19:00:00Z") == 409  # 00:30 IST on 07-12 -> still the 07-11 shift


def test_concurrent_punch_ins_for_one_date_yield_exactly_one_201(client, staff, db):
    n = 20
    barrier = threading.Barrier(n)

    def go(i):
        barrier.wait()
        return client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06T04:35:00Z") + i * 1000}).status_code

    with ThreadPoolExecutor(n) as pool:
        codes = list(pool.map(go, range(n)))
    assert sorted(codes) == [201] + [409] * (n - 1), codes
    assert db.attendance_logs.count_documents({"emp_code": "EMP0001"}) == 1


# ================================================================ GET /attendance
@pytest.fixture
def big(db):
    """250 records: 25 days (2026-07-01..25) x 10 employees, inserted in shuffled order."""
    pairs = [(f"EMP{e:04d}", f"2026-07-{d:02d}") for e in range(1, 11) for d in range(1, 26)]
    random.Random(5).shuffle(pairs)
    for code, date in pairs:
        seed_log(db, code, date, status="WFH" if int(code[3:]) == 3 else "PRESENT")
    return sorted(pairs, key=lambda p: (-int(p[1].replace("-", "")), p[0]))


def test_list_attendance_sorted_by_date_desc_then_emp_code_asc(client, big):
    body = client.get("/attendance?page_size=100").json()
    got = [(i["emp_code"], i["date"]) for i in body["items"]]
    assert got == big[:100] and body["total"] == 250
    assert got[:3] == [("EMP0001", "2026-07-25"), ("EMP0002", "2026-07-25"), ("EMP0003", "2026-07-25")]  # tie-break


def test_list_attendance_pages_are_complete_and_disjoint(client, big):
    seen = []
    for page, size in [(1, 100), (2, 100), (3, 100), (4, 100)]:
        body = client.get(f"/attendance?page={page}&page_size=100").json()
        seen += [(i["emp_code"], i["date"]) for i in body["items"]]
        assert body["total"] == 250 and body["page"] == page
    assert seen == big and len(set(seen)) == 250


def test_list_attendance_is_paged_in_the_database_not_in_memory(client, big, db, monkeypatch):
    """Regression for the starter's list(find(q)) + Python sort/slice: only one page of documents may be fetched."""
    class CountingCursor:
        def __init__(self, cursor):
            self.cursor, self.yielded = cursor, 0

        def __getattr__(self, name):
            attr = getattr(self.cursor, name)
            if name in ("sort", "skip", "limit"):
                def chained(*args, **kwargs):
                    self.cursor = attr(*args, **kwargs)
                    return self
                return chained
            return attr

        def __iter__(self):
            for doc in self.cursor:
                self.yielded += 1
                yield doc

    created = []

    class Spy:
        def __init__(self, collection):
            self.collection = collection

        def find(self, *args, **kwargs):
            created.append(CountingCursor(self.collection.find(*args, **kwargs)))
            return created[-1]

        def __getattr__(self, name):
            return getattr(self.collection, name)

    class DbSpy:
        def __init__(self, database):
            self.database = database

        @property
        def attendance_logs(self):
            return Spy(self.database.attendance_logs)

        def __getattr__(self, name):
            return getattr(self.database, name)

    monkeypatch.setattr(m, "db", DbSpy(db))
    body = client.get("/attendance?page=2&page_size=20").json()
    assert len(body["items"]) == 20 and body["total"] == 250
    assert sum(c.yielded for c in created) == 20  # not 250


def test_list_attendance_filters_are_applied_and_total_reflects_them(client, big):
    get = lambda qs: client.get("/attendance?page_size=100&" + qs).json()
    assert get("emp_code=EMP0003")["total"] == 25
    assert {i["emp_code"] for i in get("emp_code=EMP0003")["items"]} == {"EMP0003"}
    assert get("status=WFH")["total"] == 25 and get("status=PRESENT")["total"] == 225 and get("status=LEAVE")["total"] == 0
    assert get("date_from=2026-07-24")["total"] == 20  # 24th and 25th, 10 employees each
    assert get("date_to=2026-07-02")["total"] == 20
    assert get("date_from=2026-07-10&date_to=2026-07-12")["total"] == 30  # both ends inclusive
    assert get("date_from=2026-07-10&date_to=2026-07-10")["total"] == 10
    assert get("emp_code=EMP0003&status=WFH&date_from=2026-07-20&date_to=2026-07-25")["total"] == 6
    assert get("emp_code=EMP0003&status=PRESENT") == {"items": [], "total": 0, "page": 1, "page_size": 100}
    assert get("emp_code=NOPE")["total"] == 0


@pytest.mark.parametrize("qs", [
    "date_from=2026-07-10&date_to=2026-07-01", "date_from=garbage", "date_to=garbage", "date_from=2026-02-30", "date_to=2026-7-1",
    "date_from=٢٠٢٦-٠٧-٠٦", "date_from=2026-07-06%0A", "date_from=2026-07", "date_from=",
    "status=BOGUS", "status=present", "status=", "page=0", "page=-3", "page=x", "page_size=0", "page_size=101", "page_size=100000",
])
def test_list_attendance_rejects_invalid_queries_with_422(client, big, qs):
    assert_validation_error(client.get("/attendance?" + qs))


def test_list_attendance_equal_from_and_to_is_valid(client, big):
    assert client.get("/attendance?date_from=2026-07-05&date_to=2026-07-05").status_code == 200


def test_list_attendance_serializes_ids_dates_legacy_and_history(client, db):
    seed_log(db, "EMP0001", "2026-07-07", punch_in=datetime(2026, 7, 7, 3, 58, tzinfo=UTC), punch_out=datetime(2026, 7, 7, 13, 0, tzinfo=UTC),
             work_hours=9.03, history=[{"at": datetime(2026, 7, 8, 5, 0, tzinfo=UTC), "by": "hr.admin", "reason": "biometric glitch",
                                        "changes": {"punch_in": {"from": datetime(2026, 7, 7, 4, 35, tzinfo=UTC), "to": datetime(2026, 7, 7, 3, 58, tzinfo=UTC)},
                                                    "late_minutes": {"from": 35, "to": 0}}}])
    db.attendance_logs.insert_one({"emp_code": "EMP0002", "date": "2026-07-14", "status": "PRESENT",  # legacy: no half_day / history
                                   "punch_in": datetime(2026, 7, 14, 4, 2, tzinfo=UTC), "punch_out": datetime(2026, 7, 14, 13, 1, tzinfo=UTC),
                                   "work_hours": 8.98, "late_minutes": 0, "overtime_minutes": 0})
    items = {i["emp_code"]: i for i in client.get("/attendance").json()["items"]}
    assert all(set(i) == RECORD_KEYS for i in items.values())  # no id / _id, no missing keys
    full = items["EMP0001"]
    assert full["punch_in"] == ms("2026-07-07T03:58:00Z") and full["punch_out"] == ms("2026-07-07T13:00:00Z")
    assert full["history"][0]["at"] == ms("2026-07-08T05:00:00Z")
    assert full["history"][0]["changes"]["punch_in"] == {"from": ms("2026-07-07T04:35:00Z"), "to": ms("2026-07-07T03:58:00Z")}
    assert full["history"][0]["changes"]["late_minutes"] == {"from": 35, "to": 0}
    legacy = items["EMP0002"]
    assert legacy["half_day"] is False and legacy["history"] == [] and legacy["work_hours"] == 8.98


def test_end_to_end_punch_in_then_list_roundtrip(client, staff):
    posted = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06T04:35:00Z")}).json()
    listed = client.get("/attendance?emp_code=EMP0001").json()
    assert listed["total"] == 1 and listed["items"][0] == posted
