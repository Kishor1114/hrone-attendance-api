"""Phase 4B tests: PATCH /attendance/{emp_code}/{date} (manual corrections).

Mongomock by default; tests marked `real_mongo` (and everything, when TEST_MONGO_URI is set) use a real MongoDB.
Day shift 09:30-18:30 IST means 04:00Z-13:00Z. Overnight shift 22:00-06:00 IST means 16:30Z-00:30Z (next day).
"""
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import app.main as m

UTC = timezone.utc
DAY, NIGHT = ("09:30", "18:30"), ("22:00", "06:00")
RECORD_KEYS = {"emp_code", "date", "status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day", "history"}
REASON, BY = "biometric glitch", "hr.admin"


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


def seed(db, code="EMP0001", date="2026-07-06", status="PRESENT", punch_in="2026-07-06T04:35:00Z", punch_out="2026-07-06T13:40:00Z",
         work_hours=9.08, late=35, overtime=40, half_day=False, history=()):
    """Default = contract sample 2: in 10:05 IST (35 late), out 19:10 IST (40 overtime), 9.08 h."""
    doc = {"emp_code": code, "date": date, "status": status, "punch_in": dt(punch_in) if punch_in else None,
           "punch_out": dt(punch_out) if punch_out else None, "work_hours": work_hours, "late_minutes": late,
           "overtime_minutes": overtime, "half_day": half_day, "history": list(history)}
    db.attendance_logs.insert_one(doc)
    return doc


def seed_open(db, **kw):
    return seed(db, **{"punch_in": "2026-07-06T04:00:00Z", "punch_out": None, "work_hours": None, "late": 0, "overtime": 0, **kw})


def seed_absent(db, status="ABSENT", **kw):
    return seed(db, **{"status": status, "punch_in": None, "punch_out": None, "work_hours": None, "late": 0, "overtime": 0, **kw})


def patch(client, code="EMP0001", date="2026-07-06", **fields):
    body = {"reason": REASON, "regularized_by": BY}
    for key, value in fields.items():  # only the two punch fields take ISO shorthand; everything else is sent exactly as given
        body[key] = ms(value) if key in ("punch_in", "punch_out") and isinstance(value, str) and value.endswith("Z") else value
    return client.patch(f"/attendance/{code}/{date}", json=body)


def stored(db, code="EMP0001", date="2026-07-06"):
    return db.attendance_logs.find_one({"emp_code": code, "date": date}, {"_id": 0})


def assert_422(resp):
    assert resp.status_code == 422, resp.text
    assert isinstance(resp.json()["detail"], list)


def changes_of(resp):
    return resp.json()["history"][-1]["changes"]


SAMPLE_8_ENTRY = {"at": dt("2026-07-08T05:00:00Z"), "by": BY, "reason": REASON,
                  "changes": {"punch_in": {"from": dt("2026-07-07T04:35:00Z"), "to": dt("2026-07-07T03:58:00Z")},
                              "late_minutes": {"from": 35, "to": 0}, "work_hours": {"from": 8.42, "to": 9.03}}}


# ================================================================= 1. valid corrections, 5. recomputation, 7. history contents
def test_contract_sample_8_correction_reproduces_the_documented_record(client, db):
    seed_employee(db)
    seed(db, punch_out="2026-07-06T13:00:00Z", work_hours=8.42, late=35, overtime=0)  # before HR's fix (sample 8, one day earlier)
    before, after = m.dt_to_ms(m.utc_now()), None
    r = patch(client, punch_in="2026-07-06T03:58:00Z")
    after = m.dt_to_ms(m.utc_now())
    assert r.status_code == 200
    body = r.json()
    assert set(body) == RECORD_KEYS  # no id / _id
    assert (body["punch_in"], body["punch_out"], body["work_hours"], body["late_minutes"], body["overtime_minutes"], body["half_day"]) == (
        ms("2026-07-06T03:58:00Z"), ms("2026-07-06T13:00:00Z"), 9.03, 0, 0, False)
    entry = body["history"][0]
    assert len(body["history"]) == 1 and entry["by"] == BY and entry["reason"] == REASON and before <= entry["at"] <= after
    assert entry["changes"] == {"punch_in": {"from": ms("2026-07-06T04:35:00Z"), "to": ms("2026-07-06T03:58:00Z")},
                                "late_minutes": {"from": 35, "to": 0}, "work_hours": {"from": 8.42, "to": 9.03}}  # exactly DATA_MODEL sample 8


def test_correcting_punch_out_recomputes_hours_and_overtime(client, db):
    seed_employee(db)
    seed(db)
    r = patch(client, punch_out="2026-07-06T14:10:00Z")  # 19:40 IST
    assert r.status_code == 200
    assert (r.json()["work_hours"], r.json()["overtime_minutes"], r.json()["late_minutes"], r.json()["half_day"]) == (9.58, 70, 35, False)
    assert changes_of(r) == {"punch_out": {"from": ms("2026-07-06T13:40:00Z"), "to": ms("2026-07-06T14:10:00Z")},
                             "work_hours": {"from": 9.08, "to": 9.58}, "overtime_minutes": {"from": 40, "to": 70}}


def test_status_only_correction_between_presence_statuses_changes_only_status(client, db):
    seed_employee(db)
    seed(db)
    r = patch(client, status="WFH")
    assert r.status_code == 200 and r.json()["status"] == "WFH"
    assert changes_of(r) == {"status": {"from": "PRESENT", "to": "WFH"}}
    assert (r.json()["work_hours"], r.json()["late_minutes"], r.json()["overtime_minutes"]) == (9.08, 35, 40)  # derived values unchanged


def test_correcting_to_absent_clears_punches_and_every_derived_field(client, db):
    seed_employee(db)
    seed(db)
    r = patch(client, status="ABSENT")
    assert r.status_code == 200
    assert (r.json()["punch_in"], r.json()["punch_out"], r.json()["work_hours"], r.json()["late_minutes"], r.json()["overtime_minutes"],
            r.json()["half_day"]) == (None, None, None, 0, 0, False)
    assert changes_of(r) == {"status": {"from": "PRESENT", "to": "ABSENT"},
                             "punch_in": {"from": ms("2026-07-06T04:35:00Z"), "to": None},
                             "punch_out": {"from": ms("2026-07-06T13:40:00Z"), "to": None},
                             "work_hours": {"from": 9.08, "to": None}, "late_minutes": {"from": 35, "to": 0},
                             "overtime_minutes": {"from": 40, "to": 0}}  # half_day was already false: not listed
    assert stored(db)["punch_in"] is None and stored(db)["punch_out"] is None


def test_correcting_a_half_day_record_to_leave_resets_half_day(client, db):
    seed_employee(db)
    seed(db, punch_in="2026-07-06T04:00:00Z", punch_out="2026-07-06T07:30:00Z", work_hours=3.5, late=0, overtime=0, half_day=True)
    r = patch(client, status="LEAVE")
    assert changes_of(r)["half_day"] == {"from": True, "to": False} and r.json()["half_day"] is False


def test_absent_to_present_with_punch_in_computes_late_only(client, db):
    seed_employee(db)
    seed_absent(db)
    r = patch(client, status="PRESENT", punch_in="2026-07-06T04:35:00Z")
    assert r.status_code == 200
    assert (r.json()["work_hours"], r.json()["late_minutes"], r.json()["overtime_minutes"], r.json()["half_day"]) == (None, 35, 0, False)
    assert changes_of(r) == {"status": {"from": "ABSENT", "to": "PRESENT"}, "punch_in": {"from": None, "to": ms("2026-07-06T04:35:00Z")},
                             "late_minutes": {"from": 0, "to": 35}}


def test_absent_to_wfh_with_both_times_computes_everything(client, db):
    seed_employee(db)
    seed_absent(db)
    r = patch(client, status="WFH", punch_in="2026-07-06T04:00:00Z", punch_out="2026-07-06T13:40:00Z")
    assert r.status_code == 200
    assert (r.json()["work_hours"], r.json()["late_minutes"], r.json()["overtime_minutes"], r.json()["status"]) == (9.67, 0, 40, "WFH")
    assert set(changes_of(r)) == {"status", "punch_in", "punch_out", "work_hours", "overtime_minutes"}


def test_leave_to_absent_changes_only_status(client, db):
    seed_employee(db)
    seed_absent(db, status="LEAVE")
    r = patch(client, status="ABSENT")
    assert r.status_code == 200 and changes_of(r) == {"status": {"from": "LEAVE", "to": "ABSENT"}}


def test_an_open_record_can_be_closed_by_a_correction(client, db):
    seed_employee(db)
    seed_open(db)
    r = patch(client, punch_out="2026-07-06T13:40:00Z")
    assert r.status_code == 200
    assert changes_of(r) == {"punch_out": {"from": None, "to": ms("2026-07-06T13:40:00Z")}, "work_hours": {"from": None, "to": 9.67},
                             "overtime_minutes": {"from": 0, "to": 40}}


@pytest.mark.parametrize("seconds, work_hours, half_day", [(16181, 4.49, True), (16182, 4.5, False), (12600, 3.5, True), (28962, 8.05, False)])
def test_half_up_rounding_and_half_day_boundary_through_a_correction(client, db, seconds, work_hours, half_day):
    seed_employee(db)
    seed_open(db)  # in 04:00:00Z
    r = patch(client, punch_out=ms("2026-07-06T04:00:00Z") + seconds * 1000)
    assert (r.json()["work_hours"], r.json()["half_day"]) == (work_hours, half_day)
    assert ("half_day" in changes_of(r)) == half_day  # only listed when it actually flipped


@pytest.mark.parametrize("out, expected", [("2026-07-06T13:29:59Z", 0), ("2026-07-06T13:30:00Z", 30), ("2026-07-06T13:31:00Z", 31)])
def test_overtime_threshold_through_a_correction(client, db, out, expected):
    seed_employee(db)
    seed_open(db)
    assert patch(client, punch_out=out).json()["overtime_minutes"] == expected


@pytest.mark.parametrize("pin, expected", [("2026-07-06T04:00:00Z", 0), ("2026-07-06T04:10:00Z", 0), ("2026-07-06T04:10:01Z", 10),
                                           ("2026-07-06T04:45:59Z", 45)])
def test_late_threshold_through_a_correction(client, db, pin, expected):
    seed_employee(db)
    seed(db)  # stored late = 35
    r = patch(client, punch_in=pin)
    assert r.json()["late_minutes"] == expected
    assert changes_of(r)["late_minutes"] == {"from": 35, "to": expected}


def test_sub_second_input_is_truncated(client, db):
    seed_employee(db)
    seed(db)
    r = patch(client, punch_in=ms("2026-07-06T04:10:01Z") + 999)
    assert r.json()["punch_in"] == ms("2026-07-06T04:10:01Z") and r.json()["late_minutes"] == 10
    assert stored(db)["punch_in"] == dt("2026-07-06T04:10:01Z")


def test_client_supplied_derived_fields_are_ignored(client, db):
    seed_employee(db)
    seed(db)
    r = client.patch("/attendance/EMP0001/2026-07-06", json={  # sent explicitly: the helper's own `date` argument is the URL
        "reason": REASON, "regularized_by": BY, "punch_out": ms("2026-07-06T14:10:00Z"), "work_hours": 99, "late_minutes": 500,
        "overtime_minutes": 999, "half_day": True, "history": [{"x": 1}], "emp_code": "EMP9999", "date": "2030-01-01"})
    assert r.status_code == 200
    assert (r.json()["work_hours"], r.json()["late_minutes"], r.json()["overtime_minutes"], r.json()["half_day"]) == (9.58, 35, 70, False)
    assert len(r.json()["history"]) == 1 and set(changes_of(r)) == {"punch_out", "work_hours", "overtime_minutes"}
    assert r.json()["emp_code"] == "EMP0001" and r.json()["date"] == "2026-07-06"


def test_correction_does_not_touch_other_records(client, db):
    seed_employee(db)
    seed_employee(db, "EMP0002")
    seed(db)
    seed(db, "EMP0002")
    seed(db, date="2026-07-07", punch_in="2026-07-07T04:35:00Z", punch_out="2026-07-07T13:40:00Z")
    before = (stored(db, "EMP0002"), stored(db, date="2026-07-07"))
    assert patch(client, status="WFH").status_code == 200
    assert (stored(db, "EMP0002"), stored(db, date="2026-07-07")) == before


# ================================================================= 7. history: appended, preserved, exact
def test_existing_history_is_preserved_and_exactly_one_entry_is_appended(client, db):
    seed_employee(db)
    seed(db, punch_in="2026-07-06T03:58:00Z", punch_out="2026-07-06T13:00:00Z", work_hours=9.03, late=0, overtime=0, history=[SAMPLE_8_ENTRY])
    r = patch(client, status="WFH", reason="working remotely")
    assert r.status_code == 200 and len(r.json()["history"]) == 2
    assert stored(db)["history"][0] == SAMPLE_8_ENTRY  # the old entry is untouched
    assert r.json()["history"][0]["at"] == ms("2026-07-08T05:00:00Z")
    assert r.json()["history"][1]["reason"] == "working remotely" and r.json()["history"][1]["changes"] == {"status": {"from": "PRESENT", "to": "WFH"}}


def test_sequential_corrections_build_a_continuous_chain(client, db):
    seed_employee(db)
    seed(db)
    steps = [dict(punch_out="2026-07-06T14:00:00Z"), dict(status="WFH"), dict(punch_in="2026-07-06T04:05:00Z"), dict(punch_out="2026-07-06T14:30:00Z")]
    for i, step in enumerate(steps, start=1):
        r = patch(client, reason=f"correction number {i}", **step)
        assert r.status_code == 200 and len(r.json()["history"]) == i  # exactly one entry per success
    history = r.json()["history"]
    assert [h["reason"] for h in history] == [f"correction number {i}" for i in range(1, 5)]
    outs = [h["changes"]["punch_out"] for h in history if "punch_out" in h["changes"]]
    assert outs[0]["from"] == ms("2026-07-06T13:40:00Z") and outs[0]["to"] == outs[1]["from"] and outs[1]["to"] == ms("2026-07-06T14:30:00Z")
    assert [h["at"] for h in history] == sorted(h["at"] for h in history)


def test_history_stores_bson_datetimes_and_the_api_returns_epoch_ms_in_every_nested_place(client, db):
    seed_employee(db)
    seed(db)
    r = patch(client, punch_in="2026-07-06T04:10:01Z", punch_out="2026-07-06T14:10:00Z")
    raw = stored(db)["history"][0]
    assert raw["at"].tzinfo is not None and raw["at"].microsecond % 1000 == 0
    for field in ("punch_in", "punch_out"):
        for side in ("from", "to"):
            assert isinstance(raw["changes"][field][side], datetime) and raw["changes"][field][side].tzinfo is not None  # BSON dates
    api = r.json()["history"][0]
    assert isinstance(api["at"], int) and api["at"] == m.dt_to_ms(raw["at"])
    assert api["changes"]["punch_in"] == {"from": ms("2026-07-06T04:35:00Z"), "to": ms("2026-07-06T04:10:01Z")}  # ints, not ISO strings
    assert api["changes"]["punch_out"] == {"from": ms("2026-07-06T13:40:00Z"), "to": ms("2026-07-06T14:10:00Z")}
    assert api["changes"]["late_minutes"] == {"from": 35, "to": 10} and isinstance(api["changes"]["work_hours"]["to"], float)
    assert client.get("/attendance?emp_code=EMP0001").json()["items"][0]["history"] == r.json()["history"]  # same via GET


def test_history_changes_contains_only_fields_that_actually_changed(client, db):
    seed_employee(db)
    seed(db)
    r = patch(client, punch_out="2026-07-06T13:45:00Z")  # 19:15 IST: overtime 40 -> 45; late and half_day stay
    assert set(changes_of(r)) == {"punch_out", "work_hours", "overtime_minutes"}
    r = patch(client, punch_in="2026-07-06T04:35:00Z", status="WFH")  # punch_in repeated unchanged, status changes
    assert set(changes_of(r)) == {"status"}


# ================================================================= 7. no-op corrections
@pytest.mark.parametrize("fields", [
    {},                                              # nothing but reason / regularized_by
    {"status": "PRESENT"},                           # same status
    {"punch_in": "2026-07-06T04:35:00Z"},            # same punch_in
    {"punch_out": "2026-07-06T13:40:00Z"},           # same punch_out
    {"status": "PRESENT", "punch_in": "2026-07-06T04:35:00Z", "punch_out": "2026-07-06T13:40:00Z"},
    {"punch_in": ms("2026-07-06T04:35:00Z") + 999},  # truncates to the stored second
    {"work_hours": 5, "late_minutes": 0, "overtime_minutes": 0, "half_day": True},  # derived fields are ignored: still nothing
])
def test_a_request_that_changes_nothing_is_422_and_writes_nothing(client, db, fields):
    seed_employee(db)
    seed(db, history=[SAMPLE_8_ENTRY])
    before = stored(db)
    assert_422(patch(client, **fields))
    assert stored(db) == before  # no history entry appended, nothing rewritten


def test_a_no_op_on_an_absent_record_is_422(client, db):
    seed_employee(db)
    seed_absent(db)
    assert_422(patch(client, status="ABSENT"))
    assert len(stored(db)["history"]) == 0


def test_a_no_op_is_not_decided_by_derived_values(client, db):
    """Stored derived values that disagree with a recompute do not turn an unchanged request into a correction."""
    seed_employee(db)
    seed(db, late=7)  # R2 would say 35; the request changes none of status / punch_in / punch_out
    assert_422(patch(client, punch_in="2026-07-06T04:35:00Z"))
    assert stored(db)["late_minutes"] == 7


# ================================================================= 2. missing employee / record, path validation
def test_unknown_employee_is_404(client, db):
    seed_employee(db)
    seed(db)
    r = patch(client, code="EMP9999", status="WFH")
    assert r.status_code == 404 and isinstance(r.json()["detail"], str)


def test_employee_without_a_record_for_that_date_is_404(client, db):
    seed_employee(db)
    seed(db, date="2026-07-07", punch_in="2026-07-07T04:35:00Z", punch_out="2026-07-07T13:40:00Z")
    r = patch(client, date="2026-07-06", status="WFH")
    assert r.status_code == 404 and isinstance(r.json()["detail"], str)


def test_orphan_log_for_an_unknown_employee_is_404(client, db):
    seed(db, "EMP9999")
    assert patch(client, code="EMP9999", status="WFH").status_code == 404


def test_404_comes_before_the_stored_record_rules(client, db):
    seed_employee(db)
    assert patch(client, status="ABSENT", punch_in="2026-07-06T04:00:00Z").status_code == 404  # would be 422 if a record existed


def test_request_shape_is_validated_before_anything_is_looked_up(client, db):
    assert_422(client.patch("/attendance/EMP9999/2026-07-06", json={"regularized_by": BY}))  # missing reason, unknown employee


@pytest.mark.parametrize("bad_date", ["2026-02-30", "garbage", "2026-7-6", "20260706", "٢٠٢٦-٠٧-٠٦", "2026-13-01"])
def test_invalid_path_date_is_422(client, db, bad_date):
    seed_employee(db)
    seed(db)
    assert_422(client.patch(f"/attendance/EMP0001/{bad_date}", json={"reason": REASON, "regularized_by": BY, "status": "WFH"}))


# ================================================================= 3. reason / regularized_by
@pytest.mark.parametrize("body", [
    {"regularized_by": BY, "status": "WFH"},                                   # reason missing
    {"reason": REASON, "status": "WFH"},                                       # regularized_by missing
    {"status": "WFH"},                                                         # both missing
    {"reason": "abcd", "regularized_by": BY, "status": "WFH"},                 # 4 chars: below minLength 5
    {"reason": "x" * 201, "regularized_by": BY, "status": "WFH"},              # above maxLength 200
    {"reason": "", "regularized_by": BY, "status": "WFH"},
    {"reason": None, "regularized_by": BY, "status": "WFH"},
    {"reason": 12345, "regularized_by": BY, "status": "WFH"},
    {"reason": REASON, "regularized_by": "", "status": "WFH"},
    {"reason": REASON, "regularized_by": "x" * 51, "status": "WFH"},           # above maxLength 50
    {"reason": REASON, "regularized_by": None, "status": "WFH"},
    {"reason": REASON, "regularized_by": 7, "status": "WFH"},
])
def test_missing_or_invalid_reason_and_regularized_by_are_422(client, db, body):
    seed_employee(db)
    seed(db)
    before = stored(db)
    assert_422(client.patch("/attendance/EMP0001/2026-07-06", json=body))
    assert stored(db) == before


@pytest.mark.parametrize("reason, by", [("x" * 5, "a"), ("x" * 200, "y" * 50)])
def test_reason_and_regularized_by_boundary_lengths_are_accepted(client, db, reason, by):
    seed_employee(db)
    seed(db)
    r = client.patch("/attendance/EMP0001/2026-07-06", json={"reason": reason, "regularized_by": by, "status": "WFH"})
    assert r.status_code == 200 and (r.json()["history"][0]["reason"], r.json()["history"][0]["by"]) == (reason, by)


@pytest.mark.parametrize("body", [[], "text", 5, None])
def test_non_object_bodies_are_422(client, db, body):
    seed_employee(db)
    seed(db)
    assert_422(client.patch("/attendance/EMP0001/2026-07-06", json=body))


# ================================================================= 4. invalid timestamps and status / punch combinations
@pytest.mark.parametrize("field", ["punch_in", "punch_out"])
@pytest.mark.parametrize("bad", [1783312500000.0, "1783312500000", 1783312500, 0, -1, True, 99_999_999_999, 4_102_444_800_001, [], None])
def test_invalid_timestamp_values_are_422(client, db, field, bad):
    seed_employee(db)
    seed(db)
    before = stored(db)
    assert_422(patch(client, **{field: bad}))
    assert stored(db) == before


@pytest.mark.parametrize("null_field", ["status", "punch_in", "punch_out"])
def test_explicit_null_is_rejected_even_when_the_rest_of_the_request_is_a_valid_correction(client, db, null_field):
    """The contract's schemas are non-nullable: null is not 'omitted' and cannot clear a punch time. (Sent alone it would
    also be a 422 as a no-op, so the meaningful case is a null next to a change that would otherwise succeed.)"""
    seed_employee(db)
    seed(db)
    before = stored(db)
    body = {"reason": REASON, "regularized_by": BY, "punch_out": ms("2026-07-06T14:10:00Z"), null_field: None}
    r = client.patch("/attendance/EMP0001/2026-07-06", json=body)
    assert_422(r)
    assert "cannot be null" in str(r.json()["detail"])
    assert stored(db) == before


@pytest.mark.parametrize("bad", ["BANANA", "present", "", 5, None, ["PRESENT"]])
def test_invalid_status_values_are_422(client, db, bad):
    seed_employee(db)
    seed(db)
    assert_422(patch(client, status=bad))


@pytest.mark.parametrize("status", ["ABSENT", "LEAVE"])
@pytest.mark.parametrize("fields", [{"punch_in": "2026-07-06T04:00:00Z"}, {"punch_out": "2026-07-06T13:00:00Z"},
                                    {"punch_in": "2026-07-06T04:00:00Z", "punch_out": "2026-07-06T13:00:00Z"}])
def test_absent_or_leave_together_with_a_punch_time_is_422(client, db, status, fields):
    seed_employee(db)
    seed(db)
    before = stored(db)
    r = patch(client, status=status, **fields)
    assert_422(r)
    assert stored(db) == before


@pytest.mark.parametrize("fields", [{"punch_in": "2026-07-06T04:00:00Z"}, {"punch_out": "2026-07-06T13:00:00Z"}])
def test_punch_time_on_a_record_that_stays_absent_is_422(client, db, fields):
    """Contract data model: ABSENT/LEAVE have both punch times null. Status omitted + stored ABSENT = still ABSENT."""
    seed_employee(db)
    seed_absent(db)
    assert_422(patch(client, **fields))
    assert stored(db)["punch_in"] is None


@pytest.mark.parametrize("status", ["PRESENT", "WFH", "ON_DUTY"])
def test_presence_status_without_any_punch_in_is_422(client, db, status):
    seed_employee(db)
    seed_absent(db)
    r = patch(client, status=status)
    assert_422(r)
    assert r.json()["detail"][0]["loc"] == ["body", "punch_in"]
    assert stored(db)["status"] == "ABSENT"


def test_presence_status_with_only_a_punch_out_on_an_absent_record_is_422(client, db):
    seed_employee(db)
    seed_absent(db)
    assert_422(patch(client, status="PRESENT", punch_out="2026-07-06T13:00:00Z"))


def test_punch_out_must_be_after_punch_in(client, db):
    seed_employee(db)
    seed(db)
    for out in ("2026-07-06T04:35:00Z", "2026-07-06T04:34:59Z", "2026-07-06T04:00:00Z"):  # equal, one second early, earlier
        assert_422(patch(client, punch_out=out))
    assert patch(client, punch_out="2026-07-06T04:35:01Z").status_code == 200  # one second later is fine


def test_punch_out_within_24_hours(client, db):
    seed_employee(db)
    seed(db)
    assert_422(patch(client, punch_out="2026-07-07T04:35:01Z"))
    assert_422(patch(client, punch_out="2026-07-08T04:35:00Z"))
    r = patch(client, punch_out="2026-07-07T04:35:00Z")  # exactly 24 h
    assert r.status_code == 200 and r.json()["work_hours"] == 24.0


def test_a_new_punch_in_that_falls_after_the_stored_punch_out_is_422(client, db):
    seed_employee(db)
    seed(db)  # out 13:40Z
    r = patch(client, punch_in="2026-07-06T14:00:00Z")  # same IST date, but after the stored punch_out
    assert_422(r)
    assert r.json()["detail"][0]["loc"] == ["body", "punch_in"]


def test_422_details_name_the_offending_field(client, db):
    seed_employee(db)
    seed(db)
    assert patch(client, punch_out="2026-07-06T04:00:00Z").json()["detail"][0]["loc"] == ["body", "punch_out"]
    assert patch(client, status="ABSENT", punch_in="2026-07-06T04:00:00Z").json()["detail"][0]["loc"] == ["body", "punch_in"]


@pytest.mark.parametrize("pin, ok", [
    ("2026-07-05T18:29:59Z", False),  # 23:59:59 IST on 07-05: the previous attendance day
    ("2026-07-05T18:30:00Z", True),   # 00:00:00 IST on 07-06
    ("2026-07-06T18:29:59Z", True),   # 23:59:59 IST on 07-06
    ("2026-07-06T18:30:00Z", False),  # 00:00:00 IST on 07-07: the next day
])
def test_punch_in_must_stay_on_the_records_attendance_date(client, db, pin, ok):
    seed_employee(db)
    seed_open(db)
    r = patch(client, punch_in=pin)
    assert (r.status_code == 200) == ok
    if not ok:
        assert_422(r)
        assert stored(db)["punch_in"] == dt("2026-07-06T04:00:00Z")


# ================================================================= 6. overnight shifts
@pytest.fixture
def night(db):
    seed_employee(db, "EMP0005", NIGHT)
    seed(db, "EMP0005", "2026-07-06", punch_in="2026-07-06T16:25:00Z", punch_out="2026-07-07T01:10:00Z", work_hours=8.75, late=0, overtime=40)  # sample 7


def test_overnight_punch_in_after_midnight_stays_on_the_shifts_start_day(client, night):
    r = patch(client, "EMP0005", punch_in="2026-07-06T19:00:00Z")  # 00:30 IST on 07-07 belongs to the 07-06 shift
    assert r.status_code == 200 and r.json()["date"] == "2026-07-06"
    assert (r.json()["late_minutes"], r.json()["work_hours"], r.json()["overtime_minutes"]) == (150, 6.17, 40)
    assert changes_of(r) == {"punch_in": {"from": ms("2026-07-06T16:25:00Z"), "to": ms("2026-07-06T19:00:00Z")},
                             "work_hours": {"from": 8.75, "to": 6.17}, "late_minutes": {"from": 0, "to": 150}}


def test_overnight_punch_out_overtime_is_measured_from_the_next_days_shift_end(client, night):
    r = patch(client, "EMP0005", punch_out="2026-07-07T00:59:59Z")  # 06:29:59 IST: 29 minutes after the end
    assert (r.json()["overtime_minutes"], r.json()["work_hours"]) == (0, 8.58)
    assert changes_of(r) == {"punch_out": {"from": ms("2026-07-07T01:10:00Z"), "to": ms("2026-07-07T00:59:59Z")},
                             "work_hours": {"from": 8.75, "to": 8.58}, "overtime_minutes": {"from": 40, "to": 0}}
    assert patch(client, "EMP0005", punch_out="2026-07-07T01:00:00Z").json()["overtime_minutes"] == 30  # 06:30:00 exactly: counted


@pytest.mark.parametrize("pin, ok", [
    ("2026-07-07T00:29:59Z", True),   # 05:59:59 IST: still earlier than shift_end 06:00 -> the 07-06 shift
    ("2026-07-07T00:30:00Z", False),  # 06:00:00 IST: its own day (07-07) -> not this record
    ("2026-07-06T15:29:59Z", True),   # 20:59:59 IST on 07-06, before the shift starts: same day
    ("2026-07-05T17:00:00Z", False),  # 22:30 IST on 07-05: the previous shift
])
def test_overnight_attendance_date_rule_applies_to_corrected_punch_ins(client, night, pin, ok):
    r = patch(client, "EMP0005", punch_in=pin, punch_out="2026-07-07T01:10:00Z")
    assert (r.status_code == 200) == ok, r.text
    if not ok:
        assert_422(r)


def test_overnight_open_record_corrected_to_absent_and_back(client, db):
    seed_employee(db, "EMP0005", NIGHT)
    seed_open(db, code="EMP0005", punch_in="2026-07-06T16:25:00Z")
    assert patch(client, "EMP0005", status="ABSENT").status_code == 200
    r = patch(client, "EMP0005", status="PRESENT", punch_in="2026-07-06T19:00:00Z")
    assert r.status_code == 200 and r.json()["late_minutes"] == 150 and len(r.json()["history"]) == 2


# ================================================================= 8. legacy records
LEGACY = {"emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT", "punch_in": dt("2026-07-06T04:02:00Z"),
          "punch_out": dt("2026-07-06T13:01:00Z"), "work_hours": 8.98, "late_minutes": 0, "overtime_minutes": 0}  # no half_day, no history


def test_legacy_record_without_history_gets_a_history_with_one_entry(client, db):
    seed_employee(db)
    db.attendance_logs.insert_one(dict(LEGACY))
    r = patch(client, punch_out="2026-07-06T13:40:00Z")
    assert r.status_code == 200 and len(r.json()["history"]) == 1
    assert changes_of(r) == {"punch_out": {"from": ms("2026-07-06T13:01:00Z"), "to": ms("2026-07-06T13:40:00Z")},
                             "work_hours": {"from": 8.98, "to": 9.63}, "overtime_minutes": {"from": 0, "to": 40}}
    assert isinstance(stored(db)["history"], list) and len(stored(db)["history"]) == 1


def test_legacy_record_with_null_history_is_handled(client, db):
    seed_employee(db)
    db.attendance_logs.insert_one({**LEGACY, "history": None})
    r = patch(client, status="WFH")
    assert r.status_code == 200 and len(r.json()["history"]) == 1 and stored(db)["history"][0]["changes"] == {"status": {"from": "PRESENT", "to": "WFH"}}


def test_legacy_record_missing_derived_fields_gets_no_spurious_changes(client, db):
    seed_employee(db)
    db.attendance_logs.insert_one({"emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT", "punch_in": dt("2026-07-06T04:02:00Z"),
                                   "punch_out": dt("2026-07-06T13:01:00Z"), "work_hours": 8.98})  # late / overtime / half_day / history all missing
    r = patch(client, status="WFH")
    assert changes_of(r) == {"status": {"from": "PRESENT", "to": "WFH"}}  # missing means 0 / false, which equals the recompute
    assert (r.json()["late_minutes"], r.json()["overtime_minutes"], r.json()["half_day"]) == (0, 0, False)
    assert stored(db)["half_day"] is False and stored(db)["late_minutes"] == 0  # normalized on write


def test_legacy_absent_record_without_punch_fields(client, db):
    seed_employee(db)
    db.attendance_logs.insert_one({"emp_code": "EMP0001", "date": "2026-07-06", "status": "ABSENT"})
    assert patch(client, status="LEAVE").status_code == 200
    r = patch(client, status="PRESENT", punch_in="2026-07-06T04:35:00Z")
    assert r.status_code == 200 and r.json()["late_minutes"] == 35 and len(r.json()["history"]) == 2


def test_legacy_employee_without_shift_fields_uses_contract_defaults(client, db):
    db.employees.insert_one({"emp_code": "EMP0001", "name": "Old", "email": "o@example.com", "department": "Eng", "joined_on": "2025-01-01"})
    seed(db)
    assert patch(client, punch_out="2026-07-06T14:10:00Z").json()["overtime_minutes"] == 70  # 09:30-18:30 assumed


def test_naive_stored_datetimes_are_treated_as_utc(client, db):
    seed_employee(db)
    db.attendance_logs.insert_one({**LEGACY, "punch_in": datetime(2026, 7, 6, 4, 2, 0), "punch_out": datetime(2026, 7, 6, 13, 1, 0)})
    assert_422(patch(client, punch_in="2026-07-06T04:02:00Z"))  # the same instant: still a no-op
    r = patch(client, punch_in="2026-07-06T03:58:00Z")
    assert r.status_code == 200 and changes_of(r)["punch_in"] == {"from": ms("2026-07-06T04:02:00Z"), "to": ms("2026-07-06T03:58:00Z")}


# ================================================================= 9. concurrency
def _wrapped_db(real, barrier=None, hook=None):
    """attendance_logs.find_one is wrapped: after reading, a thread can wait at a barrier (forcing both requests to have read
    the same version) and/or a hook can change the stored record, which is exactly what a concurrent writer would do."""
    seen, calls = set(), []

    class Coll:
        def __init__(self, c):
            self.c = c

        def find_one(self, *args, **kwargs):
            doc = self.c.find_one(*args, **kwargs)
            calls.append(1)
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


def test_two_corrections_that_read_the_same_version_cannot_both_succeed(client, db, monkeypatch):
    seed_employee(db)
    seed(db, history=[SAMPLE_8_ENTRY])
    monkeypatch.setattr(m, "db", _wrapped_db(db, threading.Barrier(2)))
    outs = ["2026-07-06T14:00:00Z", "2026-07-06T14:30:00Z"]
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda o: patch(client, punch_out=o, reason=f"set out {o}"), outs))
    assert sorted(r.status_code for r in results) == [200, 409]
    loser = next(r for r in results if r.status_code == 409)
    assert isinstance(loser.json()["detail"], str)
    winner = next(r for r in results if r.status_code == 200)
    record = stored(db)
    assert len(record["history"]) == 2 and record["history"][0] == SAMPLE_8_ENTRY  # old entry kept, exactly one new entry
    assert record["history"][1]["reason"] == winner.json()["history"][1]["reason"]  # the entry belongs to the winner
    assert m.dt_to_ms(record["punch_out"]) == winner.json()["punch_out"]  # the record holds the winner's values, whole
    assert record["work_hours"] == winner.json()["work_hours"] and record["overtime_minutes"] == winner.json()["overtime_minutes"]


def test_a_correction_that_lands_between_read_and_write_makes_the_second_one_409(client, db, monkeypatch):
    seed_employee(db)
    seed(db)
    rival = {"at": dt("2026-07-08T05:00:00Z"), "by": "someone.else", "reason": "rival correction", "changes": {"status": {"from": "PRESENT", "to": "WFH"}}}

    def rival_correction(coll, doc, call_number):
        if call_number == 1:
            coll.update_one({"_id": doc["_id"]}, {"$set": {"status": "WFH"}, "$push": {"history": rival}})

    monkeypatch.setattr(m, "db", _wrapped_db(db, None, rival_correction))
    r = patch(client, punch_out="2026-07-06T14:00:00Z")
    assert r.status_code == 409
    record = stored(db)
    assert record["history"] == [rival] and record["punch_out"] == dt("2026-07-06T13:40:00Z") and record["status"] == "WFH"  # nothing lost or mixed


def test_a_punch_out_that_lands_between_read_and_write_makes_the_correction_409(client, db, monkeypatch):
    seed_employee(db)
    seed_open(db)

    def punch_out_lands(coll, doc, call_number):
        if call_number == 1:
            coll.update_one({"_id": doc["_id"]}, {"$set": {"punch_out": dt("2026-07-06T13:00:00Z"), "work_hours": 9.0, "overtime_minutes": 0}})

    monkeypatch.setattr(m, "db", _wrapped_db(db, None, punch_out_lands))
    assert patch(client, status="WFH").status_code == 409
    record = stored(db)
    assert record["status"] == "PRESENT" and record["punch_out"] == dt("2026-07-06T13:00:00Z") and record["history"] == []


def test_a_loser_can_retry_and_succeeds_against_the_new_version(client, db, monkeypatch):
    seed_employee(db)
    seed(db)

    def rival(coll, doc, call_number):
        if call_number == 1:
            coll.update_one({"_id": doc["_id"]}, {"$push": {"history": {"at": dt("2026-07-08T05:00:00Z"), "by": "x", "reason": "rival", "changes": {}}}})

    monkeypatch.setattr(m, "db", _wrapped_db(db, None, rival))
    assert patch(client, status="WFH").status_code == 409
    monkeypatch.setattr(m, "db", db)  # no more interference
    r = patch(client, status="WFH")
    assert r.status_code == 200 and len(r.json()["history"]) == 2


def _chain_is_intact(history, original_out_ms):
    outs = [h["changes"]["punch_out"] for h in history if "punch_out" in h["changes"]]
    assert outs[0]["from"] == original_out_ms
    assert all(outs[i]["from"] == outs[i - 1]["to"] for i in range(1, len(outs)))  # each correction started from the previous result


def test_twenty_identical_simultaneous_corrections_yield_one_200_and_one_entry(client, db):
    seed_employee(db)
    seed(db)
    n = 20
    barrier = threading.Barrier(n)

    def go(_):
        barrier.wait()
        return patch(client, punch_out="2026-07-06T14:00:00Z").status_code

    with ThreadPoolExecutor(n) as pool:
        codes = list(pool.map(go, range(n)))
    assert codes.count(200) == 1 and set(codes) <= {200, 409, 422}, codes  # late arrivals see a no-op (422), early ones a conflict (409)
    assert len(stored(db)["history"]) == 1


def test_no_audit_entry_is_ever_lost_under_competing_corrections(client, db):
    """Invariant: the number of 200 responses equals the number of history entries, and the entries chain from -> to."""
    seed_employee(db)
    seed(db)
    n = 12
    barrier = threading.Barrier(n)

    def go(i):
        barrier.wait()
        return patch(client, punch_out=f"2026-07-06T{14 + i // 60:02d}:{i % 60:02d}:30Z", reason=f"correction {i:02d}").status_code

    with ThreadPoolExecutor(n) as pool:
        codes = list(pool.map(go, range(n)))
    history = stored(db)["history"]
    assert len(history) == codes.count(200) >= 1, codes
    assert set(codes) <= {200, 409}
    api_history = client.get("/attendance?emp_code=EMP0001").json()["items"][0]["history"]
    _chain_is_intact(api_history, ms("2026-07-06T13:40:00Z"))
    assert m.dt_to_ms(stored(db)["punch_out"]) == api_history[-1]["changes"]["punch_out"]["to"]  # record = last winner's value


@pytest.mark.real_mongo
def test_real_mongodb_stress_no_lost_audit_entries(client, db):
    seed_employee(db)
    for round_no in range(20):
        date = (datetime(2026, 7, 1) + timedelta(days=round_no)).strftime("%Y-%m-%d")
        seed(db, date=date, punch_in=f"{date}T04:35:00Z", punch_out=f"{date}T13:40:00Z")
        barrier = threading.Barrier(8)

        def go(i, date=date, barrier=barrier):
            barrier.wait()
            return patch(client, date=date, punch_out=f"{date}T14:{i:02d}:30Z", reason=f"round {round_no} try {i}").status_code

        with ThreadPoolExecutor(8) as pool:
            codes = list(pool.map(go, range(8)))
        history = stored(db, date=date)["history"]
        assert len(history) == codes.count(200) >= 1 and set(codes) <= {200, 409}, (round_no, codes)
        outs = [h["changes"]["punch_out"] for h in history]
        assert outs[0]["from"] == dt(f"{date}T13:40:00Z") and all(outs[i]["from"] == outs[i - 1]["to"] for i in range(1, len(outs)))


# ================================================================= 10/11. interaction with the other endpoints
def test_punch_out_after_a_correction_still_works_and_writes_no_history(client, db):
    seed_employee(db)
    seed_open(db)
    assert patch(client, punch_in="2026-07-06T04:35:00Z").status_code == 200
    r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06T13:40:00Z")})
    assert r.status_code == 200 and len(r.json()["history"]) == 1  # only the correction is in the audit trail
    assert (r.json()["late_minutes"], r.json()["work_hours"]) == (35, 9.08)


def test_punch_in_then_correct_then_list_roundtrip(client, db):
    seed_employee(db)
    posted = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06T04:35:00Z")}).json()
    assert posted["history"] == []
    r = patch(client, punch_in="2026-07-06T03:58:00Z")
    assert r.status_code == 200 and r.json()["late_minutes"] == 0
    assert client.get("/attendance?emp_code=EMP0001").json()["items"] == [r.json()]


def test_response_equals_the_stored_record_through_get(client, db):
    seed_employee(db)
    seed(db)
    r = patch(client, status="ON_DUTY", punch_out="2026-07-06T14:10:00Z")
    assert r.status_code == 200 and client.get("/attendance").json()["items"] == [r.json()]
