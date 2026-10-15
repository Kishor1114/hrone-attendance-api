"""Contract conformance: live responses of the implemented endpoints are validated against the schemas in the
supplied openapi.yaml (the authoritative contract). Needs jsonschema and PyYAML (requirements-dev.txt)."""
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as m

yaml = pytest.importorskip("yaml")
jsonschema = pytest.importorskip("jsonschema")

SPEC_PATH = Path(__file__).resolve().parent.parent / "openapi.yaml"
UTC = timezone.utc


def _to_json_schema(node):
    """OpenAPI 3.0 -> JSON Schema: 'nullable: true' becomes a type union; a bare {nullable: true} accepts anything."""
    if isinstance(node, list):
        return [_to_json_schema(n) for n in node]
    if not isinstance(node, dict):
        return node
    out = {k: _to_json_schema(v) for k, v in node.items() if k != "nullable"}
    if node.get("nullable") is True:
        if "type" in out:
            out["type"] = [out["type"], "null"]
        elif not out:
            return {}
    return out


@pytest.fixture(scope="module")
def components():
    spec = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))
    return _to_json_schema(spec["components"])


@pytest.fixture
def check(components):
    def run(name, payload):
        if name in components["schemas"]:
            schema = {"$ref": f"#/components/schemas/{name}", "components": components}
        else:  # Error / ValidationError are reusable *responses* in the contract; use their JSON body schema
            schema = {**components["responses"][name]["content"]["application/json"]["schema"], "components": components}
        jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(payload)
    return run


@pytest.fixture
def client(db):
    with TestClient(m.app) as c:
        yield c


def ms(iso):
    return m.dt_to_ms(datetime.fromisoformat(iso.replace("Z", "+00:00")))


def test_validator_is_not_vacuous(check):
    good = {"emp_code": "EMP0001", "name": "A", "email": "a@b.co", "department": "D", "shift_start": "09:30",
            "shift_end": "18:30", "joined_on": "2026-01-05", "created_at": 1783312500000}
    check("Employee", good)
    for bad in ({**good, "created_at": "2026-01-05T00:00:00"}, {**good, "created_at": 5}, {**good, "joined_on": "2026-02-30"},
                {k: v for k, v in good.items() if k != "created_at"}):
        with pytest.raises(jsonschema.ValidationError):
            check("Employee", bad)
    with pytest.raises(jsonschema.ValidationError):  # the starter's leaked 'id' / ISO punch_in would violate this
        check("AttendanceRecord", {"emp_code": "E", "date": "2026-07-06", "status": "PRESENT", "punch_in": "2026-07-06T04:35:00",
                                   "punch_out": None, "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []})


def test_error_schemas_are_checked_too(check):
    check("Error", {"detail": "employee not found"})
    check("ValidationError", {"detail": [{"loc": ["body"], "msg": "x", "type": "y"}]})
    with pytest.raises(jsonschema.ValidationError):
        check("Error", {"detail": [{"msg": "list where the contract wants a string"}]})
    with pytest.raises(jsonschema.ValidationError):
        check("ValidationError", {"detail": "string where the contract wants a list"})


def test_employee_endpoints_conform(client, check):
    r = client.post("/employees", json={"emp_code": "EMP0001", "name": "Asha", "email": "asha@example.com",
                                        "department": "Engineering", "joined_on": "2026-01-05"})
    assert r.status_code == 201
    check("Employee", r.json())
    r = client.post("/employees", json={"emp_code": "EMP0001", "name": "Dup", "email": "d@example.com",
                                        "department": "Engineering", "joined_on": "2026-01-05"})
    assert r.status_code == 409
    check("Error", r.json())
    r = client.post("/employees", json={"emp_code": "bad"})
    assert r.status_code == 422
    check("ValidationError", r.json())
    check("EmployeePage", client.get("/employees").json())
    check("ValidationError", client.get("/employees?page=0").json())


def test_attendance_endpoints_conform(client, db, check):
    db.employees.insert_one({"emp_code": "EMP0001", "name": "Asha", "email": "a@b.co", "department": "Engineering",
                             "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-05",
                             "created_at": datetime(2026, 1, 5, 4, 30, tzinfo=UTC)})
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06T04:35:00Z")})
    assert r.status_code == 201
    check("AttendanceRecord", r.json())
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06T05:00:00Z")})
    assert r.status_code == 409
    check("Error", r.json())
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP9999"})
    assert r.status_code == 404
    check("Error", r.json())
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1.5, "status": "ABSENT"})
    assert r.status_code == 422
    check("ValidationError", r.json())
    # a regularized record with history, a legacy record, an ABSENT record and an open record, all through GET /attendance
    db.attendance_logs.insert_many([
        {"emp_code": "EMP0001", "date": "2026-07-07", "status": "PRESENT", "punch_in": datetime(2026, 7, 7, 3, 58, tzinfo=UTC),
         "punch_out": datetime(2026, 7, 7, 13, 0, tzinfo=UTC), "work_hours": 9.03, "late_minutes": 0, "overtime_minutes": 0,
         "half_day": False, "history": [{"at": datetime(2026, 7, 8, 5, 0, tzinfo=UTC), "by": "hr", "reason": "fix",
                                         "changes": {"punch_in": {"from": datetime(2026, 7, 7, 4, 35, tzinfo=UTC),
                                                                  "to": datetime(2026, 7, 7, 3, 58, tzinfo=UTC)},
                                                     "late_minutes": {"from": 35, "to": 0}}}]},
        {"emp_code": "EMP0001", "date": "2026-07-14", "status": "PRESENT", "punch_in": datetime(2026, 7, 14, 4, 2, tzinfo=UTC),
         "punch_out": datetime(2026, 7, 14, 13, 1, tzinfo=UTC), "work_hours": 8.98, "late_minutes": 0, "overtime_minutes": 0},
        {"emp_code": "EMP0001", "date": "2026-07-15", "status": "ABSENT", "punch_in": None, "punch_out": None, "work_hours": None,
         "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []},
    ])
    page = client.get("/attendance?page_size=100").json()
    assert page["total"] == 4
    check("AttendancePage", page)
    check("ValidationError", client.get("/attendance?date_from=2026-07-10&date_to=2026-07-01").json())


def test_health_conforms(client, check, monkeypatch):
    assert client.get("/health").json() == {"status": "ok"}
    from pymongo import MongoClient

    monkeypatch.setattr(m, "db", MongoClient("mongodb://localhost:1", tz_aware=True, serverSelectionTimeoutMS=300)["x"])
    r = TestClient(m.app).get("/health")
    assert r.status_code == 503
    check("Error", r.json())


def test_punch_out_conforms(client, db, check):
    emp = {"emp_code": "EMP0001", "name": "Asha", "email": "a@b.co", "department": "Engineering", "shift_start": "09:30",
           "shift_end": "18:30", "joined_on": "2026-01-05", "created_at": datetime(2026, 1, 5, 4, 30, tzinfo=UTC)}
    db.employees.insert_one(emp)
    db.attendance_logs.insert_one({"emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT", "punch_in": datetime(2026, 7, 6, 4, 35, tzinfo=UTC),
                                   "punch_out": None, "work_hours": None, "late_minutes": 35, "overtime_minutes": 0, "half_day": False, "history": []})
    r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06T13:40:00Z")})
    assert r.status_code == 200
    check("AttendanceRecord", r.json())
    r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06T14:00:00Z")})
    assert r.status_code == 409
    check("Error", r.json())
    r = client.post("/attendance/punch-out", json={"emp_code": "EMP9999"})
    assert r.status_code == 404
    check("Error", r.json())
    db.attendance_logs.insert_one({"emp_code": "EMP0001", "date": "2026-07-07", "status": "PRESENT", "punch_in": datetime(2026, 7, 7, 4, 0, tzinfo=UTC),
                                   "punch_out": None, "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []})
    r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-07T04:00:00Z")})
    assert r.status_code == 422
    check("ValidationError", r.json())
    r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": 1.5})
    assert r.status_code == 422
    check("ValidationError", r.json())


def test_patch_conforms_including_nested_history(client, db, check):
    db.employees.insert_one({"emp_code": "EMP0001", "name": "Asha", "email": "a@b.co", "department": "Engineering", "shift_start": "09:30",
                             "shift_end": "18:30", "joined_on": "2026-01-05", "created_at": datetime(2026, 1, 5, 4, 30, tzinfo=UTC)})
    db.attendance_logs.insert_one({"emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT", "punch_in": datetime(2026, 7, 6, 4, 35, tzinfo=UTC),
                                   "punch_out": datetime(2026, 7, 6, 13, 0, tzinfo=UTC), "work_hours": 8.42, "late_minutes": 35, "overtime_minutes": 0,
                                   "half_day": False, "history": []})
    r = client.patch("/attendance/EMP0001/2026-07-06", json={"punch_in": ms("2026-07-06T03:58:00Z"), "reason": "biometric glitch", "regularized_by": "hr.admin"})
    assert r.status_code == 200
    check("AttendanceRecord", r.json())  # history[].at and changes.punch_in.from/to must be epoch-ms integers per the schema
    assert isinstance(r.json()["history"][0]["changes"]["punch_in"]["from"], int)
    r = client.patch("/attendance/EMP0001/2026-07-06", json={"status": "ABSENT", "reason": "marked absent", "regularized_by": "hr.admin"})
    assert r.status_code == 200
    check("AttendanceRecord", r.json())  # null punch times and nullable from/to inside history
    assert r.json()["history"][1]["changes"]["punch_in"]["to"] is None
    r = client.patch("/attendance/EMP9999/2026-07-06", json={"status": "WFH", "reason": "remote work", "regularized_by": "hr.admin"})
    assert r.status_code == 404
    check("Error", r.json())
    r = client.patch("/attendance/EMP0001/2026-07-07", json={"status": "WFH", "reason": "remote work", "regularized_by": "hr.admin"})
    assert r.status_code == 404
    check("Error", r.json())
    for body in ({"reason": "no changes here", "regularized_by": "hr.admin"},  # no-op
                 {"status": "LEAVE", "punch_in": ms("2026-07-06T04:00:00Z"), "reason": "leave day", "regularized_by": "hr.admin"},
                 {"status": "WFH", "reason": "abc", "regularized_by": "hr.admin"}):
        r = client.patch("/attendance/EMP0001/2026-07-06", json=body)
        assert r.status_code == 422
        check("ValidationError", r.json())
