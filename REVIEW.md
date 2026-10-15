# REVIEW.md

| # | Where | What is wrong | How to notice | Fix applied |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Naive/local datetimes and same-date shift assumptions break IST and overnight shifts. | Punch in after midnight for a 22:00–06:00 shift. | Normalize instants to UTC, derive the shift's IST start date, and apply the strict 10-minute grace rule. |
| 2 | `compute_work_hours` | Built-in `round` uses ties-to-even, not half-up. | A duration at a half-centesimal rounding boundary. | Decimal `ROUND_HALF_UP` after whole-second truncation. |
| 3 | `compute_overtime` | Always puts shift end on the attendance date. | A 22:00–06:00 shift punched out at 06:40. | Move shift end to the next day for overnight shifts; apply the 30-minute threshold. |
| 4 | `health` | Returns success without checking MongoDB. | Stop MongoDB and call `/health`. | Ping the database and return 503 on connection failure. |
| 5 | `create_employee` | Duplicate check followed by insert races under concurrent requests. | Send parallel creates for the same `emp_code`. | Unique database index; convert duplicate-key error to 409. |
| 6 | `EmployeeIn` / `create_employee` | Shift start and end could be identical; created timestamp was naive. | Submit identical shift times or inspect stored `created_at`. | Validate distinct shift times and store UTC-aware timestamps. |
| 7 | `list_employees` | Offset used `page * page_size`, skipping page one. | Request page 1 with page size 20. | Use `(page - 1) * page_size`. |
| 8 | `list_employees` | Total ignored the department filter and results were not sorted. | Filter by department and compare `total`; inspect ordering. | Count with the same filter and sort by `emp_code`. |
| 9 | `list_employees` | Pagination values were not bounded. | Request page 0 or page size above 100. | Enforce page >= 1 and page size 1–100. |
| 10 | `punch_in` | Unknown employees could cause a server error; timestamps used local time; duplicate check was race-prone. | Unknown code, UTC/IST boundary, or parallel punches. | Return 404, normalize epoch milliseconds, derive IST attendance date, and rely on the unique compound index. |
| 11 | `punch_in` | Status and timestamp inputs were insufficiently validated. | Send `ABSENT`, a string timestamp, or seconds instead of milliseconds. | Strict Pydantic request model and epoch-millisecond validation. |
| 12 | `list_attendance` | Loaded and sorted the entire matching result set in Python. | Test with a large collection. | Filter, sort, skip, limit, and count in MongoDB with indexes. |
| 13 | `list_attendance` | Date ranges and pagination were not fully validated; deterministic tie sorting was missing. | `date_from > date_to` or repeated dates across employees. | Validate dates and sort by date descending then employee code ascending. |
| 14 | `punch_out` (missing) | No close-shift endpoint or protection against simultaneous updates. | Punch out twice concurrently or cross midnight. | Added a conditional atomic update and R3–R5 calculations. |
| 15 | Regularization (missing) | No correction validation or immutable audit trail. | Change a status/time and inspect history; send concurrent corrections. | Added validation, before/after diffs, append-only history, and optimistic concurrency. |
| 16 | Analytics (missing) | No aggregation-backed summaries, rank handling, trend gap filling, or explain endpoint. | Exercise ties, zero-log employees, weekends, and missing dates. | Added MongoDB pipelines and `executionStats` explain endpoint. |

Reviewed and retained: `emp_code` is the business identifier, not MongoDB `_id`; the API returns no internal `_id`; absent/leave records have no punch times; and history is written only for manual corrections.
