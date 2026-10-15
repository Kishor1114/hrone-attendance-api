"""Shared by the explain tests: the requests to explain, and the assertion on a returned plan."""
import json

# (endpoint, query parameters): every variety of request the explain endpoint accepts, with the filters that matter for index choice.
EXPLAIN_CASES = [
    ("attendance_list", {}),
    ("attendance_list", {"emp_code": "EMP0007"}),
    ("attendance_list", {"date_from": "2026-03-01", "date_to": "2026-03-31"}),
    ("attendance_list", {"status": "LEAVE"}),
    ("attendance_list", {"status": "WFH", "date_from": "2026-05-01"}),
    ("attendance_list", {"emp_code": "EMP0007", "status": "PRESENT", "date_from": "2026-03-01", "date_to": "2026-06-30"}),
    ("attendance_list", {"page": 50, "page_size": 100}),
    ("employee_monthly", {"emp_code": "EMP0007", "month": "2026-03"}),
    ("department_summary", {"month": "2026-03"}),
    ("department_summary", {"month": "2026-03", "department": "Engineering"}),
    ("late_leaderboard", {"month": "2026-03"}),
    ("late_leaderboard", {"month": "2026-03", "limit": 5, "department": "Sales"}),
    ("department_trend", {"department": "Engineering", "from": "2026-03-02", "to": "2026-06-01"}),  # 92 dates: the longest allowed
]


def walk(node):
    """Every (key, value) pair in a nested JSON-like structure."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield k, v
            yield from walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v)


def assert_index_backed(explain):
    """The grader's rule, plus one stricter check: the plan contains IXSCAN and no COLLSCAN anywhere (rejected plans included),
    executionStats are present, and no $lookup stage reports a collection scan."""
    text = json.dumps(explain)
    assert "executionStats" in text, "explain output has no executionStats"
    assert "COLLSCAN" not in text, "the plan contains a collection scan"
    assert "IXSCAN" in text, "no index scan in the plan"
    scans = [v for k, v in walk(explain) if k == "collectionScans"]
    assert all(v == 0 for v in scans), f"a $lookup stage scanned a collection: {scans}"
