# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code` (no duplicates, joins), `(department, emp_code)` (filtered listing), `(department, joined_on)` and `(joined_on, department)` (headcount). `attendance_logs`: unique `(emp_code, date)` (one record per day, monthly figures), `(emp_code, punch_in desc)` (punch-out lookup), `(date desc, emp_code)` (list order, month scans). Rejected: a unique `email`, because the contract only makes `emp_code` unique.

2. **Punch-in race.** Both requests try to insert. The unique `(emp_code, date)` index lets MongoDB accept exactly one; the other gets a duplicate-key error, which I return as 409. There is no check-then-insert step.

3. **Ties.** `$rank` gives tied employees the same rank (1, 2, 2, 4). `limit` is applied to the rank after ranking, so everyone tied at the cutoff is returned, even if that means more than `limit` rows.

4. **Headcount.** The summary starts from `employees`, keeps those who joined by month end, then looks up each one's logs. No logs just means zeros, so they are still counted.

5. **100x the data.** Keep pre-aggregated daily statistics, updated on each punch or correction, so analytics read small summaries instead of scanning raw logs.
