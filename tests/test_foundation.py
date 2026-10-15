"""Phase 1 tests: shared helpers (time, rounding, rules R1-R5), validation types, serializers, indexes."""
import random
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, TypeAdapter, ValidationError
from pymongo.errors import DuplicateKeyError

import app.main as m

UTC, IST = timezone.utc, m.IST
DAY = ("09:30", "18:30")  # default shift
NIGHT = ("22:00", "06:00")  # overnight shift (sample employee EMP0005)


def ist(y, mo, d, h, mi, s=0, us=0):
    return datetime(y, mo, d, h, mi, s, us, tzinfo=IST)


def utc(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


# ---------------------------------------------------------------- time helpers
def test_ms_to_instant_truncates_to_whole_seconds_with_integer_math():
    assert m.ms_to_instant(1783312500999) == datetime(2026, 7, 6, 4, 35, 0, tzinfo=UTC)
    assert m.ms_to_instant(1783312500000).microsecond == 0
    assert m.ms_to_instant(1783312500000).tzinfo is not None


def test_dt_to_ms_roundtrip_and_naive_is_utc():
    dt = datetime(2026, 7, 6, 4, 35, 0, tzinfo=UTC)
    assert m.dt_to_ms(dt) == 1783312500000
    assert m.dt_to_ms(dt.replace(tzinfo=None)) == 1783312500000  # naive == UTC (how Mongo stores it)
    assert m.dt_to_ms(dt.astimezone(IST)) == 1783312500000  # same instant, other zone
    assert m.dt_to_ms(dt.replace(microsecond=123000)) == 1783312500123
    assert isinstance(m.dt_to_ms(dt), int)


def test_utc_now_is_aware_and_millisecond_precision():
    now = m.utc_now()
    assert now.tzinfo is not None and now.microsecond % 1000 == 0


@pytest.mark.parametrize("good", ["2026-07-06", "2024-02-29", "2026-12-31"])
def test_parse_date_accepts_real_dates(good):
    assert m.parse_date(good) == date.fromisoformat(good)


@pytest.mark.parametrize("bad", ["2026-02-30", "2025-02-29", "2026-7-1", "20260706", "2026-07-06T00:00", " 2026-07-06", "", "garbage", "0000-01-01",
                                 "2026-07-06\n", "2026-07-06 ", "\n2026-07-06",  # regression: '$' used to tolerate a trailing newline
                                 "٢٠٢٦-٠٧-٠٦", "２０２６-０７-０６", "2026-٠٧-06"])  # regression: \d used to accept non-ASCII digits
def test_parse_date_rejects_everything_else(bad):
    with pytest.raises(ValueError):
        m.parse_date(bad)


@pytest.mark.parametrize("bad", ["2026-13", "2026-00", "2026-7", "202607", "2026-07-01", "abcd-ef", "2026-07\n", "٢٠٢٦-07", "2026-٠٧"])
def test_check_month_rejects(bad):
    with pytest.raises(ValueError):
        m.check_month(bad)


def test_month_bounds_including_december_and_leap_february():
    assert m.month_bounds("2026-07") == ("2026-07-01", "2026-08-01", "2026-07-31")
    assert m.month_bounds("2026-12") == ("2026-12-01", "2027-01-01", "2026-12-31")
    assert m.month_bounds("2024-02") == ("2024-02-01", "2024-03-01", "2024-02-29")
    assert m.month_bounds("2026-02")[2] == "2026-02-28"


def test_is_weekday_distinguishes_calendar_days_from_working_days():
    assert [m.is_weekday(f"2026-07-{d:02d}") for d in range(6, 13)] == [True] * 5 + [False] * 2  # Mon..Sun


# ---------------------------------------------------------------- rounding
@pytest.mark.parametrize(
    "value, places, expected",
    [(0.005, 2, 0.01), (1.005, 2, 1.01), (8.045, 2, 8.05), (2.675, 2, 2.68), (0.125, 2, 0.13), (4.4949, 2, 4.49),
     (0.12345, 4, 0.1235), (0.5, 0, 1.0), (0.0, 2, 0.0)],
)
def test_round_half_up_floats(value, places, expected):
    assert m.round_half_up(value, places) == expected


def test_round_half_up_removes_binary_noise_from_computed_averages():
    # (8.01 + 8.02) / 2 is mathematically 8.015; float arithmetic can land a hair below it
    noisy = 8.014999999999999
    assert round(noisy, 2) == 8.01  # what plain round() would report
    assert m.round_half_up(noisy, 2) == 8.02


def test_round_half_up_is_exact_for_fractions():
    from fractions import Fraction

    assert m.round_half_up(Fraction(1, 8), 2) == 0.13  # 0.125
    assert m.round_half_up(Fraction(1, 3), 4) == 0.3333
    assert m.round_half_up(Fraction(2, 3), 2) == 0.67


# ---------------------------------------------------------------- R1 attendance date
@pytest.mark.parametrize(
    "instant, shift, expected",
    [
        (utc("2026-07-10T18:29:59Z"), DAY, "2026-07-10"),  # 23:59:59 IST
        (utc("2026-07-10T18:30:00Z"), DAY, "2026-07-11"),  # 00:00:00 IST next day
        (utc("2026-07-10T20:00:00Z"), DAY, "2026-07-11"),  # 01:30 IST: a UTC date would say 07-10
        (ist(2026, 7, 6, 21, 55), NIGHT, "2026-07-06"),  # before shift start, same day
        (ist(2026, 7, 7, 0, 30), NIGHT, "2026-07-06"),  # after midnight: previous day's shift
        (ist(2026, 7, 7, 5, 59, 59), NIGHT, "2026-07-06"),  # earlier than shift_end 06:00
        (ist(2026, 7, 7, 6, 0, 0), NIGHT, "2026-07-07"),  # exactly shift_end: NOT earlier, own day
        (ist(2026, 7, 7, 12, 0), NIGHT, "2026-07-07"),
        (ist(2026, 7, 7, 3, 0), DAY, "2026-07-07"),  # day shift: the overnight rule never applies
    ],
)
def test_attendance_date(instant, shift, expected):
    assert m.attendance_date(instant, *shift) == expected


def test_attendance_date_same_for_aware_and_naive_utc_inputs():
    aware = utc("2026-07-10T20:00:00Z")
    assert m.attendance_date(aware, *DAY) == m.attendance_date(aware.replace(tzinfo=None), *DAY) == "2026-07-11"


def test_attendance_date_uses_truncated_seconds():
    assert m.attendance_date(ist(2026, 7, 7, 5, 59, 59, 900000), *NIGHT) == "2026-07-06"


# ---------------------------------------------------------------- R2 late
@pytest.mark.parametrize(
    "h, mi, s, us, expected",
    [(9, 29, 0, 0, 0), (9, 30, 0, 0, 0), (9, 40, 0, 0, 0), (9, 40, 0, 900000, 0), (9, 40, 1, 0, 10),
     (9, 40, 1, 900000, 10), (9, 41, 0, 0, 11), (10, 15, 59, 0, 45), (8, 0, 0, 0, 0)],
)
def test_late_day_shift(h, mi, s, us, expected):
    assert m.compute_late_minutes(ist(2026, 7, 6, h, mi, s, us), *DAY) == expected


def test_late_identical_for_any_representation_of_the_same_instant():
    instant = ist(2026, 7, 6, 10, 30)  # 60 minutes late
    for variant in (instant, instant.astimezone(UTC), instant.astimezone(UTC).replace(tzinfo=None)):
        assert m.compute_late_minutes(variant, *DAY) == 60


@pytest.mark.parametrize(
    "instant, expected",
    [(ist(2026, 7, 6, 21, 55), 0), (ist(2026, 7, 6, 22, 10), 0), (ist(2026, 7, 6, 22, 10, 1), 10),
     (ist(2026, 7, 6, 23, 30), 90), (ist(2026, 7, 7, 0, 30), 150), (ist(2026, 7, 7, 5, 59, 59), 479),
     (ist(2026, 7, 7, 6, 0), 0)],  # 06:00 sharp starts the NEXT day's shift, hours before it begins
)
def test_late_overnight_shift(instant, expected):
    assert m.compute_late_minutes(instant, *NIGHT) == expected


# ---------------------------------------------------------------- R3 overtime
@pytest.mark.parametrize(
    "out, expected",
    [(ist(2026, 7, 6, 17, 0), 0), (ist(2026, 7, 6, 18, 30), 0), (ist(2026, 7, 6, 18, 35), 0),
     (ist(2026, 7, 6, 18, 59, 59), 0), (ist(2026, 7, 6, 19, 0), 30), (ist(2026, 7, 6, 19, 10), 40),
     (ist(2026, 7, 6, 19, 10, 59), 40)],
)
def test_overtime_day_shift(out, expected):
    assert m.compute_overtime(out, *DAY, "2026-07-06") == expected


@pytest.mark.parametrize(
    "out, expected",
    [(ist(2026, 7, 7, 6, 29, 59), 0), (ist(2026, 7, 7, 6, 30), 30), (ist(2026, 7, 7, 6, 40), 40),
     (ist(2026, 7, 7, 5, 0), 0)],
)
def test_overtime_overnight_shift_ends_on_next_calendar_day(out, expected):
    assert m.compute_overtime(out, *NIGHT, "2026-07-06") == expected


# ---------------------------------------------------------------- R4 / R5 work hours and half day
def test_work_hours_half_up_on_exact_ties():
    base = ist(2026, 7, 6, 9, 30)
    assert m.compute_work_hours(base, base + timedelta(seconds=54)) == 0.02  # 0.015 h
    assert m.compute_work_hours(base, base + timedelta(hours=8, minutes=2, seconds=42)) == 8.05  # 8.045 h
    assert m.compute_work_hours(base, base + timedelta(hours=8, minutes=3, seconds=18)) == 8.06  # 8.055 h


def test_work_hours_matches_independent_decimal_reference_for_every_second():
    base = ist(2026, 7, 6, 9, 30)
    for secs in range(1, 20 * 3600):
        expected = float((Decimal(secs) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        assert m.compute_work_hours(base, base + timedelta(seconds=secs)) == expected, secs


def test_half_day_uses_the_rounded_value():
    base = ist(2026, 7, 6, 9, 30)
    wh = lambda secs: m.compute_work_hours(base, base + timedelta(seconds=secs))
    assert wh(16181) == 4.49 and m.compute_half_day(wh(16181)) is True
    assert wh(16182) == 4.50 and m.compute_half_day(wh(16182)) is False  # 4.495 h rounds UP to 4.50
    assert m.compute_half_day(wh(12600)) is True  # 3.5 h
    assert m.compute_half_day(None) is False


def test_work_hours_truncates_sub_seconds_first():
    base = ist(2026, 7, 6, 9, 30, 0, 999999)
    assert m.compute_work_hours(base, base + timedelta(seconds=3600, microseconds=900000)) == 1.0


# ---------------------------------------------------------------- derive_fields
def test_derive_fields_all_record_states():
    pin, pout = ist(2026, 7, 6, 10, 5), ist(2026, 7, 6, 19, 10)
    assert m.derive_fields("PRESENT", pin, None, *DAY, "2026-07-06") == {
        "work_hours": None, "late_minutes": 35, "overtime_minutes": 0, "half_day": False}
    assert m.derive_fields("PRESENT", pin, pout, *DAY, "2026-07-06") == {
        "work_hours": 9.08, "late_minutes": 35, "overtime_minutes": 40, "half_day": False}
    for status in ("ABSENT", "LEAVE"):
        assert m.derive_fields(status, None, None, *DAY, "2026-07-06") == {
            "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    for status in ("WFH", "ON_DUTY"):
        assert m.derive_fields(status, pin, pout, *DAY, "2026-07-06")["late_minutes"] == 35


# Every sample document from DATA_MODEL.md, recomputed from its punch times: the stored values and the stored
# `date` must be reproduced exactly (this is the contract's own worked example set).
SAMPLES = [
    # id, shift, status, punch_in, punch_out, date, work_hours, late, overtime, half_day
    ("1 normal day", DAY, "PRESENT", "2026-07-06T03:58:00Z", "2026-07-06T13:05:00Z", "2026-07-06", 9.12, 0, 0, False),
    ("2 late + overtime", DAY, "PRESENT", "2026-07-06T04:35:00Z", "2026-07-06T13:40:00Z", "2026-07-06", 9.08, 35, 40, False),
    ("3 open record", DAY, "PRESENT", "2026-07-07T04:05:00Z", None, "2026-07-07", None, 0, 0, False),
    ("4 half day WFH", DAY, "WFH", "2026-07-07T04:00:00Z", "2026-07-07T07:30:00Z", "2026-07-07", 3.5, 0, 0, True),
    ("7 overnight", NIGHT, "PRESENT", "2026-07-06T16:25:00Z", "2026-07-07T01:10:00Z", "2026-07-06", 8.75, 0, 40, False),
    ("8 regularized (final)", DAY, "PRESENT", "2026-07-07T03:58:00Z", "2026-07-07T13:00:00Z", "2026-07-07", 9.03, 0, 0, False),
    ("8 regularized (before)", DAY, "PRESENT", "2026-07-07T04:35:00Z", "2026-07-07T13:00:00Z", "2026-07-07", 8.42, 35, 0, False),
    ("9 legacy", DAY, "PRESENT", "2026-07-14T04:02:00Z", "2026-07-14T13:01:00Z", "2026-07-14", 8.98, 0, 0, False),
]


@pytest.mark.parametrize("name, shift, status, pin, pout, att, wh, late, ot, half", SAMPLES, ids=[s[0] for s in SAMPLES])
def test_contract_samples_are_reproduced(name, shift, status, pin, pout, att, wh, late, ot, half):
    punch_in, punch_out = utc(pin), (utc(pout) if pout else None)
    assert m.attendance_date(punch_in, *shift) == att
    assert m.derive_fields(status, punch_in, punch_out, *shift, att) == {
        "work_hours": wh, "late_minutes": late, "overtime_minutes": ot, "half_day": half}


# ---------------------------------------------------------------- validation types
def test_epoch_ms_type_accepts_valid_and_rejects_everything_the_contract_lists():
    ta = TypeAdapter(m.EpochMs)
    assert ta.validate_python(1783312500000) == 1783312500000
    assert ta.validate_json("1783312500000") == 1783312500000
    for bad in (1783312500000.0, 1783312500000.5, "1783312500000", True, None, 1783312500, 0, -1,
                99_999_999_999, 4_102_444_800_001):
        with pytest.raises(ValidationError):
            ta.validate_python(bad)
    for bad_json in ("1783312500000.0", '"1783312500000"', "1783312500", "true", "null"):
        with pytest.raises(ValidationError):
            ta.validate_json(bad_json)


def test_epoch_ms_bounds_are_inclusive():
    ta = TypeAdapter(m.EpochMs)
    assert ta.validate_python(m.EPOCH_MS_MIN) == m.EPOCH_MS_MIN
    assert ta.validate_python(m.EPOCH_MS_MAX) == m.EPOCH_MS_MAX


def test_types_enforced_by_fastapi_with_422():
    class Body(BaseModel):
        at: m.EpochMs
        status: m.PresenceStatus = "PRESENT"
        shift: m.HHMM = "09:30"
        day: m.DateStr

    t = FastAPI()

    @t.post("/b")
    def b(body: Body):
        return {"ok": True}

    @t.get("/q")
    def q(page: m.PageParam = 1, page_size: m.PageSizeParam = 20, d: m.DateStr | None = None, month: m.MonthStr | None = None):
        return {"page": page, "page_size": page_size}

    c = TestClient(t)
    good = {"at": 1783312500000, "day": "2026-07-06"}
    assert c.post("/b", json=good).status_code == 200
    bad_bodies = [
        {**good, "at": 1783312500000.0}, {**good, "at": "1783312500000"}, {**good, "at": 1783312500},
        {**good, "status": "ABSENT"}, {**good, "status": "BANANA"}, {**good, "shift": "24:00"},
        {**good, "shift": "9:30"}, {**good, "day": "2026-02-30"},
    ]
    for body in bad_bodies:
        assert c.post("/b", json=body).status_code == 422, body
    assert c.get("/q").json() == {"page": 1, "page_size": 20}
    assert c.get("/q?page_size=100").status_code == 200
    for qs in ("page=0", "page=-1", "page_size=0", "page_size=101", "page=x", "d=2026-02-30", "d=garbage", "month=2026-13", "month=2026-7"):
        assert c.get("/q?" + qs).status_code == 422, qs


# ---------------------------------------------------------------- serializers
def test_serialize_attendance_full_record_with_history_and_no_ids():
    doc = {
        "_id": "SHOULD-NEVER-APPEAR", "id": "NOR-THIS", "emp_code": "EMP0001", "date": "2026-07-07", "status": "PRESENT",
        "punch_in": utc("2026-07-07T03:58:00Z"), "punch_out": utc("2026-07-07T13:00:00Z"),
        "work_hours": 9.03, "late_minutes": 0, "overtime_minutes": 0, "half_day": False,
        "history": [{
            "at": utc("2026-07-08T05:00:00Z"), "by": "hr.admin", "reason": "biometric glitch",
            "changes": {"punch_in": {"from": utc("2026-07-07T04:35:00Z"), "to": utc("2026-07-07T03:58:00Z")},
                        "late_minutes": {"from": 35, "to": 0}, "work_hours": {"from": 8.42, "to": 9.03}}}],
    }
    out = m.serialize_attendance(doc)
    assert "_id" not in out and "id" not in out
    assert out["punch_in"] == 1783396680000 and out["punch_out"] == 1783429200000
    entry = out["history"][0]
    assert entry["at"] == 1783486800000
    assert entry["changes"]["punch_in"] == {"from": 1783398900000, "to": 1783396680000}  # nested dates -> ms
    assert entry["changes"]["late_minutes"] == {"from": 35, "to": 0}
    assert entry["changes"]["work_hours"] == {"from": 8.42, "to": 9.03}
    assert set(out) == {"emp_code", "date", "status", "punch_in", "punch_out", "work_hours", "late_minutes",
                        "overtime_minutes", "half_day", "history"}


def test_serialize_attendance_legacy_and_absent_documents_get_defaults():
    legacy = {"emp_code": "EMP0002", "date": "2026-07-14", "status": "PRESENT",
              "punch_in": utc("2026-07-14T04:02:00Z"), "punch_out": utc("2026-07-14T13:01:00Z"), "work_hours": 8.98,
              "late_minutes": 0, "overtime_minutes": 0}  # no half_day, no history
    out = m.serialize_attendance(legacy)
    assert out["half_day"] is False and out["history"] == []
    bare = m.serialize_attendance({"emp_code": "E", "date": "2026-07-14", "status": "ABSENT", "punch_in": None})
    assert bare == {"emp_code": "E", "date": "2026-07-14", "status": "ABSENT", "punch_in": None, "punch_out": None,
                    "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []}


def test_serialize_employee_created_at_is_int_ms_and_no_id():
    doc = {"_id": "x", "emp_code": "EMP0001", "name": "Asha", "email": "a@x.com", "department": "Eng",
           "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-05", "created_at": utc("2026-01-05T04:30:00Z")}
    out = m.serialize_employee(doc)
    assert out["created_at"] == 1767587400000 and isinstance(out["created_at"], int) and "_id" not in out


# ---------------------------------------------------------------- indexes
EXPECTED_INDEXES = {
    "employees": {"emp_code_unique", "department_emp_code", "department_joined_on", "joined_on_department"},
    "attendance_logs": {"emp_code_date_unique", "emp_code_punch_in_desc", "date_desc_emp_code"},
}


def test_ensure_indexes_creates_everything_and_is_idempotent(db):
    m.ensure_indexes(db)
    m.ensure_indexes(db)  # second run: no error, no duplicates
    for coll, names in EXPECTED_INDEXES.items():
        assert names <= set(db[coll].index_information()), coll
    assert db.employees.index_information()["emp_code_unique"].get("unique") is True
    assert db.attendance_logs.index_information()["emp_code_date_unique"].get("unique") is True


def test_ensure_indexes_on_a_database_that_already_holds_data(db):
    db.employees.insert_many([{"emp_code": f"EMP{i:04d}", "department": "Eng", "joined_on": "2026-01-01"} for i in range(1, 50)])
    db.attendance_logs.insert_many([{"emp_code": "EMP0001", "date": f"2026-07-{d:02d}"} for d in range(1, 29)])
    m.ensure_indexes(db)  # the grader starts the app against an already-seeded database
    assert "emp_code_date_unique" in db.attendance_logs.index_information()


def test_unique_indexes_actually_reject_duplicates(db):
    m.ensure_indexes(db)
    db.employees.insert_one({"emp_code": "EMP0001"})
    with pytest.raises(DuplicateKeyError):
        db.employees.insert_one({"emp_code": "EMP0001"})
    db.attendance_logs.insert_one({"emp_code": "EMP0001", "date": "2026-07-06"})
    db.attendance_logs.insert_one({"emp_code": "EMP0001", "date": "2026-07-07"})  # different date: fine
    with pytest.raises(DuplicateKeyError):
        db.attendance_logs.insert_one({"emp_code": "EMP0001", "date": "2026-07-06"})


def test_duplicate_data_fails_startup_with_a_clear_diagnostic(db):
    db.attendance_logs.insert_many([{"emp_code": "EMP0001", "date": "2026-07-06"},
                                    {"emp_code": "EMP0001", "date": "2026-07-06"}])
    with pytest.raises(m.IndexSetupError) as err:
        m.ensure_indexes(db)
    text = str(err.value)
    assert "emp_code_date_unique" in text and "attendance_logs" in text
    assert "EMP0001" in text and "2026-07-06" in text and "x2" in text  # names the offending key and count
    assert "Startup aborted" in text
    assert "emp_code_date_unique" not in db.attendance_logs.index_information()  # not pretended to exist


def test_app_startup_fails_instead_of_continuing_without_the_unique_index(db):
    db.attendance_logs.insert_many([{"emp_code": "E", "date": "2026-07-06"}, {"emp_code": "E", "date": "2026-07-06"}])
    with pytest.raises(m.IndexSetupError):
        with TestClient(m.app):
            pass


def test_app_startup_succeeds_on_clean_data_and_creates_indexes(db):
    with TestClient(m.app):
        assert "emp_code_date_unique" in db.attendance_logs.index_information()


def test_unreachable_mongodb_is_reported_not_swallowed(monkeypatch):
    from pymongo import MongoClient

    dead = MongoClient("mongodb://localhost:1", tz_aware=True, serverSelectionTimeoutMS=300)["x"]
    monkeypatch.setattr(m, "db", dead)
    with pytest.raises(m.IndexSetupError, match="not reachable"):
        m.ensure_indexes(dead)


# ---------------------------------------------------------------- review regressions and independent oracles
@pytest.mark.parametrize("bad", ["09:30\n", " 09:30", "9:30", "24:00", "09:60", "0٩:30", "０９:30", "09-30", ""])
def test_hhmm_rejects_newline_and_non_ascii_digits(bad):
    with pytest.raises(ValidationError):
        TypeAdapter(m.HHMM).validate_python(bad)


@pytest.mark.parametrize("good", ["00:00", "09:30", "18:30", "22:00", "23:59"])
def test_hhmm_accepts_valid_times(good):
    assert TypeAdapter(m.HHMM).validate_python(good) == good


def test_epoch_ms_constants_match_the_contract():
    assert m.EPOCH_MS_MIN == 100_000_000_000 and m.EPOCH_MS_MAX == 4_102_444_800_000
    assert m.ms_to_instant(m.EPOCH_MS_MAX) == datetime(2100, 1, 1, tzinfo=UTC)
    assert m.dt_to_ms(datetime(2026, 7, 6, 4, 35, tzinfo=UTC)) == 1783312500000  # the contract's own example value


def test_epoch_ms_roundtrip_against_plain_timedelta_arithmetic():
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    rnd = random.Random(20261012)
    for _ in range(20000):
        ms = rnd.randrange(m.EPOCH_MS_MIN, m.EPOCH_MS_MAX + 1)
        whole = ms - ms % 1000
        instant = m.ms_to_instant(ms)
        assert instant == epoch + timedelta(milliseconds=whole) and instant.microsecond == 0
        assert m.dt_to_ms(instant) == whole
        assert m.dt_to_ms(epoch + timedelta(milliseconds=ms)) == ms  # sub-second precision survives dt_to_ms


def _kolkata():
    """IANA Asia/Kolkata for the independent oracle tests. Windows has no system tz database, so these tests need
    the `tzdata` package (listed in requirements-dev.txt). If it is missing, skip with an instruction, not a failure."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        return ZoneInfo("Asia/Kolkata")
    except ZoneInfoNotFoundError:
        pytest.skip("IANA timezone data not found - run: pip install -r requirements-dev.txt (installs tzdata)")


ORACLE_SHIFTS = [DAY, NIGHT, ("23:00", "07:30"), ("00:00", "08:00"), ("08:00", "16:00")]


def _oracle_date(instant, shift):
    """Independent R1 implementation: IANA Asia/Kolkata via zoneinfo, wall-clock comparison."""
    local = instant.astimezone(_kolkata()).replace(microsecond=0)
    start, end = time.fromisoformat(shift[0]), time.fromisoformat(shift[1])
    day = local.date()
    if end <= start and local.time() < end:
        day -= timedelta(days=1)
    return day.isoformat()


def test_attendance_date_matches_zoneinfo_oracle_on_random_and_boundary_instants():
    rnd = random.Random(7)
    lo, hi = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp()), int(datetime(2027, 1, 1, tzinfo=UTC).timestamp())
    instants = [datetime.fromtimestamp(rnd.randrange(lo, hi), UTC) for _ in range(20000)]
    instants += [datetime.fromtimestamp(rnd.randrange(lo, hi), UTC).replace(microsecond=rnd.randrange(10**6)) for _ in range(2000)]
    for shift in ORACLE_SHIFTS:  # dense probes around each shift end and around IST midnight
        end_h, end_m = map(int, shift[1].split(":"))
        for day in range(1, 29, 3):
            for delta in (-2, -1, 0, 1, 2):
                instants.append(ist(2026, 7, day, end_h, end_m) + timedelta(seconds=delta))
            for delta in (-1, 0, 1):
                instants.append(ist(2026, 7, day, 0, 0) + timedelta(seconds=delta))
    for instant in instants:
        for shift in ORACLE_SHIFTS:
            assert m.attendance_date(instant, *shift) == _oracle_date(instant, shift), (instant, shift)


def test_late_and_overtime_match_zoneinfo_oracle():
    rnd = random.Random(11)
    kol = _kolkata()
    for shift in ORACLE_SHIFTS:
        s_t, e_t = time.fromisoformat(shift[0]), time.fromisoformat(shift[1])
        for _ in range(3000):
            att = date(2026, 7, 1) + timedelta(days=rnd.randrange(0, 28))
            start = datetime.combine(att, s_t, kol)
            end = datetime.combine(att, e_t, kol) + (timedelta(days=1) if e_t <= s_t else timedelta())
            punch_in = start + timedelta(seconds=rnd.randrange(-3 * 3600, 6 * 3600))
            punch_out = end + timedelta(seconds=rnd.randrange(-3 * 3600, 4 * 3600))
            late_secs = int((punch_in - start).total_seconds())
            ot_min = int((punch_out - end).total_seconds()) // 60
            assert m.compute_late_minutes(punch_in, *shift, att.isoformat()) == (late_secs // 60 if late_secs > 600 else 0)
            assert m.compute_overtime(punch_out, *shift, att.isoformat()) == (ot_min if ot_min >= 30 else 0)


def test_ensure_indexes_creates_exactly_the_expected_set(db):
    m.ensure_indexes(db)
    for coll, names in EXPECTED_INDEXES.items():
        assert set(db[coll].index_information()) == names | {"_id_"}, coll


# Real-MongoDB-only checks (skipped unless TEST_MONGO_URI is set)
@pytest.mark.real_mongo
def test_real_server_is_mongodb_6_or_newer(db):
    assert int(db.client.server_info()["version"].split(".")[0]) >= 6


@pytest.mark.real_mongo
def test_real_server_reports_duplicate_key_code_when_unique_index_cannot_be_built(db):
    db.attendance_logs.insert_many([{"emp_code": "EMP0001", "date": "2026-07-06"}, {"emp_code": "EMP0001", "date": "2026-07-06"}])
    with pytest.raises(m.IndexSetupError) as err:
        m.ensure_indexes(db)
    assert "DuplicateKey" in str(err.value) and "x2" in str(err.value)
