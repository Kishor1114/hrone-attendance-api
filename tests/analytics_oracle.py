"""Independent reference implementations of the analytics contract, written as plain loops over documents.

They share NO code with app/main.py (own calendar logic from `calendar`/`datetime`, own half-up rounding with `Decimal`), so a
bug in a pipeline cannot hide behind a shared helper. Tests compare the endpoints to these on hand-built and random data.
"""
import calendar
import datetime as dt
from decimal import ROUND_HALF_UP, Decimal

PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}


def half_up(value, places):
    q = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def d_half_up(value: Decimal, places):
    return float(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def weekdays_of_month(month):
    year, mon = map(int, month.split("-"))
    days = (dt.date(year, mon, d) for d in range(1, calendar.monthrange(year, mon)[1] + 1))
    return [d for d in days if d.weekday() < 5]


def is_weekday(date_str):
    return dt.date.fromisoformat(date_str).weekday() < 5


def presence_weight(log):
    if log.get("status") not in PRESENCE:
        return Decimal(0)
    return Decimal("0.5") if log.get("half_day") is True else Decimal(1)


def in_month(log, month):
    return log["date"][:7] == month


def oracle_monthly(employee, logs, month):
    mine = [l for l in logs if l["emp_code"] == employee["emp_code"] and in_month(l, month)]
    working_days = sum(1 for d in weekdays_of_month(month) if d.isoformat() >= employee["joined_on"])
    present = sum((presence_weight(l) for l in mine if is_weekday(l["date"])), Decimal(0))
    return {
        "emp_code": employee["emp_code"],
        "month": month,
        "working_days": working_days,
        "present_days": float(present),
        "leave_days": sum(1 for l in mine if l.get("status") == "LEAVE"),
        "late_count": sum(1 for l in mine if (l.get("late_minutes") or 0) > 0),
        "total_late_minutes": sum((l.get("late_minutes") or 0) for l in mine),
        "total_overtime_minutes": sum((l.get("overtime_minutes") or 0) for l in mine),
        "attendance_pct": None if working_days == 0 else d_half_up(present * 100 / Decimal(working_days), 2),
    }


def last_day_of(month):
    year, mon = map(int, month.split("-"))
    return dt.date(year, mon, calendar.monthrange(year, mon)[1]).isoformat()


def oracle_summary(employees, logs, month, department=None):
    """Department summary by brute force: for every employee who has joined by month end, walk their logs."""
    last = last_day_of(month)
    rows = {}
    for e in employees:
        if e["joined_on"] > last or (department is not None and e["department"] != department):
            continue
        row = rows.setdefault(e["department"], {"headcount": 0, "present": Decimal(0), "hours": [], "late_count": 0, "late_total": 0, "leave": 0, "onduty": 0})
        row["headcount"] += 1
        for l in logs:
            if l["emp_code"] != e["emp_code"] or not in_month(l, month):
                continue
            if is_weekday(l["date"]):
                row["present"] += presence_weight(l)
            if l.get("status") in PRESENCE and l.get("work_hours") is not None:
                row["hours"].append(Decimal(str(l["work_hours"])))
            late = l.get("late_minutes") or 0
            row["late_count"] += late > 0
            row["late_total"] += late
            row["leave"] += l.get("status") == "LEAVE"
            row["onduty"] += l.get("status") == "ON_DUTY"
    return {"month": month, "items": [
        {"department": dep, "headcount": r["headcount"], "present_days": float(r["present"]),
         "avg_work_hours": None if not r["hours"] else d_half_up(sum(r["hours"]) / Decimal(len(r["hours"])), 2),
         "late_count": r["late_count"], "total_late_minutes": r["late_total"], "leave_count": r["leave"], "on_duty_count": r["onduty"]}
        for dep, r in sorted(rows.items())]}


def oracle_leaderboard(employees, logs, month, limit=10, department=None):
    """Brute force. Competition rank is DEFINED here as 1 + the number of employees with a strictly larger total, which shares
    no logic with the pipeline's window function."""
    by_code = {e["emp_code"]: e for e in employees}
    totals = {}
    for l in logs:
        late = l.get("late_minutes") or 0
        e = by_code.get(l["emp_code"])
        if not in_month(l, month) or late <= 0 or e is None or (department is not None and e["department"] != department):
            continue
        t = totals.setdefault(l["emp_code"], [0, 0])
        t[0] += late
        t[1] += 1
    ordered = sorted(totals.items(), key=lambda kv: (-kv[1][0], kv[0]))
    items = []
    for code, (total, count) in ordered:
        rank = 1 + sum(1 for _, (other, _c) in ordered if other > total)
        if rank <= limit:
            e = by_code[code]
            items.append({"rank": rank, "emp_code": code, "name": e["name"], "department": e["department"], "total_late_minutes": total, "late_count": count})
    return {"month": month, "items": items}


def oracle_trend(employees, logs, department, from_date, to_date):
    """Brute-force daily series. The moving average is the mean of the UNROUNDED rates (exact Decimals), rounded once."""
    codes = {e["emp_code"] for e in employees if e["department"] == department}
    start, end = dt.date.fromisoformat(from_date), dt.date.fromisoformat(to_date)
    rows, rates = [], []
    day = start
    while day <= end:
        ds = day.isoformat()
        headcount = sum(1 for e in employees if e["department"] == department and e["joined_on"] <= ds)
        todays = [l for l in logs if l["date"] == ds and l["emp_code"] in codes]
        present = sum((presence_weight(l) for l in todays), Decimal(0))
        working = day.weekday() < 5
        rate = present / Decimal(headcount) if working and headcount > 0 else None
        rates.append(rate)
        window = [r for r in rates[-7:] if r is not None]
        moving = None if not window else sum(window) / Decimal(len(window))
        rows.append({"date": ds, "is_working_day": working, "headcount": headcount, "present_count": float(present),
                     "late_count": sum(1 for l in todays if (l.get("late_minutes") or 0) > 0),
                     "attendance_rate": None if rate is None else d_half_up(rate, 4),
                     "moving_avg_7d": None if moving is None else d_half_up(moving, 4)})
        day += dt.timedelta(days=1)
    return {"department": department, "items": rows}
