# Token Bucket Rate Limiter

A standalone FastAPI service that answers whether another service should allow
or deny a request. Per-client configuration and limiter state live in Redis, and
each decision is made atomically by Lua so concurrent callers cannot spend the
same token twice.

## Architecture

```text
Calling service
      |
      | POST /check {client_key}
      v
FastAPI rate-limiter (4 workers)
      |
      | one atomic Lua evaluation
      v
Redis (config + bucket/window state, AOF persistence)
      |
      v
ALLOW (200) or DENY (429) + rate-limit headers
```

## Features

- Token bucket mode for sustained rates with controlled bursts
- Sliding-window mode for a strict request count within a time interval
- Per-client configuration through admin endpoints
- Redis-backed state that survives API restarts
- Atomic decisions under concurrency using Redis Lua
- `X-RateLimit-Limit`, `X-RateLimit-Remaining`, and `X-RateLimit-Reset`
- Reproducible 500+ RPS correctness test

## Run locally

Requirements: Docker with Docker Compose and Python 3.11+.

```bash
docker compose up --build -d
```

The API is available at `http://localhost:8000`; interactive OpenAPI docs are
at `http://localhost:8000/docs`.

```bash
curl http://localhost:8000/health
```

Stop the containers without deleting Redis's named volume:

```bash
docker compose down
```

Use `docker compose down -v` only when you intentionally want to delete all
persisted limiter state.

## API examples

Configure a token-bucket client with a burst of 20 and sustained rate of 5 RPS:

```bash
curl -X POST http://localhost:8000/admin/config \
  -H "Content-Type: application/json" \
  -d '{"client_key":"checkout","mode":"token_bucket","requests_per_second":5,"burst_size":20}'
```

Configure a sliding window allowing 100 requests per 60 seconds:

```bash
curl -X POST http://localhost:8000/admin/config \
  -H "Content-Type: application/json" \
  -d '{"client_key":"reports","mode":"sliding_window","max_requests":100,"window_seconds":60}'
```

Ask for a decision:

```bash
curl -i -X POST http://localhost:8000/check \
  -H "Content-Type: application/json" \
  -d '{"client_key":"checkout"}'
```

`200` means ALLOW and `429` means DENY. Both responses include the three
rate-limit headers.

Read a client's effective configuration:

```bash
curl http://localhost:8000/admin/config/checkout
```

Clients without custom configuration use token bucket mode at 1 RPS with a
burst capacity of 5.

## Test

Run deterministic unit tests inside the application image:

```bash
docker compose exec api python -m pytest -q
```

Run the Phase 7 load test from a dedicated container on the Compose network:

```bash
docker compose run --rm \
  -e BASE_URL=http://api:8000 \
  api python scripts/load_test.py
```

Optional environment variables `CONCURRENCY` and `DURATION_SECONDS` override
the defaults of 100 persistent connections and 10 seconds.

## How the load test proves correctness

The test attacks one freshly configured client from concurrent persistent HTTP
connections. Its bucket starts with capacity `C` and earns tokens at rate `R`.
Across elapsed time `T`, the service can therefore allow no more than:

```text
floor(C + R * T)
```

The measured interval surrounds every server-side decision, making this a
conservative upper bound without an arbitrary tolerance. The script exits with
failure if throughput is below 500 RPS, any request errors, or ALLOW responses
exceed the bound.

### Measured result (2026-07-08)

Test topology: a dedicated load-generator container, four Uvicorn workers, and
one Redis container on the same Docker Compose network.

| Metric | Result |
|---|---:|
| Duration | 10.01 s |
| Concurrency | 100 |
| Completed requests | 96,390 |
| Throughput | 9,627 RPS |
| ALLOW | 1,044 |
| DENY | 95,346 |
| Request errors | 0 |
| p50 / p95 / p99 latency | 3.8 / 26.0 / 41.5 ms |
| Strict ALLOW upper bound | 1,051 |

Result: **PASS**. The service stayed below the mathematical allowance ceiling
while processing well above 500 RPS, with no observed token double-spend.

Performance figures are local measurements, not universal capacity claims;
hardware and container runtime affect the result.

## Design decisions

- **Redis server time:** every worker uses the same clock, avoiding application
  host clock skew when calculating refills and window expiry.
- **One Lua evaluation per decision:** configuration lookup and state mutation
  are one Redis round trip and one atomic operation. A config change cannot land
  between reading the policy and spending a token.
- **Async Redis client:** workers do not block a thread while waiting for Redis.
- **AOF persistence:** Redis appends writes to disk so state survives service
  restarts. Durability depends on Redis's configured fsync policy.
- **Fresh load-test key:** each run begins with a known full bucket and cannot be
  contaminated by state from an earlier run.

## Resume bullet

Built a containerized FastAPI/Redis rate-limiter supporting per-client token
bucket and sliding-window policies; used atomic Lua scripts to prevent token
double-spend and validated 9.6K RPS at 100 concurrent connections with zero
request errors and no over-allocation in a 96K-request correctness test.
