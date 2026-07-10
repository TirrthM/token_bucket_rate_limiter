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

## Why these design choices are defensible in an interview

### Why token bucket was chosen

A token bucket is a good fit for an API gateway-style limiter because it models two things at once: a long-run average rate and a short-lived burst allowance. In practical terms, the service can sustain a steady stream of requests while still permitting a temporary spike when a client has accumulated tokens. That is often more useful than a strict fixed window, which can feel unfair because a client that sends a burst at the start of a window may be denied immediately afterward even though it has not exceeded its long-run limit.

A leaky bucket is smoother but less flexible. It is excellent when you want to shape traffic into a steady drain, but it is a poor fit when your business rule is "allow a burst, then keep the average under control." Token bucket better matches that requirement.

Sliding window is still useful, and this project supports it as a second policy. It answers a stricter question: "How many requests have I seen in the last $N$ seconds?" That is ideal when the product requirement is a hard upper bound over a recent interval rather than a burst-friendly average.

### What burst capacity means

The bucket capacity is the maximum number of tokens that can be saved up in advance. A larger capacity means a larger burst. The refill rate is the sustainable throughput. For example, a client configured with a capacity of 20 and a refill rate of 5 tokens per second can immediately spend up to 20 requests, then must wait for the bucket to refill at 5 requests per second afterward. That distinction is important in interviews because it shows that the system is not just "allowing $N$ requests per second"; it is balancing burst tolerance and long-run fairness.

### Why the refill is lazy and fractional

The implementation uses lazy refill: it does not maintain a background thread that continuously adds tokens. Instead, each decision computes how many tokens were earned since the last update and applies that on demand. This keeps the state model simple and avoids unnecessary work. It also makes the algorithm easy to reason about: each request is evaluated against the current time and the last known state.

Fractional tokens are required because the refill rate and elapsed time are rarely whole numbers. A client may earn 0.4 tokens over 100 milliseconds or 1.5 tokens over 300 milliseconds. Rounding would create visible correctness errors, especially at high concurrency or under small time windows. The implementation therefore stores and reuses fractional tokens and never lets the bucket exceed capacity.

### Why a denied request still updates refill state

This is subtle but important. Even when a request is denied, the bucket state should still advance to the current time because the client may have earned new tokens while waiting. If the system ignored that update on a denied request, later decisions would be based on stale state and would undercount what the client had already earned. This keeps the model monotonic and consistent over time.

### Why Redis server time is used

The most reliable shared clock in this system is Redis itself. Every API replica can ask Redis for the current time inside the same atomic operation, which means all replicas agree on the same reference point when deciding whether to refill or deny. Application-server clocks can drift or disagree, and that would create inconsistent decisions across replicas. Using Redis time also gives us a shared, centralized notion of time for the algorithm and for the sliding-window expiry.

The implementation also guards against backward clock movement by clamping elapsed time to zero. That prevents a request from accidentally earning negative time or causing the bucket to lose tokens because the clock moved backward.

### Why a Python lock is not enough

A standard in-process lock can prevent race conditions only within one Python process. It does not protect two API replicas that are handling requests at the same time, and it does not protect a process from being restarted between the read and write steps. The real failure mode is a classic read-modify-write race: two requests can both observe the same pre-spend bucket state, both decide that a token is available, and both spend it. That is the exact bug the project is designed to prevent.

### Why Redis Lua is the atomic primitive

The limiter uses a single Redis Lua script to perform policy lookup and state mutation together in one Redis execution. That matters because the check for "is there enough capacity?" and the write of "consume one token" must happen as one indivisible operation. The script is evaluated atomically on the Redis server, so concurrent requests cannot interleave and double-spend a token. This is the core distributed-systems lesson the project demonstrates.

### Why the API containers stay stateless

Each API replica stores no durable limiter state in memory. It only reads and writes shared state in Redis. That makes horizontal scaling straightforward: Nginx can balance traffic across multiple replicas and every request can be answered correctly because the shared state lives in Redis, not in a specific container. The API layer remains stateless, which is a strong engineering pattern for services that must scale.

### Why the config lookup and token spend happen together

The combined Lua script makes the configuration and the decision part of the same atomic transaction. If a config change lands between reading the policy and spending the token, the decision could be made against an outdated policy. By performing both steps in one operation, the service guarantees that every decision is evaluated against a consistent view of the effective configuration.

### Edge cases the implementation is designed to handle

- A brand-new client starts with a full bucket rather than an empty one.
- Fractional tokens accumulate correctly over time.
- The bucket never exceeds its configured capacity.
- Negative token values cannot appear from clock drift or stale state.
- Sliding-window entries are cleaned up as time passes.
- Redis persistence and expiry keep the state durable across restarts and prevent unbounded growth.

This combination of token bucket, sliding window, Redis-backed state, and atomic Lua evaluation is what makes the service feel like a real networked product rather than a toy script.

## Edge cases and operational notes

A few edge cases matter in a real deployment, and the implementation is designed with them in mind:

- A new client starts with a full bucket on first use so the very first request does not get rejected by accident.
- Fractional tokens accumulate over elapsed time, which is why the bucket stores floats rather than whole integers.
- The bucket is capped at its configured capacity so a client cannot save up an unbounded burst.
- Negative token values are prevented by clamping elapsed time to zero when the clock moves backward.
- Sliding-window entries are pruned as time passes so expired requests do not keep the window artificially full.
- Redis keys are allowed to expire naturally, while token-bucket state remains persistent across restarts because the bucket data itself is stored in Redis.
- Configuration changes do not retroactively rewrite prior bucket state; they affect future decisions from the next evaluation onward.
- Sliding-window timestamps are unique because the script uses the request counter as part of the score payload, avoiding accidental collisions.
- Redis persistence is backed by AOF so state survives a process restart, subject to Redis durability settings.
- The dashboard handles empty metrics gracefully and simply reports zero activity for clients that have not been observed yet.

These are the kinds of operational details that interviewers often ask about because they separate a toy demo from a production-minded service.

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
- **Async Redis client:** workers do not block a thread while waiting for Redis,
  which helps the service sustain high concurrency.
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
