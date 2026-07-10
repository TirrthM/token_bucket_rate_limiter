# Release Notes — v1.0.0

## Highlights

- Built a distributed FastAPI rate limiter with Redis-backed shared state.
- Implemented token-bucket and sliding-window policies with atomic Lua scripts for race-safe decisions.
- Added Docker Compose orchestration with Redis, Nginx, and three API replicas.
- Exposed health, metrics, and dashboard endpoints for observability.
- Added automated pytest coverage plus a distributed correctness load test.

## What’s included

- Public decision endpoint: `/check`
- Admin configuration endpoints: `/admin/config` and `/admin/config/{client_key}`
- Metrics endpoints: `/admin/metrics` and `/admin/metrics/{client_key}`
- Dashboard endpoint: `/dashboard`

## Verified results

- `16 passed` in the pytest suite
- Distributed load test completed with no request errors and no token over-allocation
- Health and dashboard endpoints responded successfully via the public stack

## Quick start

```bash
docker compose up --build -d
curl http://localhost:8000/health
```

## Notes

This release is intended as a polished portfolio/demo release. The repo is ready to be shared, reviewed, or used as a local reference implementation.
