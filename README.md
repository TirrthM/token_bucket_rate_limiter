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
Nginx load balancer :8000
      |
      +------ API instance 1 ----+
      +------ API instance 2 ----+---- Redis
      +------ API instance 3 ----+     (shared config, state, metrics)
                                         |
                                         v
                         ALLOW (200) or DENY (429)
```

## Features

- Token bucket mode for sustained rates with controlled bursts
- Sliding-window mode for a strict request count within a time interval
- Per-client configuration through admin endpoints
- Redis-backed state that survives API restarts
- Atomic decisions under concurrency using Redis Lua
- `X-RateLimit-Limit`, `X-RateLimit-Remaining`, and `X-RateLimit-Reset`
- Reproducible 500+ RPS correctness test
- Three independently running API instances behind Nginx
- Shared limits that remain correct regardless of which instance handles a request
- Live dashboard with per-client totals, denial rate, RPS, and instance distribution

## Run locally

Requirements: Docker with Docker Compose and Python 3.11+.

```bash
docker compose up --build -d
```

Nginx is available at `http://localhost:8000` and distributes traffic across
three API containers. Interactive OpenAPI docs are at
`http://localhost:8000/docs`, and the dashboard is at
`http://localhost:8000/dashboard`.

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
docker compose exec api1 python -m pytest -q
```

Run the Phase 7 load test from a dedicated container on the Compose network:

```bash
docker compose run --rm \
  -e BASE_URL=http://nginx \
  api1 python scripts/load_test.py
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

## Phase 8: distributed mode and dashboard

### What changed

- Nginx is now the only public entry point and balances requests across
  `api-1`, `api-2`, and `api-3` using least-connections scheduling.
- Every API instance uses the same Redis configuration and limiter state.
- Responses include `X-RateLimiter-Instance`, making routing observable.
- The atomic Lua operation now records total, allowed, denied, mode, last-seen,
  and per-instance counts alongside each limiter decision.
- `/admin/metrics` lists recently active clients.
- `/admin/metrics/{client_key}` returns one client's cumulative metrics.
- `/dashboard` polls those metrics every second and calculates live RPS from
  changes between samples.

Metrics are deliberately updated inside the same Lua operation as the decision.
If they were incremented afterward in Python, a crash between the two writes
could produce an ALLOW or DENY that the dashboard never counted.

### Verify Phase 8 step by step

Open Docker Desktop and wait until its engine is running. Then open a PowerShell
terminal in this project and run:

```powershell
cd D:\token_bucket_rate_limiter
docker compose up --build -d
docker compose ps
```

You should see five running containers: `api1`, `api2`, `api3`, `nginx`, and
`redis`. Redis should say `healthy` and Nginx should publish port `8000`.

Confirm that Nginx reaches all three API instances:

```powershell
1..9 | ForEach-Object { (Invoke-RestMethod http://localhost:8000/health).instance }
```

The output should contain `api-1`, `api-2`, and `api-3`.

Run all unit tests:

```powershell
docker compose exec api1 python -m pytest -q
```

Run the distributed correctness test:

```powershell
docker compose run --rm -e BASE_URL=http://nginx api1 python scripts/load_test.py
```

It must finish with these four lines:

```text
PASS: 500+ requests/sec
PASS: no request errors
PASS: no token over-allocation
PASS: traffic reached 3+ API instances
```

Open `http://localhost:8000/dashboard` in a browser. Select the newest `load_...`
client. You should see its total ALLOW/DENY counts and a table containing all
three API instances. To watch the RPS card change live, keep the dashboard open
and run the distributed test again in a second PowerShell terminal.

### Measured distributed result (2026-07-09)

| Metric | Result |
|---|---:|
| API instances reached | 3 |
| Duration | 10.01 s |
| Concurrent connections | 100 |
| Completed requests | 101,185 |
| Throughput | 10,111 RPS |
| ALLOW | 1,044 |
| DENY | 100,141 |
| Request errors | 0 |
| p50 / p95 / p99 latency | 9.1 / 14.8 / 27.0 ms |
| Strict ALLOW upper bound | 1,050 |

Result: **PASS**. All three instances handled traffic while collectively
remaining below one shared mathematical allowance ceiling.

### Commit Phase 8

After verifying all four PASS lines:

```powershell
git add README.md app/main.py app/static/dashboard.html docker-compose.yml nginx/nginx.conf scripts/load_test.py tests/test_metrics.py
git commit -m "Phase 8: distributed replicas and live metrics dashboard"
git push origin main
git status
```

The final `git status` should say `nothing to commit, working tree clean`.

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
- **Stateless API replicas:** no limiter state is stored in a FastAPI process,
  so Nginx can send consecutive requests to different instances safely.
- **Observable routing:** an instance response header and Redis counters prove
  traffic reached multiple replicas instead of merely showing that they started.

## Resume bullet

Built a distributed FastAPI/Redis rate-limiter with per-client token-bucket and
sliding-window policies, three Nginx-balanced API replicas, atomic Lua decisions,
and a live metrics dashboard; validated 10.1K RPS across all replicas with zero
errors and no token over-allocation in a 101K-request correctness test.
