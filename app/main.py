"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real environment variables win)

Layout of this file (everything lives here, as the assignment requires):
  1. Configuration and MongoDB client
  2. Time helpers      (epoch ms <-> datetime, IST dates, whole-second truncation)
  3. Rounding helper   (half-up, never float/banker's rounding)
  4. Business rules    (R1-R5: attendance date, late, overtime, work hours, half day)
  5. Validation types  (strict epoch ms, real calendar dates, months, statuses, pagination)
  6. Serializers       (Mongo document -> API JSON; defaults for legacy documents)
  7. Indexes           (created at startup; startup fails loudly if a unique index cannot exist)
  8. Models and endpoints
  9. Analytics (aggregation pipelines)
 10. Admin explain (proves the queries above use indexes)
"""
import calendar
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from bson.son import SON
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, Field, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import AutoReconnect, DuplicateKeyError, OperationFailure, PyMongoError

# --------------------------------------------------------------------------- #
# 1. Configuration and MongoDB client
# --------------------------------------------------------------------------- #
load_dotenv()  # override=False (the default): real environment variables take priority over .env

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# tz_aware=True: PyMongo returns UTC-aware datetimes instead of naive ones, so a naive value can never
# be mistaken for local time. A short server-selection timeout keeps /health and startup from hanging
# for PyMongo's 30 s default when MongoDB is down.
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=3000)
db = client[MONGO_DB]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))


# --------------------------------------------------------------------------- #
# 2. Time helpers
#    API instants are epoch MILLISECONDS; MongoDB stores BSON datetimes (UTC); calendar values are strings.
# --------------------------------------------------------------------------- #
EPOCH_MS_MIN = 100_000_000_000  # below this the number looks like seconds -> rejected (422)
EPOCH_MS_MAX = 4_102_444_800_000  # 2100-01-01T00:00:00Z


def as_utc(dt: datetime) -> datetime:
    """A datetime in UTC. A naive datetime is interpreted as UTC (what MongoDB does with naive values)."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def to_ist(dt: datetime) -> datetime:
    return as_utc(dt).astimezone(IST)


def truncate_seconds(dt: datetime) -> datetime:
    """R1: instants are truncated to whole seconds before any calculation. The result is always UTC-aware, so
    two instants can be subtracted whatever zone (or naive-as-UTC) they arrived in."""
    return as_utc(dt).replace(microsecond=0)


def utc_now() -> datetime:
    """Server clock, UTC-aware, millisecond precision (BSON datetimes cannot hold more)."""
    now = datetime.now(UTC)
    return now.replace(microsecond=now.microsecond // 1000 * 1000)


def ms_to_instant(ms: int) -> datetime:
    """Request epoch ms -> UTC datetime truncated to whole seconds (R1). Integer arithmetic only."""
    return datetime.fromtimestamp(ms // 1000, UTC)


def dt_to_ms(dt: datetime) -> int:
    """Stored datetime -> epoch ms, exact (no float arithmetic)."""
    dt = as_utc(dt)
    return calendar.timegm(dt.timetuple()) * 1000 + dt.microsecond // 1000


def ms_or_none(value: Optional[datetime]) -> Optional[int]:
    return None if value is None else dt_to_ms(value)


# Explicit [0-9] (not \d, which also matches Arabic-Indic / full-width digits) and fullmatch (not match with $,
# which tolerates a trailing newline): a value that validates must be byte-identical to what is stored.
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_MONTH_RE = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])")
HHMM_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"  # anchored: pydantic applies patterns as an unanchored search


def parse_date(value: str) -> date:
    """Strict YYYY-MM-DD that must be a real calendar date ('2026-02-30' and '2026-7-1' are rejected)."""
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise ValueError("must be a date in YYYY-MM-DD format")
    try:
        return date(int(value[:4]), int(value[5:7]), int(value[8:10]))
    except ValueError:
        raise ValueError("must be a real calendar date")


def check_date(value: str) -> str:
    parse_date(value)
    return value


def check_month(value: str) -> str:
    if not isinstance(value, str) or not _MONTH_RE.fullmatch(value):
        raise ValueError("must be a month in YYYY-MM format")
    return value


def month_bounds(month: str) -> tuple[str, str, str]:
    """('YYYY-MM-01', first day of next month, last day of the month). Dates are zero-padded strings, so
    plain string comparison orders them correctly: `first <= date < next_first` selects the month."""
    check_month(month)
    year, mon = int(month[:4]), int(month[5:7])
    last = calendar.monthrange(year, mon)[1]
    nyear, nmon = (year + 1, 1) if mon == 12 else (year, mon + 1)
    return f"{year:04d}-{mon:02d}-01", f"{nyear:04d}-{nmon:02d}-01", f"{year:04d}-{mon:02d}-{last:02d}"


def is_weekday(date_str: str) -> bool:
    """R7 working day: Monday-Friday, no holiday calendar."""
    return parse_date(date_str).weekday() < 5


# --------------------------------------------------------------------------- #
# 3. Rounding (R4, R8): half-up on the exact value, never Python's round() (binary float, half-even)
# --------------------------------------------------------------------------- #
def round_half_up(value: Any, places: int = 2) -> float:
    """Round half away from zero at `places` decimals.

    Fraction and int are exact. A float is first cleaned of binary noise (rounded to 9 places through its
    shortest repr), so 8.014999999999999 produced by an average is treated as the 8.015 it really is.
    """
    if isinstance(value, float):
        value = Decimal(repr(value)).quantize(Decimal("1e-9"), rounding=ROUND_HALF_UP)
    if isinstance(value, Decimal):
        value = Fraction(value)
    scaled = Fraction(value) * 10**places
    rounded = int(abs(scaled) + Fraction(1, 2))  # int() truncates; abs + 1/2 gives half-up
    return (-rounded if scaled < 0 else rounded) / 10**places


# --------------------------------------------------------------------------- #
# 4. Business rules R1-R5. Pure functions: datetimes in, numbers out, no database access.
#    All of them accept any datetime (aware or naive-as-UTC) and work in IST internally.
# --------------------------------------------------------------------------- #
GRACE_SECONDS = 10 * 60  # R2
OVERTIME_MIN_MINUTES = 30  # R3
HALF_DAY_LIMIT_HOURS = 4.5  # R5
PRESENCE_STATUSES = frozenset({"PRESENT", "WFH", "ON_DUTY"})  # R6


def hhmm_to_minutes(value: str) -> int:
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return hhmm_to_minutes(shift_end) <= hhmm_to_minutes(shift_start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: the IST calendar date of the punch-in. For an overnight shift, a punch-in whose IST time is
    earlier than shift_end belongs to the shift that started the previous day."""
    ist = to_ist(truncate_seconds(punch_in))
    day = ist.date()
    seconds_of_day = ist.hour * 3600 + ist.minute * 60 + ist.second
    if is_overnight(shift_start, shift_end) and seconds_of_day < hhmm_to_minutes(shift_end) * 60:
        day -= timedelta(days=1)
    return day.isoformat()


def shift_start_at(att_date: str, shift_start: str) -> datetime:
    return datetime.combine(parse_date(att_date), datetime.min.time(), IST) + timedelta(
        minutes=hhmm_to_minutes(shift_start)
    )


def shift_end_at(att_date: str, shift_start: str, shift_end: str) -> datetime:
    """The shift end of an overnight shift falls on the next calendar day (R1)."""
    end = datetime.combine(parse_date(att_date), datetime.min.time(), IST) + timedelta(
        minutes=hhmm_to_minutes(shift_end)
    )
    return end + timedelta(days=1) if is_overnight(shift_start, shift_end) else end


def compute_late_minutes(
    punch_in: datetime, shift_start: str, shift_end: str, att_date: Optional[str] = None
) -> int:
    """R2: late only if STRICTLY more than 10:00 after shift_start; then whole minutes (floor) since
    shift_start (not since the end of the grace period). Shift 09:30: 09:40:00 -> 0, 09:40:01 -> 10."""
    punch_in = truncate_seconds(punch_in)
    att_date = att_date or attendance_date(punch_in, shift_start, shift_end)
    seconds = int((punch_in - shift_start_at(att_date, shift_start)).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_overtime(
    punch_out: datetime, shift_start: str, shift_end: str, att_date: str
) -> int:
    """R3: whole minutes (floor) between shift_end and punch_out, counted only when that is >= 30."""
    punch_out = truncate_seconds(punch_out)
    seconds = int((punch_out - shift_end_at(att_date, shift_start, shift_end)).total_seconds())
    minutes = max(0, seconds // 60)
    return minutes if minutes >= OVERTIME_MIN_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: (punch_out - punch_in) seconds / 3600, rounded to 2 decimals half-up, on exact integers."""
    seconds = int((truncate_seconds(punch_out) - truncate_seconds(punch_in)).total_seconds())
    return round_half_up(Fraction(seconds, 3600), 2)


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: after punch-out, half day when the ROUNDED work_hours is below 4.50."""
    return work_hours is not None and work_hours < HALF_DAY_LIMIT_HOURS


def derive_fields(
    status: str,
    punch_in: Optional[datetime],
    punch_out: Optional[datetime],
    shift_start: str,
    shift_end: str,
    att_date: str,
) -> dict:
    """The four stored derived fields, for any record state (DATA_MODEL.md section 3 table).
    One function used by punch-in, punch-out and PATCH so the three can never disagree."""
    if status not in PRESENCE_STATUSES or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in, shift_start, shift_end, att_date)
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    work_hours = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": work_hours,
        "late_minutes": late,
        "overtime_minutes": compute_overtime(punch_out, shift_start, shift_end, att_date),
        "half_day": compute_half_day(work_hours),
    }


# --------------------------------------------------------------------------- #
# 5. Validation types (reused by request models and query parameters)
# --------------------------------------------------------------------------- #
# strict=True: JSON floats (1783312500000.0), strings ("1783312500000") and booleans are rejected, and the
# lower bound rejects values that look like seconds. All of it is a 422 from FastAPI.
EpochMs = Annotated[int, Field(strict=True, ge=EPOCH_MS_MIN, le=EPOCH_MS_MAX)]
DateStr = Annotated[str, AfterValidator(check_date)]
MonthStr = Annotated[str, AfterValidator(check_month)]
HHMM = Annotated[str, Field(pattern=HHMM_PATTERN)]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
PageParam = Annotated[int, Query(ge=1)]
PageSizeParam = Annotated[int, Query(ge=1, le=100)]


# --------------------------------------------------------------------------- #
# 6. Serializers: the only place a MongoDB document becomes API JSON.
#    Never emits _id or id; converts every instant (including nested history values) to epoch ms;
#    fills in defaults for legacy documents that omit fields.
# --------------------------------------------------------------------------- #
_HISTORY_TIME_FIELDS = ("punch_in", "punch_out")


def serialize_history_entry(entry: dict) -> dict:
    changes = {}
    for field, change in (entry.get("changes") or {}).items():
        convert = ms_or_none if field in _HISTORY_TIME_FIELDS else (lambda v: v)
        changes[field] = {"from": convert(change.get("from")), "to": convert(change.get("to"))}
    return {
        "at": ms_or_none(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes,
    }


def serialize_attendance(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": ms_or_none(doc.get("punch_in")),
        "punch_out": ms_or_none(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [serialize_history_entry(e) for e in (doc.get("history") or [])],
    }


def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": ms_or_none(doc.get("created_at")),
    }


# --------------------------------------------------------------------------- #
# 7. Indexes. Created at startup, idempotently (same name + same definition = no-op).
# --------------------------------------------------------------------------- #
INDEXES: dict[str, list[tuple[list[tuple[str, int]], dict]]] = {
    "employees": [
        # uniqueness of emp_code (race-safe create), $lookup joins, list sorted by emp_code
        ([("emp_code", ASCENDING)], {"name": "emp_code_unique", "unique": True}),
        # GET /employees?department=..: filter + emp_code sort
        ([("department", ASCENDING), ("emp_code", ASCENDING)], {"name": "department_emp_code"}),
        # headcount (R9): department filter + joined_on range, and the unfiltered joined_on range
        ([("department", ASCENDING), ("joined_on", ASCENDING)], {"name": "department_joined_on"}),
        ([("joined_on", ASCENDING), ("department", ASCENDING)], {"name": "joined_on_department"}),
    ],
    "attendance_logs": [
        # one record per (emp_code, date): the punch-in race guarantee; employee monthly; summary join
        ([("emp_code", ASCENDING), ("date", ASCENDING)], {"name": "emp_code_date_unique", "unique": True}),
        # punch-out: most recent record of an employee whose punch_in <= punched_at
        ([("emp_code", ASCENDING), ("punch_in", DESCENDING)], {"name": "emp_code_punch_in_desc"}),
        # GET /attendance default sort (date desc, emp_code asc) and month/range scans
        ([("date", DESCENDING), ("emp_code", ASCENDING)], {"name": "date_desc_emp_code"}),
    ],
}


class IndexSetupError(RuntimeError):
    """Startup must not continue without the indexes that carry correctness guarantees."""


def _describe_duplicates(database, collection: str, keys: list[tuple[str, int]]) -> str:
    fields = [k for k, _ in keys]
    pipeline = [
        {"$group": {"_id": {f: f"${f}" for f in fields}, "n": {"$sum": 1}}},
        {"$match": {"n": {"$gt": 1}}},
        {"$limit": 5},
    ]
    try:
        dups = list(database[collection].aggregate(pipeline))
    except PyMongoError:
        return "could not list duplicates"
    if not dups:
        return "no duplicate keys found by the diagnostic query"
    return "duplicate keys, e.g. " + "; ".join(f"{d['_id']} x{d['n']}" for d in dups)


def ensure_indexes(database) -> None:
    """Create every index in INDEXES. Raises IndexSetupError (startup fails) on any failure; a unique
    index that cannot be built is never skipped, because the race guarantees depend on it."""
    for collection, specs in INDEXES.items():
        for keys, options in specs:
            try:
                database[collection].create_index(keys, **options)
            except OperationFailure as exc:
                detail = ""
                if options.get("unique"):
                    detail = " Cause: " + _describe_duplicates(database, collection, keys) + "."
                raise IndexSetupError(
                    f"Cannot create index '{options['name']}' on {database.name}.{collection} "
                    f"({exc.details.get('codeName', 'OperationFailure') if exc.details else 'OperationFailure'}: {exc}).{detail}"
                    " Startup aborted; fix the data or the conflicting index and restart."
                ) from exc
            except PyMongoError as exc:
                raise IndexSetupError(
                    f"MongoDB is not reachable while creating index '{options['name']}' on "
                    f"{database.name}.{collection} ({type(exc).__name__}). Check MONGO_URI / MONGO_DB."
                ) from exc


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_indexes(db)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# 8. Query builders, models and endpoints
# --------------------------------------------------------------------------- #
# Filters and sort orders live in one place so the future /admin/explain endpoint can run EXACTLY the query the
# endpoint runs.
EMPLOYEE_SORT = [("emp_code", ASCENDING)]
ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


def employee_filter(department: Optional[str]) -> dict:
    return {} if department is None else {"department": department}  # exact, case-sensitive


def attendance_filter(
    emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str], status: Optional[str]
) -> dict:
    query: dict = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        query["date"] = {}
        if date_from is not None:
            query["date"]["$gte"] = date_from  # inclusive; dates are zero-padded strings, so order == calendar order
        if date_to is not None:
            query["date"]["$lte"] = date_to
    if status is not None:
        query["status"] = status
    return query


class EmployeeCreate(BaseModel):
    # [0-9], not \d: OpenAPI patterns are ECMA-262 where \d is ASCII-only (Python/Rust \d also match other scripts)
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: HHMM = "09:30"
    shift_end: HHMM = "18:30"
    joined_on: DateStr

    @model_validator(mode="after")
    def _shifts_must_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


PUNCH_OUT_MAX_SECONDS = 24 * 3600  # a punch-out more than 24 hours after the punch-in is a 422


def validation_error(where: str, field: str, message: str, value: Any) -> RequestValidationError:
    """A 422 with FastAPI's standard array-shaped body, for rules that depend on stored data."""
    return RequestValidationError(
        [{"type": "value_error", "loc": (where, field), "msg": message, "input": value}]
    )


class RegularizeRequest(BaseModel):
    """Only these fields can be corrected. Derived fields sent by a client are ignored (unknown fields are dropped)."""

    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _editable_fields_cannot_be_null(self):
        for name in ("status", "punch_in", "punch_out"):  # the contract's schemas are non-nullable
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null; omit it to leave it unchanged")
        return self


# The seven fields a correction can touch or recompute, in the order the contract lists them for history.changes.
HISTORY_FIELDS = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")
ABSENCE_STATUSES = frozenset({"ABSENT", "LEAVE"})


def attendance_state(doc: dict) -> dict:
    """The seven correctable fields of a stored record, normalized: instants are UTC-aware and a field a legacy document
    omits has its documented default (late/overtime 0, half_day false). Used for 'what changed' and 'is it a no-op'."""
    return {
        "status": doc["status"],
        "punch_in": None if doc.get("punch_in") is None else as_utc(doc["punch_in"]),
        "punch_out": None if doc.get("punch_out") is None else as_utc(doc["punch_out"]),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
    }


@app.exception_handler(AutoReconnect)
async def _mongo_unreachable(_request, _exc):
    """Connectivity loss while serving a request (includes ServerSelectionTimeoutError) is 503, not a crash."""
    return JSONResponse(status_code=503, content={"detail": "MongoDB is not reachable"})


@app.get("/health")
def health():
    try:
        db.command("ping")
    except PyMongoError:
        raise HTTPException(503, "MongoDB is not reachable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = utc_now()
    try:
        db.employees.insert_one(doc)  # uniqueness is enforced by the unique index, not by a prior lookup
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: PageParam = 1, page_size: PageSizeParam = 20):
    query = employee_filter(department)
    total = db.employees.count_documents(query)  # the filtered set, not the whole collection
    skip = (page - 1) * page_size
    items = []
    if skip < total:  # also keeps an absurd page number from reaching the server as a huge skip
        cursor = db.employees.find(query).sort(EMPLOYEE_SORT).skip(skip).limit(page_size)
        items = [serialize_employee(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code}, {"shift_start": 1, "shift_end": 1})
    if emp is None:
        raise HTTPException(404, "employee not found")
    shift_start, shift_end = emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30")
    instant = ms_to_instant(body.punched_at) if body.punched_at is not None else truncate_seconds(utc_now())
    att_date = attendance_date(instant, shift_start, shift_end)  # R1: IST date, overnight rule
    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": instant,
        "punch_out": None,
        **derive_fields(body.status, instant, None, shift_start, shift_end, att_date),
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # the unique (emp_code, date) index decides who wins a race
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this attendance date")
    return serialize_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    """Close the employee's most recent record whose punch_in is at or before punched_at (this is what makes an
    overnight shift work: the punch-out lands on the next calendar day, the record keeps the day the shift began).

    Order of checks follows the contract: 404 (unknown employee / no punch-in found), 409 (record already closed),
    422 (punched_at <= punch_in, or more than 24 h after it). Only work_hours, overtime_minutes and half_day are
    written; late_minutes belongs to punch-in and history to manual corrections.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code}, {"shift_start": 1, "shift_end": 1})
    if emp is None:
        raise HTTPException(404, "employee not found")
    shift_start, shift_end = emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30")
    instant = ms_to_instant(body.punched_at) if body.punched_at is not None else truncate_seconds(utc_now())

    # The close below is one conditional update, so of two simultaneous punch-outs exactly one can win. If the
    # update finds the record changed since it was read (closed by a rival, or its punch_in corrected by a PATCH),
    # decide again from fresh data instead of writing numbers computed from a stale read.
    for _attempt in range(3):
        record = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$lte": instant}},  # ABSENT/LEAVE have no punch_in: never match
            sort=[("punch_in", DESCENDING)],
        )
        if record is None:
            raise HTTPException(404, "no punch-in found for this employee at or before punched_at")
        if record.get("punch_out") is not None:
            raise HTTPException(409, "already punched out")

        elapsed = int((instant - truncate_seconds(record["punch_in"])).total_seconds())
        if elapsed <= 0:
            raise validation_error("body", "punched_at", "punched_at must be after punch_in", body.punched_at)
        if elapsed > PUNCH_OUT_MAX_SECONDS:
            raise validation_error("body", "punched_at", "punched_at must be within 24 hours of punch_in", body.punched_at)

        work_hours = compute_work_hours(record["punch_in"], instant)  # R4, exact half-up
        closed = db.attendance_logs.find_one_and_update(
            {"_id": record["_id"], "punch_in": record["punch_in"], "punch_out": None},  # null also matches a missing field
            {"$set": {
                "punch_out": instant,
                "work_hours": work_hours,
                "overtime_minutes": compute_overtime(instant, shift_start, shift_end, record["date"]),  # R3, anchored on the record's own date
                "half_day": compute_half_day(work_hours),  # R5, from the rounded value
            }},
            return_document=ReturnDocument.AFTER,
        )
        if closed is not None:
            return serialize_attendance(closed)
    raise HTTPException(409, "record was modified concurrently; retry")


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: DateStr, body: RegularizeRequest):
    """Manually correct one day's record, addressed by (emp_code, date). Recomputes the derived fields (R2-R5) from the
    final times and appends exactly one history entry holding only the fields that actually changed.

    Order: 422 (request shape) -> 404 employee -> 404 record -> 422 (rules that need the stored record) -> 409.
    """
    emp = db.employees.find_one({"emp_code": emp_code}, {"shift_start": 1, "shift_end": 1})
    if emp is None:
        raise HTTPException(404, "employee not found")
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(404, "no attendance record for this employee and date")
    shift_start, shift_end = emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30")

    old = attendance_state(record)
    final_status = body.status if body.status is not None else old["status"]

    if final_status in ABSENCE_STATUSES:
        # ABSENT / LEAVE mean "no punches". Supplying a punch time while the record ends up ABSENT/LEAVE is
        # inconsistent whether the status came in this request or was already stored.
        for field in ("punch_in", "punch_out"):
            if getattr(body, field) is not None:
                raise validation_error("body", field, f"{field} cannot be set while status is {final_status}", getattr(body, field))
        final_in = final_out = None
    else:
        final_in = ms_to_instant(body.punch_in) if body.punch_in is not None else old["punch_in"]
        final_out = ms_to_instant(body.punch_out) if body.punch_out is not None else old["punch_out"]
        if final_in is None:
            raise validation_error("body", "punch_in", f"status {final_status} requires a punch_in", body.punch_in)
        if body.punch_in is not None and attendance_date(final_in, shift_start, shift_end) != date:  # R1
            raise validation_error("body", "punch_in", f"punch_in must fall on the record's attendance date {date}", body.punch_in)
        if final_out is not None:
            elapsed = int((truncate_seconds(final_out) - truncate_seconds(final_in)).total_seconds())
            culprit = "punch_out" if body.punch_out is not None else "punch_in"
            if elapsed <= 0:
                raise validation_error("body", culprit, "punch_out must be after punch_in", getattr(body, culprit))
            if elapsed > PUNCH_OUT_MAX_SECONDS:
                raise validation_error("body", culprit, "punch_out must be within 24 hours of punch_in", getattr(body, culprit))

    if (final_status, final_in, final_out) == (old["status"], old["punch_in"], old["punch_out"]):
        raise validation_error("body", "reason", "this correction changes nothing", None)

    new = {"status": final_status, "punch_in": final_in, "punch_out": final_out,
           **derive_fields(final_status, final_in, final_out, shift_start, shift_end, date)}  # R2-R5, anchored on the record's date
    changes = {f: {"from": old[f], "to": new[f]} for f in HISTORY_FIELDS if old[f] != new[f]}
    entry = {"at": utc_now(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Optimistic concurrency without adding a version field: the record is only updated if it is still exactly as it was
    # read. Every successful correction appends exactly one entry, so the history length is a version counter, and a
    # concurrent punch-out changes punch_out. A loser matches nothing and gets 409; no entry is ever overwritten or lost.
    history = record.get("history")
    still_as_read = {
        "_id": record["_id"],
        "status": record["status"],
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "history": {"$size": len(history)} if isinstance(history, list) else None,  # None matches missing or null
    }
    update: dict = {"$set": {f: new[f] for f in HISTORY_FIELDS}}
    if isinstance(history, list):
        update["$push"] = {"history": entry}  # append: existing entries are never touched
    else:
        update["$set"]["history"] = [entry]  # legacy record with no history (or null): nothing to preserve
    updated = db.attendance_logs.find_one_and_update(still_as_read, update, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "record was modified concurrently; retry")
    return serialize_attendance(updated)


def check_date_order(date_from: Optional[str], date_to: Optional[str]) -> None:
    if date_from is not None and date_to is not None and date_from > date_to:
        raise RequestValidationError(  # 422 with FastAPI's standard array-shaped body, as the contract specifies
            [{"type": "value_error", "loc": ("query", "date_from"), "msg": "date_from must not be after date_to",
              "input": date_from}]
        )


def attendance_find_spec(emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str], status: Optional[str],
                         page: int, page_size: int) -> dict:
    """Filter, sort, skip and limit of GET /attendance. ONE definition drives the endpoint's query and the explain endpoint's,
    so the explained plan is the plan the endpoint really runs."""
    return {"filter": attendance_filter(emp_code, date_from, date_to, status), "sort": ATTENDANCE_SORT,
            "skip": (page - 1) * page_size, "limit": page_size}


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    page: PageParam = 1,
    page_size: PageSizeParam = 20,
):
    check_date_order(date_from, date_to)
    spec = attendance_find_spec(emp_code, date_from, date_to, status, page, page_size)
    total = db.attendance_logs.count_documents(spec["filter"])
    items = []
    if spec["skip"] < total:
        cursor = db.attendance_logs.find(spec["filter"]).sort(spec["sort"]).skip(spec["skip"]).limit(spec["limit"])  # database-side
        items = [serialize_attendance(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# 9. Analytics. Every figure is computed by a MongoDB aggregation pipeline from the STORED values (the stored derived fields
#    are authoritative; nothing is recomputed from punch times). Python only supplies calendar parameters (which dates of a
#    month are Mon-Fri) and applies the contract's half-up rounding to the few numbers the pipelines return, because
#    MongoDB's $round rounds halves to even.
# --------------------------------------------------------------------------- #
PRESENCE_LIST = sorted(PRESENCE_STATUSES)  # PRESENT, WFH, ON_DUTY (R6)


def presence_weight() -> dict:
    """R5/R6: a presence-status record is worth 1 day, a stored half_day 0.5; anything else 0. A legacy record without
    half_day counts as a full day."""
    return {"$cond": [{"$in": ["$status", PRESENCE_LIST]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}


def late_flag() -> dict:
    """1 for a record with late_minutes > 0 (a missing value counts as 0)."""
    return {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}


def month_weekdays(first: str, next_first: str) -> list[str]:
    """The Mon-Fri dates in [first, next_first): a calendar parameter for the pipelines (R7: no holiday calendar)."""
    day, end, dates = parse_date(first), parse_date(next_first), []
    while day < end:
        if day.weekday() < 5:
            dates.append(day.isoformat())
        day += timedelta(days=1)
    return dates


def month_stats_group(weekday_dates: list[str]) -> dict:
    """One $group over a set of attendance records producing every monthly figure. present_days counts Mon-Fri records only
    (R7); leave/late/overtime/on-duty figures count records on any day (the contract's literal definitions)."""
    has_hours = {"$and": [{"$in": ["$status", PRESENCE_LIST]}, {"$ne": [{"$ifNull": ["$work_hours", None]}, None]}]}
    return {"$group": {
        "_id": None,
        "present_days": {"$sum": {"$cond": [{"$in": ["$date", weekday_dates]}, presence_weight(), 0]}},
        "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
        "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        "late_count": {"$sum": late_flag()},
        "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
        "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        "hours_sum": {"$sum": {"$cond": [has_hours, "$work_hours", 0]}},
        "hours_count": {"$sum": {"$cond": [has_hours, 1, 0]}},
    }}


def employee_monthly_pipeline(emp_code: str, month: str) -> list[dict]:
    first, next_first, _last = month_bounds(month)
    return [
        # served by the unique (emp_code, date) index: one equality, one date range
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lt": next_first}}},
        month_stats_group(month_weekdays(first, next_first)),
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthStr):
    emp = db.employees.find_one({"emp_code": emp_code}, {"joined_on": 1})
    if emp is None:
        raise HTTPException(404, "employee not found")
    first, next_first, _last = month_bounds(month)
    weekdays = month_weekdays(first, next_first)
    stats = next(iter(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, month))), None) or {}
    working_days = sum(1 for d in weekdays if d >= emp.get("joined_on", ""))  # R7: counted from joined_on
    present = Fraction(stats.get("present_days", 0))  # a multiple of 0.5: exact in binary
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": float(present),
        "leave_days": int(stats.get("leave_count", 0)),
        "late_count": int(stats.get("late_count", 0)),
        "total_late_minutes": int(stats.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(stats.get("total_overtime_minutes", 0)),
        "attendance_pct": None if working_days == 0 else round_half_up(present * 100 / working_days, 2),  # R8
    }


def first_of(path: str) -> dict:
    """The first element of an array produced by $lookup (0 when the array is empty): one value per employee."""
    return {"$ifNull": [{"$arrayElemAt": [path, 0]}, 0]}


def department_summary_pipeline(month: str, department: Optional[str] = None) -> list[dict]:
    """Starts from `employees`, not from the logs: headcount must include people with no logs at all (R9), which a pipeline
    that starts from the logs can never see. Each employee carries their month of logs (a correlated $lookup served by the
    (emp_code, date) index), and the final $group rolls them up per department."""
    first, next_first, last = month_bounds(month)
    employees_in_scope = {"joined_on": {"$lte": last}}  # R9: joined on or before the last day of the month
    if department is not None:
        employees_in_scope["department"] = department  # exact, case-sensitive
    return [
        {"$match": employees_in_scope},  # served by (department, joined_on) / (joined_on, department)
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code", "as": "m",
            "pipeline": [{"$match": {"date": {"$gte": first, "$lt": next_first}}}, month_stats_group(month_weekdays(first, next_first))],
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},  # one per employee, with or without logs
            "present_days": {"$sum": first_of("$m.present_days")},
            "late_count": {"$sum": first_of("$m.late_count")},
            "total_late_minutes": {"$sum": first_of("$m.total_late_minutes")},
            "leave_count": {"$sum": first_of("$m.leave_count")},
            "on_duty_count": {"$sum": first_of("$m.on_duty_count")},
            "hours_sum": {"$sum": first_of("$m.hours_sum")},  # summed over RECORDS, so the average below is not
            "hours_count": {"$sum": first_of("$m.hours_count")},  # an average of per-employee averages
        }},
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: MonthStr, department: Optional[str] = None):
    rows = list(db.employees.aggregate(department_summary_pipeline(month, department)))
    return {
        "month": month,
        "items": [
            {
                "department": row["_id"],
                "headcount": int(row["headcount"]),
                "present_days": float(row["present_days"]),
                "avg_work_hours": None if not row["hours_count"] else round_half_up(row["hours_sum"] / row["hours_count"], 2),  # R8
                "late_count": int(row["late_count"]),
                "total_late_minutes": int(row["total_late_minutes"]),
                "leave_count": int(row["leave_count"]),
                "on_duty_count": int(row["on_duty_count"]),
            }
            for row in rows  # one row per department (a handful), already sorted by the pipeline
        ],
    }


def late_leaderboard_pipeline(month: str, limit: int, department: Optional[str] = None) -> list[dict]:
    """Ranks employees by total late minutes in the month. The rank comes from a window function ($rank = standard competition
    ranking: ties share a rank and the next rank is skipped, 1, 2, 2, 4), NOT from a row's position after sorting. `limit` is
    applied to the rank, after ranking, so employees tied at a rank <= limit are all returned."""
    first, next_first, _last = month_bounds(month)
    pipeline: list[dict] = [
        # served by the (date, emp_code) index; only records with late_minutes > 0 can make a total > 0
        {"$match": {"date": {"$gte": first, "$lt": next_first}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},  # unique emp_code index
        {"$unwind": "$emp"},  # a log whose employee does not exist is dropped here
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  # BEFORE ranking: ranks are computed inside the department
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies to the rank, after ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},  # contract order: total descending, then emp_code ascending
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name", "department": "$emp.department",
                      "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: MonthStr, limit: Annotated[int, Query(ge=1, le=50)] = 10, department: Optional[str] = None):
    rows = list(db.attendance_logs.aggregate(late_leaderboard_pipeline(month, limit, department)))
    return {
        "month": month,
        "items": [
            {"rank": int(r["rank"]), "emp_code": r["emp_code"], "name": r["name"], "department": r["department"],
             "total_late_minutes": int(r["total_late_minutes"]), "late_count": int(r["late_count"])}
            for r in rows  # shaping only: ranking, limiting and ordering were all done by the pipeline
        ],
    }


TREND_MAX_DAYS = 92  # the range is inclusive: at most 92 calendar dates
MS_PER_DAY = 86_400_000


def department_trend_pipeline(department: str, start: date, days: int, employee_codes: list[str]) -> list[dict]:
    """One row per calendar day, generated INSIDE the database (no Python loop over days or documents).

    Seeded from `employees` (never empty once the department exists) rather than from the logs, so a range with no records at
    all still yields every day; $range + $unwind generate the day rows, a correlated $lookup attaches each day's record
    statistics (served by the (date, emp_code) index) and its headcount (R9, served by (department, joined_on)), and a window
    function computes the 7-row moving average over the rows of THIS range only."""
    first_day = datetime.combine(start, datetime.min.time(), UTC)
    return [
        {"$match": {"department": department}},  # (department, emp_code) index
        {"$limit": 1},  # any one employee of the department: only a seed document for the day rows
        {"$project": {"_id": 0, "i": {"$range": [0, days]}}},
        {"$unwind": "$i"},  # one document per day offset
        {"$addFields": {"dt": {"$add": [first_day, {"$multiply": ["$i", MS_PER_DAY]}]}}},
        {"$addFields": {"date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$dt"}}, "dow": {"$dayOfWeek": "$dt"}}},  # dow: 1 = Sunday
        {"$lookup": {  # record statistics of the department's employees on that date: any weekday or weekend
            "from": "attendance_logs", "localField": "date", "foreignField": "date", "as": "s",
            "pipeline": [
                {"$match": {"emp_code": {"$in": employee_codes}}},
                {"$group": {"_id": None, "present": {"$sum": presence_weight()}, "late": {"$sum": late_flag()}}},
            ],
        }},
        {"$lookup": {  # R9 headcount on that day: department employees with joined_on <= the day
            "from": "employees", "let": {"d": "$date"}, "as": "h",
            "pipeline": [{"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$d"]}}}, {"$count": "n"}],
        }},
        {"$project": {
            "_id": 0, "dt": 1, "date": 1,
            "is_working_day": {"$in": ["$dow", [2, 3, 4, 5, 6]]},  # Mon-Fri
            "headcount": {"$ifNull": [{"$arrayElemAt": ["$h.n", 0]}, 0]},
            "present_count": {"$ifNull": [{"$arrayElemAt": ["$s.present", 0]}, 0]},  # a day with no records at all: 0
            "late_count": {"$ifNull": [{"$arrayElemAt": ["$s.late", 0]}, 0]},
        }},
        {"$addFields": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, {"$divide": ["$present_count", "$headcount"]}, None]}}},
        # mean of the non-null rates among this row and up to 6 preceding rows of the range (null when there is none)
        {"$setWindowFields": {"sortBy": {"dt": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"dt": 1}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1,
                      "attendance_rate": 1, "moving_avg_7d": 1}},
    ]


def prepare_trend(department: str, date_from: str, date_to: str) -> tuple[date, int, list[str]]:
    """Validate the range and fetch the department's employee codes: the inputs of department_trend_pipeline. Shared by the
    endpoint and the explain endpoint, so both build the same pipeline."""
    start, end = parse_date(date_from), parse_date(date_to)
    if end < start:
        raise validation_error("query", "to", "to must not be before from", date_to)
    days = (end - start).days + 1
    if days > TREND_MAX_DAYS:
        raise validation_error("query", "to", f"the range must not exceed {TREND_MAX_DAYS} days", date_to)
    employee_codes = [e["emp_code"] for e in db.employees.find({"department": department}, {"emp_code": 1, "_id": 0})]
    return start, days, employee_codes


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, date_from: Annotated[DateStr, Query(alias="from")], date_to: Annotated[DateStr, Query(alias="to")]):
    start, days, employee_codes = prepare_trend(department, date_from, date_to)
    if not employee_codes:
        raise HTTPException(404, "department not found")  # a department exists if it has any employees, joined or not
    rows = list(db.employees.aggregate(department_trend_pipeline(department, start, days, employee_codes)))
    return {
        "department": department,
        "items": [
            {
                "date": r["date"],
                "is_working_day": bool(r["is_working_day"]),
                "headcount": int(r["headcount"]),
                "present_count": float(r["present_count"]),
                "late_count": int(r["late_count"]),
                "attendance_rate": None if r["attendance_rate"] is None else round_half_up(r["attendance_rate"], 4),  # R8, rates: 4 decimals
                "moving_avg_7d": None if r["moving_avg_7d"] is None else round_half_up(r["moving_avg_7d"], 4),
            }
            for r in rows  # formatting only (at most 92 rows): the series, the gap filling and the window were built by the pipeline
        ],
    }


# --------------------------------------------------------------------------- #
# 10. Admin explain. Runs MongoDB's explain (executionStats) on the MAIN query or pipeline of an endpoint, built by the very same
#     functions the endpoint uses (attendance_find_spec, employee_monthly_pipeline, ...), and returns MongoDB's output as JSON.
#     It never executes the explained query. Nothing here is a canned or hard-coded plan.
# --------------------------------------------------------------------------- #
ExplainEndpoint = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]


def require_params(endpoint: str, **params: Any) -> None:
    """422, in FastAPI's standard shape, for a parameter the explained endpoint requires but the request left out."""
    for name, value in params.items():
        if value is None:
            raise validation_error("query", name, f"{name} is required to explain {endpoint}", None)


def aggregate_command(collection: str, pipeline: list[dict]) -> dict:
    return {"aggregate": collection, "pipeline": pipeline, "cursor": {}}


def to_json_compatible(document: Any) -> Any:
    """MongoDB's explain document as plain JSON. BSON types become their extended-JSON form (a date as {"$date": ...}), and a
    non-finite float, which JSON cannot carry, becomes a string, so the response can always be serialized."""
    return json.loads(json_util.dumps(document, json_options=json_util.RELAXED_JSON_OPTIONS), parse_constant=lambda name: name)


def redact_connection_details(node: Any) -> Any:
    """Explain output reports the server's host and port under serverInfo. They are connection details, so they are dropped;
    everything else is returned as MongoDB produced it. (The connection string itself never appears in explain output.)"""
    if isinstance(node, dict):
        return {k: ({kk: vv for kk, vv in v.items() if kk not in ("host", "port")} if k == "serverInfo" and isinstance(v, dict)
                    else redact_connection_details(v)) for k, v in node.items()}
    if isinstance(node, list):
        return [redact_connection_details(v) for v in node]
    return node


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainEndpoint,
    emp_code: Optional[str] = None,
    month: Optional[MonthStr] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    trend_from: Annotated[Optional[DateStr], Query(alias="from")] = None,
    trend_to: Annotated[Optional[DateStr], Query(alias="to")] = None,
    page: PageParam = 1,
    page_size: PageSizeParam = 20,
):
    if endpoint == "attendance_list":
        check_date_order(date_from, date_to)
        spec = attendance_find_spec(emp_code, date_from, date_to, status, page, page_size)
        collection = "attendance_logs"
        command = {"find": collection, "filter": spec["filter"], "sort": SON(spec["sort"]), "skip": spec["skip"], "limit": spec["limit"]}
    elif endpoint == "employee_monthly":
        require_params(endpoint, emp_code=emp_code, month=month)
        collection = "attendance_logs"
        command = aggregate_command(collection, employee_monthly_pipeline(emp_code, month))
    elif endpoint == "department_summary":
        require_params(endpoint, month=month)
        collection = "employees"
        command = aggregate_command(collection, department_summary_pipeline(month, department))
    elif endpoint == "late_leaderboard":
        require_params(endpoint, month=month)
        collection = "attendance_logs"
        command = aggregate_command(collection, late_leaderboard_pipeline(month, limit, department))
    else:  # department_trend
        require_params(endpoint, department=department, **{"from": trend_from, "to": trend_to})
        start, days, employee_codes = prepare_trend(department, trend_from, trend_to)  # an unknown department is explained too (no 404)
        collection = "employees"
        command = aggregate_command(collection, department_trend_pipeline(department, start, days, employee_codes))

    try:
        raw = db.command({"explain": command, "verbosity": "executionStats"})  # "explain" must be the first key: it names the command
    except AutoReconnect:
        raise  # connectivity loss: the 503 handler above answers
    except OperationFailure as exc:
        code_name = (exc.details or {}).get("codeName", "OperationFailure")  # never echo the server's message: it can name hosts or users
        raise HTTPException(500, f"MongoDB could not explain this query ({code_name})")
    except PyMongoError:
        raise HTTPException(500, "MongoDB could not explain this query")
    return {"endpoint": endpoint, "collection": collection, "explain": redact_connection_details(to_json_compatible(raw))}
