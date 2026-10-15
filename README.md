# Employee Attendance & Analytics API

A REST API built with **FastAPI, PyMongo, and MongoDB** for employee management, attendance tracking, manual attendance corrections with an audit trail, analytics reports, and MongoDB query-plan inspection.

The API contract is defined in `openapi.yaml`, the stored document structures are described in `DATA_MODEL.md`, and the application implementation is in `app/main.py`.

## Requirements

- Python 3.11 or newer
- MongoDB 6.0 or newer
- Dependencies listed in `requirements.txt`

MongoDB 6.0+ is required for aggregation features used by the analytics pipelines, including `$setWindowFields` and correlated `$lookup` pipelines.

## Setup and Run

### 1. Clone the repository

```bash
git clone https://github.com/YOUR_USERNAME/hrone-attendance-api.git
cd hrone-attendance-api
```

Replace `YOUR_USERNAME` with your GitHub username.

### 2. Create a virtual environment

**Windows PowerShell**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

**Linux/macOS**

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install dependencies

```bash
python -m pip install -r requirements.txt
```

### 4. Configure MongoDB

The application reads `MONGO_URI` and `MONGO_DB` from environment variables. If a local `.env` file exists, it is also loaded. Explicit environment variables take precedence over values in `.env`.

**Windows PowerShell**

```powershell
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
```

**Linux/macOS**

```bash
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"
```

The defaults are `mongodb://localhost:27017` and `attendance_db`.

You can use a local MongoDB installation or a MongoDB Atlas connection string. Ensure the database is reachable before starting the application.

**Security:** Never commit `.env` files, database credentials, or connection strings containing passwords.

### 5. Start the API

```bash
python -m uvicorn app.main:app --port 8000
```

The interactive API documentation is available at:

- Swagger UI: `http://127.0.0.1:8000/docs`
- OpenAPI JSON: `http://127.0.0.1:8000/openapi.json`

On startup, the application creates the required indexes. Startup fails with an error if MongoDB is unreachable or a required index cannot be created.

### Optional: Load sample data

```bash
python sample_seed.py
```

The seed script deletes existing documents from the `employees` and `attendance_logs` collections in the configured database before loading sample records. **Use only a disposable development database.**

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| GET | `/health` | Checks MongoDB connectivity; returns 200 when healthy and 503 when unavailable. |
| POST | `/employees` | Creates an employee; rejects duplicate employee codes. |
| GET | `/employees` | Returns a paginated employee list with department filtering. |
| POST | `/attendance/punch-in` | Records attendance and applies business-date, shift, and lateness rules. |
| POST | `/attendance/punch-out` | Closes the applicable attendance record and calculates derived attendance fields. |
| PATCH | `/attendance/{emp_code}/{date}` | Corrects attendance and records an audit entry. |
| GET | `/attendance` | Returns filtered and paginated attendance records. |
| GET | `/analytics/employees/{emp_code}/monthly` | Returns monthly analytics for an employee. |
| GET | `/analytics/departments/summary` | Returns department-level attendance statistics. |
| GET | `/analytics/leaderboard/late` | Ranks employees by late-arrival minutes. |
| GET | `/analytics/departments/{department}/trend` | Returns daily department trends and a seven-day moving average. |
| GET | `/admin/explain/{endpoint}` | Returns MongoDB execution statistics for a supported endpoint query or pipeline. |

Refer to `openapi.yaml` for the exact request parameters, response schemas, and validation rules.

### Important API conventions

- Timestamps in API requests and responses use Unix epoch milliseconds.
- Calendar dates use `YYYY-MM-DD` strings and follow IST business-date rules.
- Duplicate employee creation and repeated punch-ins return conflict responses.
- Manual corrections recompute derived fields and preserve an audit history.
- Analytics calculations use MongoDB aggregation pipelines.

## Database Indexes

The application creates indexes at startup using `ensure_indexes()`.

### Employees collection

- Unique `emp_code`
- Compound `(department, emp_code)`
- Compound `(department, joined_on)`
- Compound `(joined_on, department)`

### Attendance collection

- Unique `(emp_code, date)`
- Compound `(emp_code, punch_in DESC)`
- Compound `(date DESC, emp_code)`

These indexes support uniqueness constraints and the application's principal filtering and sorting operations. See `DECISIONS.md` for the design rationale.

## Attendance Calculations and Design

### Derived fields

The application stores derived values such as:

- `late_minutes`
- `overtime_minutes`
- `work_hours`
- `half_day`

These are computed by application helper functions when attendance is recorded or corrected. Analytics use the stored values.

### Concurrency and data integrity

- Unique indexes enforce employee-code and employee-date uniqueness.
- Punch-out uses a conditional database update to prevent a record from being closed more than once.
- Manual corrections use conditional updates and append audit entries.
- Database-level conflicts are handled through appropriate error responses.

### Analytics

The four analytics reports use MongoDB aggregation pipelines instead of retrieving the entire attendance collection into Python for calculation.

The department summary starts from employees so employees without attendance records can be included in headcount. The late leaderboard uses ranking logic, and the department trend uses database-side date generation and a seven-day moving average.

### Admin explain endpoint

The explain endpoint uses the query or pipeline builders associated with supported endpoints and requests MongoDB `executionStats`. It returns the execution-plan information in a JSON-compatible response.

The endpoint has no authentication because the supplied contract does not define an authentication mechanism. Deployments should consider access restrictions appropriate to their environment.

## Testing

Install the development dependencies:

```bash
python -m pip install -r requirements-dev.txt
```

Run the default test suite:

```bash
python -m pytest -q
```

### Default test configuration

The default suite runs without a real MongoDB server. It uses `mongomock` for database operations and `tests/mini_mongo.py`, a test-only interpreter for the aggregation operators required by the analytics tests.

This verifies the covered application logic, but it does not prove that every aggregation pipeline is accepted by a real MongoDB server or that the server selects the expected indexes.

### Testing against real MongoDB

Use MongoDB 6.0 or newer. Configure `TEST_MONGO_URI` to point to a disposable test database server.

**Windows PowerShell**

```powershell
$env:TEST_MONGO_URI = "YOUR_TEST_MONGODB_CONNECTION_STRING"
python -m pytest -q
```

**Linux/macOS**

```bash
TEST_MONGO_URI="YOUR_TEST_MONGODB_CONNECTION_STRING" python -m pytest -q
```

The real-server tests cover database-specific aggregation features, query plans, concurrency, and scale scenarios. The scale tests create approximately 100,000 attendance records and check analytics results, query-plan characteristics, and response-time limits.

Use a dedicated test environment. Review the test fixtures before execution because the tests create and drop temporary databases.

### Verification status

| Verification | Result |
|---|---|
| Default test suite | 916 passed, 26 skipped, 2 warnings |
| Runtime | 86.25 seconds |
| Real MongoDB suite | Inconclusive; Atlas connectivity errors prevented a reliable complete run |

The 26 skipped tests in the default configuration require a real MongoDB connection. The real-server run encountered DNS resolution timeouts and replica-set primary-selection failures. Therefore, full real-server aggregation, query-plan, and scale verification remains outstanding.

The two warnings in the latest default run are deprecation warnings from `mongomock`, not test failures.

The test suite also checks documentation consistency for `README.md`, `REVIEW.md`, and `DECISIONS.md`.

## Contract Interpretations

Some behavior is not fully specified by the contract. The implementation documents its chosen interpretations in `DECISIONS.md` and tests the applicable behavior.

Examples include:

- Punch-out selection and error precedence.
- Manual correction no-op handling.
- Attendance records around employee joining dates.
- Department ordering and date-range boundaries.
- Behavior of explain requests for unknown employees or departments.

Consult `DECISIONS.md` for the exact interpretations implemented.

## Project Structure

```text
hrone-attendance-api/
├── app/
│   ├── __init__.py
│   └── main.py
├── sample_data/
├── tests/
│   ├── conftest.py
│   ├── mini_mongo.py
│   └── ...
├── .gitignore
├── DATA_MODEL.md
├── DECISIONS.md
├── REVIEW.md
├── openapi.yaml
├── pytest.ini
├── requirements.txt
├── requirements-dev.txt
└── sample_seed.py
```

The `tests/` directory contains the project's automated tests. Other files listed above describe the API contract, stored data model, design decisions, and review of the original implementation.

## Known Limitations

- Full verification against a real MongoDB server remains incomplete because of connectivity failures during testing.
- The explain endpoint is unauthenticated, as defined by the supplied contract.
- The department trend pipeline passes department employee codes as a list, which can grow with department size.
- Real-server execution plans and scale-test results should be verified in a stable MongoDB environment before production use.

## Submission Checklist

Before publishing or submitting, confirm that the repository contains:

- `app/main.py` and `app/__init__.py`
- `requirements.txt` and `requirements-dev.txt`
- `README.md`, `REVIEW.md`, and `DECISIONS.md`
- `openapi.yaml` and `DATA_MODEL.md`
- `sample_data/` and `sample_seed.py`
- `pytest.ini`, `.gitignore`, and `tests/`

Do **not** commit:

- `.env` files or credentials
- The confidential assignment/problem statement
- Database dumps or production data
- A virtual environment
- Files explicitly prohibited by the assignment

Review the staged files before committing:

```bash
git status --short
git diff --cached --check
```

This project is submitted as an implementation of the supplied attendance and analytics API contract. See `REVIEW.md` for the implementation review and `DECISIONS.md` for the design rationale and documented assumptions.
