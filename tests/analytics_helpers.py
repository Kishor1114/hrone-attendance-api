"""Shared builders for the analytics tests."""
from datetime import datetime, timezone

UTC = timezone.utc


def employee(adb, code="EMP0001", joined="2026-01-05", department="Engineering", name=None):
    doc = {"emp_code": code, "name": name or f"Emp {code}", "email": f"{code.lower()}@example.com", "department": department,
           "shift_start": "09:30", "shift_end": "18:30", "joined_on": joined, "created_at": datetime(2026, 1, 5, 4, 30, tzinfo=UTC)}
    adb.employees.insert_one(doc)
    return doc


def log(adb, code, date, status="PRESENT", late=0, overtime=0, half=False, hours=None, **extra):
    doc = {"emp_code": code, "date": date, "status": status, "punch_in": None, "punch_out": None, "work_hours": hours,
           "late_minutes": late, "overtime_minutes": overtime, "half_day": half, "history": []}
    doc.update(extra)
    adb.attendance_logs.insert_one(doc)
    return doc
