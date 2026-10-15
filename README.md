# Employee Attendance & Analytics API

FastAPI + PyMongo implementation for the Employee Attendance & Analytics assignment.

## Run locally

Requirements: Python 3.11+ and MongoDB 6.0+.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI='mongodb://localhost:27017'
export MONGO_DB='attendance_db'
uvicorn app.main:app --port 8000
```

`MONGO_URI` and `MONGO_DB` are read from the environment. Indexes are created idempotently at application startup. `GET /health` pings MongoDB. Interactive API documentation is available at `/docs`.

## Implementation notes

- All instants are stored as UTC BSON datetimes and returned as epoch milliseconds, truncated to whole seconds.
- Attendance dates and shift times follow IST; overnight shifts are supported.
- Punch-in uniqueness is enforced by a unique `(emp_code, date)` index. Punch-out uses an atomic conditional update. Corrections use optimistic concurrency and append-only audit entries.
- Analytics use MongoDB aggregation pipelines; the trend uses a database-generated date spine and a seven-row window.
- `GET /admin/explain/{endpoint}` exposes MongoDB `executionStats` explain output for the selected query/pipeline.

Before production use, run the endpoints against MongoDB 6.0+ with the supplied and additional edge-case data. In particular, verify explain plans on the target MongoDB deployment because planner choices depend on its data distribution and indexes.
