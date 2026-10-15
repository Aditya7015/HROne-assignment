# DECISIONS.md

1. **Indexes.** I create a unique index on `employees.emp_code` for identity and concurrent duplicate protection, department/join-date indexes for department and headcount queries, a unique `(emp_code, date)` attendance index for one record per employee-day, and date-oriented indexes for attendance listing and monthly analytics. The unique attendance index is essential; I would not add a separate index on every derived metric because that would increase write cost without matching a primary query.

2. **Punch-in race.** Both requests may pass application-level validation, but the unique compound index allows only one insert for an employee/date pair. MongoDB accepts one insert; the other raises `DuplicateKeyError`, which the API translates to 409. No check-then-insert is relied on for correctness.

3. **Ties.** The leaderboard uses MongoDB `$rank` based on total late minutes, then applies the rank cutoff. Equal totals share a rank and the next rank is skipped; rows are finally ordered by employee code for deterministic output.

4. **Headcount.** Department summary starts from eligible employees and looks up attendance, preserving employees with no logs. Employees joining after the month ends are filtered out before grouping.

5. **100x scale.** I would benchmark real explain plans, consider a replica set and read strategy, and move repeated expensive summaries to carefully invalidated materialized aggregates if the freshness requirements permit.
