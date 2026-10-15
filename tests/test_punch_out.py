"""Phase 4A tests: POST /attendance/punch-out.

All tests run on mongomock by default. Tests marked `real_mongo` (and every test when TEST_MONGO_URI is set) run on a
real MongoDB; the atomicity guarantee itself can only be proven there.
"""
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import app.main as m

UTC = timezone.utc
DAY = ("09:30", "18:30")
NIGHT = ("22:00", "06:00")
RECORD_KEYS = {"emp_code", "date", "status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day", "history"}
# Monday 2026-07-06, day shift 09:30-18:30 IST: shift start = 04:00Z, shift end = 13:00Z


def dt(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def ms(iso):
    return m.dt_to_ms(dt(iso))


@pytest.fixture
def client(db):
    with TestClient(m.app) as c:
        yield c


def seed_employee(db, code="EMP0001", shift=DAY, **extra):
    db.employees.insert_one({"emp_code": code, "name": f"Emp {code}", "email": f"{code.lower()}@example.com", "department": "Engineering",
                             "shift_start": shift[0], "shift_end": shift[1], "joined_on": "2026-01-05",
                             "created_at": datetime(2026, 1, 5, 4, 30, tzinfo=UTC), **extra})


def seed_open(db, code="EMP0001", date="2026-07-06", punch_in="2026-07-06T04:00:00Z", status="PRESENT", late=0, **extra):
    doc = {"emp_code": code, "date": date, "status": status, "punch_in": dt(punch_in) if isinstance(punch_in, str) else punch_in,
           "punch_out": None, "work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False, "history": []}
    doc.update(extra)
    db.attendance_logs.insert_one(doc)
    return doc


def out(client, code="EMP0001", at=None, **extra):
    body = {"emp_code": code, **extra}
    if at is not None:
        body["punched_at"] = ms(at) if isinstance(at, str) else at
    return client.post("/attendance/punch-out", json=body)


def stored(db, code="EMP0001", date="2026-07-06"):
    return db.attendance_logs.find_one({"emp_code": code, "date": date}, {"_id": 0})


def assert_422(resp):
    assert resp.status_code == 422, resp.text
    assert isinstance(resp.json()["detail"], list)


# ====================================================== 1. normal shift and serialization
def test_normal_shift_returns_the_contract_record(client, db):
    seed_employee(db)
    seed_open(db, punch_in="2026-07-06T04:35:00Z", late=35)  # contract sample 2: in 10:05 IST, out 19:10 IST
    r = out(client, at="2026-07-06T13:40:00Z")
    assert r.status_code == 200
    assert r.json() == {"emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT", "punch_in": ms("2026-07-06T04:35:00Z"),
                        "punch_out": ms("2026-07-06T13:40:00Z"), "work_hours": 9.08, "late_minutes": 35, "overtime_minutes": 40,
                        "half_day": False, "history": []}
    assert set(r.json()) == RECORD_KEYS  # no id / _id


def test_sample_1_normal_day_no_overtime(client, db):
    seed_employee(db)
    seed_open(db, punch_in="2026-07-06T03:58:00Z")
    body = out(client, at="2026-07-06T13:05:00Z").json()  # 18:35 IST: 5 minutes after the end, below the 30-minute minimum
    assert (body["work_hours"], body["overtime_minutes"], body["half_day"], body["late_minutes"]) == (9.12, 0, False, 0)


def test_response_equals_what_a_later_get_returns(client, db):
    seed_employee(db)
    seed_open(db, punch_in="2026-07-06T04:35:00Z", late=35)
    posted = out(client, at="2026-07-06T13:40:00Z").json()
    assert client.get("/attendance?emp_code=EMP0001").json()["items"] == [posted]


# ====================================================== 2. overtime threshold (R3)
@pytest.mark.parametrize("at, expected", [
    ("2026-07-06T12:00:00Z", 0),   # 17:30 IST, left early
    ("2026-07-06T12:59:59Z", 0),   # 18:29:59, a second before the end
    ("2026-07-06T13:00:00Z", 0),   # exactly 18:30
    ("2026-07-06T13:29:59Z", 0),   # 18:59:59 -> 29 whole minutes: below the minimum
    ("2026-07-06T13:30:00Z", 30),  # 19:00:00 -> exactly 30: counted
    ("2026-07-06T13:30:59Z", 30),  # 19:00:59 -> floor to 30
    ("2026-07-06T13:31:00Z", 31),
    ("2026-07-06T13:40:00Z", 40),
])
def test_overtime_threshold_day_shift(client, db, at, expected):
    seed_employee(db)
    seed_open(db)
    assert out(client, at=at).json()["overtime_minutes"] == expected


# ====================================================== 3. work hours half-up and the half-day boundary (R4, R5)
@pytest.mark.parametrize("seconds, work_hours, half_day", [
    (1, 0.0, True),
    (54, 0.02, True),            # 0.015 h: float round() gives 0.01, half-up gives 0.02
    (12600, 3.5, True),
    (16181, 4.49, True),         # 4.4947 h
    (16182, 4.5, False),         # 4.495 h is an exact tie: half-up -> 4.50 -> NOT a half day
    (16200, 4.5, False),
    (28962, 8.05, False),        # 8h02m42s = 8.045 h exact tie
    (28998, 8.06, False),        # 8h03m18s = 8.055 h exact tie
    (86400, 24.0, False),        # exactly 24 h is still allowed
])
def test_work_hours_half_up_and_half_day(client, db, seconds, work_hours, half_day):
    seed_employee(db)
    seed_open(db)  # punch-in 04:00:00Z
    body = out(client, at=ms("2026-07-06T04:00:00Z") + seconds * 1000).json()
    assert (body["work_hours"], body["half_day"]) == (work_hours, half_day)


def test_punched_at_is_truncated_to_whole_seconds(client, db):
    seed_employee(db)
    seed_open(db, punch_in="2026-07-06T03:58:00Z")
    body = out(client, at=ms("2026-07-06T13:05:00Z") + 999).json()
    assert body["punch_out"] == ms("2026-07-06T13:05:00Z") and body["work_hours"] == 9.12
    assert stored(db)["punch_out"] == dt("2026-07-06T13:05:00Z")  # the truncated value is what is stored


def test_stored_punch_in_is_truncated_before_the_comparison(client, db):
    seed_employee(db)
    seed_open(db, punch_in=datetime(2026, 7, 6, 4, 0, 0, 900000, tzinfo=UTC))  # 04:00:00.900
    r = out(client, at="2026-07-06T04:00:01Z")  # one second after the TRUNCATED punch-in
    assert r.status_code == 200 and r.json()["work_hours"] == 0.0


# ====================================================== 4. overnight shifts (R1, R3)
def test_supplied_overnight_sample_gives_40_minutes_overtime(client, db):
    seed_employee(db, "EMP0005", NIGHT)
    seed_open(db, "EMP0005", "2026-07-06", "2026-07-06T16:25:00Z")  # 21:55 IST
    r = out(client, "EMP0005", "2026-07-07T01:10:00Z")  # 06:40 IST next day
    assert r.status_code == 200
    assert r.json() == {"emp_code": "EMP0005", "date": "2026-07-06", "status": "PRESENT", "punch_in": ms("2026-07-06T16:25:00Z"),
                        "punch_out": ms("2026-07-07T01:10:00Z"), "work_hours": 8.75, "late_minutes": 0, "overtime_minutes": 40,
                        "half_day": False, "history": []}


@pytest.mark.parametrize("at, expected", [
    ("2026-07-06T23:00:00Z", 0),   # 04:30 IST: left before the shift end
    ("2026-07-07T00:30:00Z", 0),   # exactly 06:00 IST
    ("2026-07-07T00:59:59Z", 0),   # 06:29:59 -> 29 minutes
    ("2026-07-07T01:00:00Z", 30),  # 06:30:00
    ("2026-07-07T01:10:00Z", 40),
])
def test_overnight_overtime_is_measured_from_the_next_calendar_days_shift_end(client, db, at, expected):
    seed_employee(db, "EMP0005", NIGHT)
    seed_open(db, "EMP0005", "2026-07-06", "2026-07-06T16:25:00Z")
    assert out(client, "EMP0005", at).json()["overtime_minutes"] == expected


def test_overnight_record_punched_in_after_midnight_belongs_to_the_previous_day(client, db):
    seed_employee(db, "EMP0005", NIGHT)
    seed_open(db, "EMP0005", "2026-07-06", "2026-07-06T19:00:00Z", late=150)  # 00:30 IST on 07-07
    body = out(client, "EMP0005", "2026-07-07T01:10:00Z").json()
    assert body["date"] == "2026-07-06" and body["overtime_minutes"] == 40 and body["late_minutes"] == 150


def test_day_shift_punched_out_after_midnight_is_measured_from_the_records_own_day(client, db):
    seed_employee(db)
    seed_open(db)
    body = out(client, at="2026-07-06T20:00:00Z").json()  # 01:30 IST on 07-07, 16 h later
    assert body["date"] == "2026-07-06" and body["work_hours"] == 16.0
    assert body["overtime_minutes"] == 420  # 18:30 -> 01:30 = 7 h; anchoring on the punch-out's date would say 0


# ====================================================== 5. missing employees and missing attendance records
def test_unknown_employee_is_404(client, db):
    seed_employee(db)
    seed_open(db)
    r = out(client, "EMP9999", "2026-07-06T13:00:00Z")
    assert r.status_code == 404 and isinstance(r.json()["detail"], str)
    assert stored(db)["punch_out"] is None


def test_employee_without_any_record_is_404(client, db):
    seed_employee(db)
    r = out(client, at="2026-07-06T13:00:00Z")
    assert r.status_code == 404 and isinstance(r.json()["detail"], str)


def test_absent_and_leave_records_are_never_punched_out(client, db):
    seed_employee(db)
    db.attendance_logs.insert_many([
        {"emp_code": "EMP0001", "date": "2026-07-06", "status": "ABSENT", "punch_in": None, "punch_out": None},
        {"emp_code": "EMP0001", "date": "2026-07-07", "status": "LEAVE"},  # punch_in key missing entirely
    ])
    assert out(client, at="2026-07-08T13:00:00Z").status_code == 404
    assert db.attendance_logs.count_documents({"punch_out": {"$ne": None}}) == 0


def test_no_punch_in_at_or_before_punched_at_is_404(client, db):
    """Contract lookup: 'most recent record whose punch_in is at or before punched_at'. A time before the only
    punch-in finds nothing, which the contract calls 'no punch-in found' (404)."""
    seed_employee(db)
    seed_open(db)
    r = out(client, at="2026-07-06T03:59:59Z")
    assert r.status_code == 404
    assert stored(db)["punch_out"] is None


def test_unknown_employee_with_an_orphan_log_is_404_not_409(client, db):
    seed_open(db, "EMP9999")
    assert out(client, "EMP9999", "2026-07-06T13:00:00Z").status_code == 404


def test_validation_runs_before_the_employee_lookup(client, db):
    assert_422(out(client, "EMP9999", at=1783312500000.0))


# ====================================================== 6. punched_at equal to (or not later than) punch_in
def test_punch_out_equal_to_punch_in_is_422_and_leaves_the_record_open(client, db):
    seed_employee(db)
    seed_open(db)
    assert_422(out(client, at="2026-07-06T04:00:00Z"))
    assert stored(db)["punch_out"] is None and stored(db)["work_hours"] is None


def test_punch_out_that_truncates_to_the_punch_in_second_is_422(client, db):
    seed_employee(db)
    seed_open(db)
    assert_422(out(client, at=ms("2026-07-06T04:00:00Z") + 999))
    assert stored(db)["punch_out"] is None


def test_422_detail_names_the_field(client, db):
    seed_employee(db)
    seed_open(db)
    detail = out(client, at="2026-07-06T04:00:00Z").json()["detail"][0]
    assert detail["loc"] == ["body", "punched_at"] and "after punch_in" in detail["msg"]


# ====================================================== 7. more than 24 hours after punch_in
def test_exactly_24_hours_is_allowed(client, db):
    seed_employee(db)
    seed_open(db)
    r = out(client, at="2026-07-07T04:00:00Z")
    assert r.status_code == 200 and r.json()["work_hours"] == 24.0


def test_24_hours_and_999_ms_truncates_to_exactly_24_hours_and_is_allowed(client, db):
    seed_employee(db)
    seed_open(db)
    assert out(client, at=ms("2026-07-07T04:00:00Z") + 999).status_code == 200


@pytest.mark.parametrize("at", ["2026-07-07T04:00:01Z", "2026-07-07T13:00:00Z", "2026-07-08T04:00:00Z"])
def test_more_than_24_hours_is_422_and_leaves_the_record_open(client, db, at):
    seed_employee(db)
    seed_open(db)
    r = out(client, at=at)
    assert_422(r)
    assert "24 hours" in r.json()["detail"][0]["msg"]
    assert stored(db)["punch_out"] is None and stored(db)["work_hours"] is None


# ====================================================== 8a. 409 and record selection
def test_second_punch_out_is_409_and_never_overwrites_the_closed_record(client, db):
    seed_employee(db)
    seed_open(db)
    first = out(client, at="2026-07-06T13:40:00Z")
    before = stored(db)
    second = out(client, at="2026-07-06T15:00:00Z")
    assert first.status_code == 200 and second.status_code == 409 and isinstance(second.json()["detail"], str)
    assert stored(db) == before  # untouched: same punch_out, work_hours, overtime
    assert before["punch_out"] == dt("2026-07-06T13:40:00Z")


def test_seeded_closed_record_is_409(client, db):
    seed_employee(db)
    seed_open(db, punch_in="2026-07-06T03:58:00Z", punch_out=dt("2026-07-06T13:05:00Z"), work_hours=9.12)
    assert out(client, at="2026-07-06T14:00:00Z").status_code == 409


def test_409_takes_precedence_over_the_24_hour_422(client, db):
    seed_employee(db)
    seed_open(db, punch_out=dt("2026-07-06T13:00:00Z"), work_hours=9.0)
    assert out(client, at="2026-07-09T04:00:00Z").status_code == 409


def test_newest_record_is_closed_when_several_are_open(client, db):
    seed_employee(db)
    seed_open(db, date="2026-07-06", punch_in="2026-07-06T04:00:00Z")
    seed_open(db, date="2026-07-07", punch_in="2026-07-07T04:00:00Z")
    assert out(client, at="2026-07-07T13:00:00Z").status_code == 200
    assert stored(db, date="2026-07-07")["punch_out"] == dt("2026-07-07T13:00:00Z")
    assert stored(db, date="2026-07-06")["punch_out"] is None  # the older open record is left alone


def test_a_time_between_two_punch_ins_closes_the_older_record(client, db):
    seed_employee(db)
    seed_open(db, date="2026-07-06", punch_in="2026-07-06T04:00:00Z")
    seed_open(db, date="2026-07-07", punch_in="2026-07-07T04:00:00Z")
    assert out(client, at="2026-07-06T13:00:00Z").status_code == 200  # only the 07-06 punch-in is at or before this time
    assert stored(db, date="2026-07-06")["punch_out"] == dt("2026-07-06T13:00:00Z")
    assert stored(db, date="2026-07-07")["punch_out"] is None


def test_older_open_record_is_not_used_when_the_newest_is_already_closed(client, db):
    """Contract: the 'most recent' record is the one that is closed -> 409. No silent fallback to an older open one."""
    seed_employee(db)
    seed_open(db, date="2026-07-06", punch_in="2026-07-06T04:00:00Z")  # still open
    seed_open(db, date="2026-07-07", punch_in="2026-07-07T04:00:00Z", punch_out=dt("2026-07-07T13:00:00Z"), work_hours=9.0)
    assert out(client, at="2026-07-07T14:00:00Z").status_code == 409
    assert stored(db, date="2026-07-06")["punch_out"] is None


def test_older_closed_record_does_not_block_the_newest_open_one(client, db):
    seed_employee(db)
    seed_open(db, date="2026-07-06", punch_in="2026-07-06T04:00:00Z", punch_out=dt("2026-07-06T13:00:00Z"), work_hours=9.0)
    seed_open(db, date="2026-07-07", punch_in="2026-07-07T04:00:00Z")
    assert out(client, at="2026-07-07T13:00:00Z").status_code == 200
    assert stored(db, date="2026-07-06")["work_hours"] == 9.0  # untouched


def test_other_employees_and_dates_are_not_touched(client, db):
    seed_employee(db, "EMP0001")
    seed_employee(db, "EMP0002")
    seed_open(db, "EMP0001")
    seed_open(db, "EMP0002")
    assert out(client, "EMP0001", "2026-07-06T13:00:00Z").status_code == 200
    assert stored(db, "EMP0002")["punch_out"] is None


# ====================================================== 8b. concurrency (exactly one 200)
def test_twenty_simultaneous_punch_outs_yield_exactly_one_200(client, db):
    seed_employee(db)
    seed_open(db)
    n = 20
    barrier = threading.Barrier(n)

    def go(_):
        barrier.wait()
        return out(client, at="2026-07-06T13:40:00Z").status_code

    with ThreadPoolExecutor(n) as pool:
        codes = list(pool.map(go, range(n)))
    assert sorted(codes) == [200] + [409] * (n - 1), codes
    assert stored(db)["punch_out"] == dt("2026-07-06T13:40:00Z")


def _interleaving_db(real, barrier, hook=None):
    """Wrap attendance_logs.find_one so every request thread pauses right AFTER it has read the open record. This forces
    the dangerous interleaving (both requests saw the record open) regardless of mongomock's thread timing."""
    seen, calls = set(), []

    class Coll:
        def __init__(self, c):
            self.c = c

        def find_one(self, *args, **kwargs):
            doc = self.c.find_one(*args, **kwargs)
            calls.append(threading.get_ident())
            if hook:
                hook(self.c, doc, len(calls))
            if barrier is not None and threading.get_ident() not in seen:
                seen.add(threading.get_ident())
                try:
                    barrier.wait(timeout=5)
                except threading.BrokenBarrierError:
                    pass
            return doc

        def __getattr__(self, name):
            return getattr(self.c, name)

    class Db:
        @property
        def attendance_logs(self):
            return Coll(real.attendance_logs)

        def __getattr__(self, name):
            return getattr(real, name)

    return Db()


def test_two_requests_that_both_read_the_record_open_cannot_both_succeed(client, db, monkeypatch):
    seed_employee(db)
    seed_open(db)
    monkeypatch.setattr(m, "db", _interleaving_db(db, threading.Barrier(2)))
    times = ["2026-07-06T13:00:00Z", "2026-07-06T13:30:00Z"]
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda t: out(client, at=t), times))
    assert sorted(r.status_code for r in results) == [200, 409]
    winner = next(r for r in results if r.status_code == 200).json()
    record = stored(db)
    assert record["punch_out"] == dt("2026-07-06T13:00:00Z") or record["punch_out"] == dt("2026-07-06T13:30:00Z")
    assert ms(record["punch_out"].strftime("%Y-%m-%dT%H:%M:%SZ")) == winner["punch_out"]  # the stored time is the winner's
    assert record["work_hours"] == winner["work_hours"] and record["overtime_minutes"] == winner["overtime_minutes"]  # consistent


def test_a_correction_that_lands_between_read_and_write_is_not_overwritten_with_stale_numbers(client, db, monkeypatch):
    """Simulates a PATCH changing punch_in after punch-out has read the record: the conditional update must not match, and
    the retry must recompute from the corrected punch_in."""
    seed_employee(db)
    seed_open(db, punch_in="2026-07-06T04:35:00Z", late=35)

    def correct_punch_in(coll, doc, call_number):
        if call_number == 1:
            coll.update_one({"_id": doc["_id"]}, {"$set": {"punch_in": dt("2026-07-06T03:58:00Z"), "late_minutes": 0}})

    monkeypatch.setattr(m, "db", _interleaving_db(db, None, correct_punch_in))
    r = out(client, at="2026-07-06T13:05:00Z")
    assert r.status_code == 200
    assert r.json()["punch_in"] == ms("2026-07-06T03:58:00Z")
    assert r.json()["work_hours"] == 9.12  # from the corrected 03:58, not the stale 04:35 (which would say 8.5)
    assert stored(db)["late_minutes"] == 0


@pytest.mark.real_mongo
def test_real_mongodb_stress_exactly_one_winner_per_round(client, db):
    seed_employee(db)
    for round_no in range(30):
        date = (datetime(2026, 7, 1) + timedelta(days=round_no)).strftime("%Y-%m-%d")
        pin = f"{date}T04:00:00Z"
        seed_open(db, date=date, punch_in=pin)
        barrier = threading.Barrier(8)

        def go(i, pin=pin, barrier=barrier):
            barrier.wait()
            return out(client, at=ms(pin) + (3600 + i) * 1000).status_code

        with ThreadPoolExecutor(8) as pool:
            codes = list(pool.map(go, range(8)))
        assert sorted(codes) == [200] + [409] * 7, (round_no, codes)


# ====================================================== 9. BSON datetimes and epoch milliseconds
@pytest.mark.parametrize("offset_ms", [0, 1, 500, 999])
def test_stored_value_is_a_utc_aware_whole_second_datetime_and_response_is_epoch_ms(client, db, offset_ms):
    seed_employee(db)
    seed_open(db)
    at_ms = ms("2026-07-06T13:07:09Z") + offset_ms
    body = out(client, at=at_ms).json()
    value = stored(db)["punch_out"]
    assert value.tzinfo is not None and value.utcoffset() == timedelta(0)
    assert value == datetime(2026, 7, 6, 13, 7, 9, tzinfo=UTC) and value.microsecond == 0
    assert body["punch_out"] == ms("2026-07-06T13:07:09Z") == m.dt_to_ms(value) and isinstance(body["punch_out"], int)


def test_derived_values_are_stored_with_the_right_types(client, db):
    seed_employee(db)
    seed_open(db, punch_in="2026-07-06T04:35:00Z", late=35)
    out(client, at="2026-07-06T13:40:00Z")
    doc = stored(db)
    assert isinstance(doc["work_hours"], float) and isinstance(doc["overtime_minutes"], int) and doc["half_day"] is False
    assert type(doc["overtime_minutes"]) is int and type(doc["half_day"]) is bool


def test_a_naive_stored_punch_in_is_treated_as_utc(client, db):
    seed_employee(db)
    seed_open(db, punch_in=datetime(2026, 7, 6, 3, 58, 0))  # naive, as the old starter code wrote them
    body = out(client, at="2026-07-06T13:05:00Z").json()
    assert body["work_hours"] == 9.12 and body["punch_in"] == ms("2026-07-06T03:58:00Z")


# ====================================================== 10. legacy records and fields that are not written
def test_legacy_record_with_missing_fields_can_be_punched_out(client, db):
    seed_employee(db)
    db.attendance_logs.insert_one({"emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT", "punch_in": dt("2026-07-06T04:00:00Z")})
    r = out(client, at="2026-07-06T13:40:00Z")  # no punch_out / late / overtime / half_day / history keys at all
    assert r.status_code == 200 and set(r.json()) == RECORD_KEYS
    assert (r.json()["work_hours"], r.json()["overtime_minutes"], r.json()["half_day"]) == (9.67, 40, False)
    assert (r.json()["late_minutes"], r.json()["history"]) == (0, [])


def test_legacy_employee_without_shift_fields_uses_the_contract_defaults(client, db):
    db.employees.insert_one({"emp_code": "EMP0001", "name": "Old", "email": "o@example.com", "department": "Eng", "joined_on": "2025-01-01"})
    seed_open(db)
    assert out(client, at="2026-07-06T13:40:00Z").json()["overtime_minutes"] == 40  # 09:30-18:30 assumed


def test_punch_out_does_not_append_history_or_touch_existing_entries(client, db):
    seed_employee(db)
    entry = {"at": dt("2026-07-08T05:00:00Z"), "by": "hr.admin", "reason": "biometric glitch",
             "changes": {"punch_in": {"from": dt("2026-07-06T04:35:00Z"), "to": dt("2026-07-06T04:00:00Z")}}}
    seed_open(db, history=[entry])
    body = out(client, at="2026-07-06T13:40:00Z").json()
    assert stored(db)["history"] == [entry]  # still exactly one entry, unchanged
    assert len(body["history"]) == 1 and body["history"][0]["by"] == "hr.admin"
    seed_open(db, date="2026-07-07", punch_in="2026-07-07T04:00:00Z")
    out(client, at="2026-07-07T13:00:00Z")
    assert stored(db, date="2026-07-07")["history"] == []  # a normal punch-out writes no entry


def test_late_minutes_and_other_fields_are_not_rewritten(client, db):
    seed_employee(db)
    seed_open(db, late=7)  # deliberately not what R2 would compute (0): punch-out must not recompute it
    before = stored(db)
    out(client, at="2026-07-06T13:40:00Z")
    after = stored(db)
    changed = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
    assert changed == {"punch_out", "work_hours", "overtime_minutes"}  # half_day stays False; nothing else moves
    assert after["late_minutes"] == 7 and after["punch_in"] == before["punch_in"] and after["status"] == "PRESENT"


@pytest.mark.parametrize("status", ["PRESENT", "WFH", "ON_DUTY"])
def test_all_presence_statuses_can_be_punched_out_and_keep_their_status(client, db, status):
    seed_employee(db)
    seed_open(db, status=status)
    assert out(client, at="2026-07-06T13:40:00Z").json()["status"] == status


# ====================================================== defaults and request validation
def test_omitted_punched_at_uses_the_current_time(client, db):
    seed_employee(db)
    now = m.truncate_seconds(m.utc_now())
    seed_open(db, date=now.astimezone(m.IST).date().isoformat(), punch_in=now - timedelta(hours=2))
    before = m.dt_to_ms(m.utc_now())
    r = out(client)
    after = m.dt_to_ms(m.utc_now())
    assert r.status_code == 200
    assert before - 1000 <= r.json()["punch_out"] <= after and r.json()["punch_out"] % 1000 == 0
    assert 1.99 <= r.json()["work_hours"] <= 2.01


def test_null_punched_at_is_treated_as_omitted(client, db):
    seed_employee(db)
    now = m.truncate_seconds(m.utc_now())
    seed_open(db, date=now.astimezone(m.IST).date().isoformat(), punch_in=now - timedelta(hours=1))
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": None}).status_code == 200


def test_omitted_punched_at_with_a_future_punch_in_is_404(client, db):
    seed_employee(db)
    seed_open(db, punch_in=m.truncate_seconds(m.utc_now()) + timedelta(hours=1))
    assert out(client).status_code == 404


@pytest.mark.parametrize("bad", [1783312500000.0, "1783312500000", 1783312500, 0, -1, True, 99_999_999_999, 4_102_444_800_001, [], 1783312500000.5])
def test_invalid_punched_at_is_422_and_changes_nothing(client, db, bad):
    seed_employee(db)
    seed_open(db)
    assert_422(client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": bad}))
    assert stored(db)["punch_out"] is None


@pytest.mark.parametrize("body", [{}, {"emp_code": None}, {"emp_code": 5}, {"punched_at": 1783312500000}, [], "text"])
def test_invalid_bodies_are_422(client, db, body):
    seed_employee(db)
    seed_open(db)
    assert_422(client.post("/attendance/punch-out", json=body))


def test_derived_fields_sent_by_a_client_are_ignored(client, db):
    seed_employee(db)
    seed_open(db)
    body = out(client, at="2026-07-06T13:40:00Z", work_hours=99, overtime_minutes=999, half_day=True, late_minutes=500, history=[1]).json()
    assert (body["work_hours"], body["overtime_minutes"], body["half_day"], body["late_minutes"], body["history"]) == (9.67, 40, False, 0, [])
