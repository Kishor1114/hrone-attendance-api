"""The submission documents make claims about the code. These tests keep them true."""
import inspect
import re
from pathlib import Path

import app.main as m

ROOT = Path(__file__).resolve().parent.parent
DECISIONS = (ROOT / "DECISIONS.md").read_text(encoding="utf-8")
REVIEW = (ROOT / "REVIEW.md").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")


def words(text):
    return len(re.findall(r"\S+", text.replace("**", "")))


def test_decisions_answers_all_five_questions_in_150_to_200_words():
    answers = [line for line in DECISIONS.splitlines() if re.match(r"^\d\. \*\*", line)]
    assert [a[0] for a in answers] == ["1", "2", "3", "4", "5"]
    assert 150 <= sum(words(a) for a in answers) <= 200


def test_every_index_in_the_code_is_named_in_decisions():
    for collection, specs in m.INDEXES.items():
        assert collection in DECISIONS
        for keys, _options in specs:
            fields = [f + (" desc" if direction == -1 else "") for f, direction in keys]
            token = f"`{fields[0]}`" if len(fields) == 1 else "`(" + ", ".join(fields) + ")`"
            assert token in DECISIONS, f"{collection} index {token} is not described in DECISIONS.md"
    unique = [(c, [f for f, _ in k]) for c, specs in m.INDEXES.items() for k, o in specs if o.get("unique")]
    assert unique == [("employees", ["emp_code"]), ("attendance_logs", ["emp_code", "date"])]  # the two the answers rely on


def test_the_punch_in_race_answer_matches_the_code():
    source = inspect.getsource(m.punch_in)
    assert "attendance_logs.find_one" not in source  # no check-then-insert
    assert "insert_one" in source and "DuplicateKeyError" in source and "409" in source


def test_the_tie_answer_matches_the_leaderboard_pipeline():
    answer = next(line for line in DECISIONS.splitlines() if line.startswith("3. **"))
    assert "$rank" in answer and "after ranking" in answer and "before ranking" not in answer and "same rank" in answer  # the wording, too
    pipeline = m.late_leaderboard_pipeline("2026-07", 3)
    assert {"$rank": {}} in [s["$setWindowFields"]["output"]["rank"] for s in pipeline if "$setWindowFields" in s]
    assert {"$match": {"rank": {"$lte": 3}}} in pipeline and not any("$limit" in s for s in pipeline)


def test_the_headcount_answer_matches_the_summary_implementation():
    assert "db.employees.aggregate" in inspect.getsource(m.department_summary)
    assert next(iter(m.department_summary_pipeline("2026-07")[0])) == "$match" and "joined_on" in m.department_summary_pipeline("2026-07")[0]["$match"]
    assert next(iter(m.department_summary_pipeline("2026-07")[1])) == "$lookup"


def test_review_has_a_complete_numbered_table():
    rows = [line for line in REVIEW.splitlines() if re.match(r"^\| \d+ \|", line)]
    assert [int(re.match(r"^\| (\d+)", r).group(1)) for r in rows] == list(range(1, len(rows) + 1)) and len(rows) >= 20
    for row in rows:
        cells = [c.strip() for c in row.strip("|").split("|")]
        assert len(cells) == 5 and all(cells), row  # no empty cell


def test_review_names_functions_that_exist_in_the_current_code():
    for name in ("attendance_date", "round_half_up", "shift_end_at", "shift_start_at", "ensure_indexes", "serialize_attendance",
                 "serialize_employee", "ms_to_instant", "utc_now", "EmployeeCreate", "EpochMs", "PresenceStatus", "INDEXES"):
        assert hasattr(m, name), f"{name} no longer exists in app/main.py"
        assert name in REVIEW, f"REVIEW.md no longer mentions {name}"


def test_readme_documents_every_route_of_the_application():
    paths = sorted({r.path for r in m.app.routes if hasattr(r, "methods") and not r.path.startswith(("/docs", "/redoc", "/openapi"))})
    assert len(paths) == 11
    for path in paths:
        assert path in README, f"{path} is not in the README endpoint table"


def test_readme_gives_setup_test_and_submission_instructions():
    for needle in ("pip install -r requirements.txt", "uvicorn app.main:app", "python -m pytest -q", "TEST_MONGO_URI", "MONGO_URI", "MONGO_DB",
                   "PROBLEM_STATEMENT.docx", ".env", "Dockerfile", "Verification status", "Known limitations"):
        assert needle in README, needle
    assert ".env.example" not in README  # the kit mentioned a file that does not exist


def test_the_repository_does_not_ship_secrets_or_the_confidential_statement():
    forbidden = [p for p in ROOT.rglob("*") if p.is_file() and ".venv" not in p.parts and ".git" not in p.parts and "__pycache__" not in p.parts
                 and (p.name == ".env" or p.name.startswith(".env.") or p.name.lower() == "dockerfile" or "PROBLEM_STATEMENT" in p.name
                      or p.suffix in (".dump", ".bson", ".archive", ".pem", ".key"))]
    assert forbidden == []
    ignored = (ROOT / ".gitignore").read_text()
    for pattern in (".env", "PROBLEM_STATEMENT.docx", "Dockerfile", ".venv/"):
        assert pattern in ignored
