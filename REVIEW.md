# REVIEW.md

Defects found in the starter `app/main.py` (line numbers refer to the **original** starter file).

**How they were found.** I ran the unmodified starter through FastAPI's `TestClient` against `mongomock`, called its helper
functions directly, and read the code. "Reproduced" below means I ran it and saw the wrong result; "read" means I only found it in
the code. `mongomock` does not reproduce real timing, so the two races were shown by *forcing* the interleaving (both requests pass the
existence check before either inserts). Each fix has regression tests (`tests/`), and for the endpoints I also re-introduced each
original defect one at a time (mutation testing) to confirm the tests fail without the fix.

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How I fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` L36-37 | The 10-minute grace is tested on *floored minutes*, so 10 min 1 s counts as 10 and is not "> 10". | Reproduced: punch-in 09:40:01 on a 09:30 shift returns 0; the contract says 10. | Work in whole seconds: late only if more than 600 s, then `seconds // 60`. |
| 2 | `compute_late_minutes` L35 | Shift start is built with `punch_in.replace(hour=...)`, in the punch-in's own zone; IST is never applied. | Reproduced: UTC-aware 05:00Z (10:30 IST) on a 09:30 shift returns 0, expected 60. | Convert to IST first and anchor on the attendance date (`shift_start_at`). |
| 3 | `compute_late_minutes` L35 | Overnight shifts ignored: the start is always on the punch-in's own calendar day. | Reproduced: 22:00 shift, punch-in 00:30 IST next day returns 0, expected 150. | The anchor is the attendance date from R1 (`attendance_date`). |
| 4 | `compute_work_hours` L41 | `round()` works on binary floats (half-even in effect), not half-up. | Reproduced: 8 h 02 min 42 s = 8.045 h returns 8.04, expected 8.05; 600 of the 1,200 exact-tie second values in 0-12 h are wrong. | Exact integer seconds and `Fraction`, rounded half-up (`round_half_up`); `half_day` is derived from the rounded value. |
| 5 | `compute_overtime` L47 | No 30-minute minimum. | Reproduced: out at 18:35 on an 18:30 shift end returns 5, expected 0. | Count only whole minutes >= 30. |
| 6 | `compute_overtime` L46 | Shift end is placed on the record date, so an overnight end (06:00) is *before* the shift start. | Reproduced: sample overnight record, out 06:40 next day, returns 1480, expected 40. | An overnight shift ends on the next calendar day (`shift_end_at`). |
| 7 | `compute_overtime` L46-47 | Subtracts an aware end from a possibly naive `punch_out`. | Reproduced: `TypeError` for a naive `punch_out`. | All instants are UTC-aware; client uses `tz_aware=True`; naive values are treated as UTC. |
| 8 | `punch_in` L102 | `fromtimestamp(ms / 1000)` and `datetime.now()` use *server-local* naive time; float division; no truncation to whole seconds. | Read, and visible in #9: results change with the host's timezone. | `ms_to_instant` (integer arithmetic, UTC, truncated); `utc_now`. |
| 9 | `punch_in` L103 | The attendance date is the server-local date, not the IST date. | Reproduced: 2026-07-10T20:00Z (01:30 IST on 07-11) gives `2026-07-10` on a UTC host; correct only if the host runs in IST. | `attendance_date` converts to IST. |
| 10 | `punch_in` L103 | The overnight attendance-date rule (R1) is missing. | Reproduced (IST host): 00:30 IST punch-in on an overnight shift gets the next day's date and 0 late minutes. | `attendance_date` assigns it to the previous day when the time is before `shift_end`. |
| 11 | `EmployeeIn` L53-60 | No validation at all. | Reproduced: `emp_code="abc"`, shift `99:99`, `joined_on="garbage"`, equal shifts all return 201 and are stored. | `EmployeeCreate`: patterns, lengths, `HH:MM`, a real date, and `shift_start != shift_end`. |
| 12 | `PunchInIn.punched_at` L65, `punch_in` L102 | Pydantic lax mode accepts floats and numeric strings; a seconds-looking value is accepted; `if body.punched_at` treats 0 as "now". | Reproduced: `1783312500` (seconds) created a record dated 1970-01-21; `1783312500000.0` and `"1783312500000"` returned 201. | Strict `EpochMs` (integer, 1e11..4102444800000); `is not None`. |
| 13 | `PunchInIn.status` L66 | Free-text status. | Reproduced: `ABSENT` and `BANANA` accepted at punch-in. | `PresenceStatus` literal (`PRESENT`, `WFH`, `ON_DUTY`). |
| 14 | whole file | No indexes (only `_id_`), so no uniqueness guarantee and nothing for the queries to use. | Reproduced: `index_information()` lists `_id_` only. | `INDEXES` created at startup (`ensure_indexes`); startup fails with a clear message if a unique index cannot be built. |
| 15 | `health` L72-74 | Never touches MongoDB, so it says "ok" with the database down; the client's default 30 s server-selection timeout would also hang requests. | Reproduced: ping against a dead server failed after 30.0 s. | `db.command("ping")`, 503 on failure; `serverSelectionTimeoutMS=3000`. |
| 16 | `create_employee` L79-83 | Check-then-insert race; nothing enforces uniqueness. | Reproduced with forced interleaving: two 201s and two documents for one `emp_code`. | Unique `emp_code` index; `DuplicateKeyError` becomes 409. |
| 17 | `create_employee` L82, `list_employees` L95 | `created_at` is naive local time and is returned as an ISO string; the contract says epoch milliseconds. | Reproduced: the response carried an ISO-8601 string for `created_at`, not an integer. | `utc_now()` and `serialize_employee` (epoch ms). |
| 18 | `list_employees` L93 | `skip = page * page_size` skips the first page. | Reproduced: a plain `GET /employees` returns an empty list; `page=1&page_size=2` returns employees 3 and 4. | `(page - 1) * page_size`. |
| 19 | `list_employees` L94 | `total` counts the whole collection, ignoring `department`. | Reproduced: `department=Sales` returned the unfiltered total. | `count_documents(query)`. |
| 20 | `list_employees` L95 | No sort; the contract orders by `emp_code`. | Read. | `sort("emp_code")`, with a `(department, emp_code)` index. |
| 21 | `list_employees` L89, `list_attendance` L130-131 | Pagination parameters are unbounded. | Reproduced: `page_size=0` returns every document; `page=-1` makes PyMongo raise `ValueError: skip must be >= 0` (a 500). | `page >= 1`, `1 <= page_size <= 100` (422 otherwise). |
| 22 | `punch_in` L101, L113 | An unknown employee makes `emp["shift_start"]` fail, and a stray log makes the 409 check run before any 404. | Reproduced: unknown `emp_code` returns a 500 (`TypeError`). | Look the employee up first; 404. |
| 23 | `punch_in` L104-118 | Check-then-insert race: nothing stops two records for one employee and date. | Reproduced with forced interleaving: two 201s and two documents. | Unique `(emp_code, date)` index; `DuplicateKeyError` becomes 409. |
| 24 | `punch_in` L119-121, `list_attendance` L148-149 | Responses leak an `id` the contract does not have, return datetimes as ISO strings, omit fields legacy documents lack, and leave nested history dates unconverted. | Reproduced: `id` in the body; the legacy sample record came back without `half_day` and `history`. | `serialize_attendance`: no ids, epoch ms everywhere (including inside `history`), documented defaults for missing fields. |
| 25 | `list_attendance` L144-147 | Loads the whole collection into memory, then sorts and slices in Python. | Read, and the first stage of the "100k records" problem; confirmed with a spy that only one page is now fetched. | Database-side sort, skip and limit with matching indexes. |
| 26 | `list_attendance` L145 | Sorts by date only; ties come back in storage order. | Reproduced: on 2026-07-08 EMP0004 came before EMP0003; the contract says `date` descending, then `emp_code` ascending. | Sort on `(date desc, emp_code asc)`. |
| 27 | `list_attendance` L126-143 | No validation of dates, `status` or their order. | Reproduced: `date_from > date_to`, `status=BOGUS` and `date_from=garbage` all returned 200. | `DateStr`, `Status` literal, and a 422 when `date_from > date_to`. |
| 28 | whole file | No handling of PyMongo errors, so a lost connection surfaces as a 500. | Read. | `AutoReconnect` is answered with 503; duplicate-key errors are handled where they occur. |

## Looked at and judged not defects

* `IST = timezone(timedelta(hours=5, minutes=30))` is correct (IST has no daylight saving).
* `load_dotenv()` uses its default `override=False`, so real environment variables win over `.env`, as required.
* `create_employee` already removes `_id` from its response.
* Request fields the model does not declare (for example client-sent derived fields) are ignored by Pydantic's default, which the contract wants; this was kept.
* `status_code=201` on both POST routes and the `{"_id": 0}` projection in `list_employees` are fine.
* `README.md` tells the reader to copy `.env.example`, but the kit does not contain one (not a defect in `main.py`; the README was rewritten).

## Not defects, but missing from the starter

`POST /attendance/punch-out`, `PATCH /attendance/{emp_code}/{date}`, the four analytics endpoints and `GET /admin/explain/{endpoint}` did not exist and are now implemented. Their design is in `README.md` and `DECISIONS.md`.

## Deliberately left unresolved

* **Not run against a real MongoDB.** The suite uses `mongomock` and, for the aggregation pipelines, a test-only emulator (`tests/mini_mongo.py`). The 26 skipped tests are the ones that need a real server (index plans, atomicity stress, server-feature preflight). Until they have been run with `TEST_MONGO_URI`, whether a real server accepts the pipelines and plans them with an index is **unverified**.
* **Contract points the documents leave open** (records dated before `joined_on`, what counts as "any day", rounding of the moving average, and so on) follow the literal wording and are listed in `README.md`; each is pinned by a test.
* The 92-day trend limit is read as 92 dates, inclusive.
