# Token Bucket Rate Limiter

A production-style FastAPI service that answers one question for other services: should this request be allowed or denied? The project combines a distributed rate-limiting engine, Redis-backed shared state, atomic Lua scripting, Docker-based orchestration, and a live dashboard into a single, deployable backend system.

This repository is designed to be both practical and interview-friendly. It demonstrates real engineering decisions around concurrency, state sharing, distributed systems, and correctness under load.

## Overview

This project implements:

- A public HTTP endpoint for rate-limit decisions
- Per-client configuration through admin endpoints
- Token-bucket and sliding-window policies
- Redis-backed shared state that survives restarts
- Atomic, race-safe decisions using Lua scripts in Redis
- Standard rate-limit response headers
- A metrics dashboard for live observability
- Docker Compose orchestration with Nginx, Redis, and three API replicas

## Architecture

```text
Client / upstream service
        |
        | POST /check {client_key}
        v
   Nginx (port 8000)
        |
   +---- API replica 1 ----+
   +---- API replica 2 ----+---- Redis (shared config, state, metrics)
   +---- API replica 3 ----+
        |
        v
  ALLOW (200) or DENY (429)
```

## Features

- Token bucket mode for sustainable throughput with controlled bursts
- Sliding-window mode for hard request-count ceilings over a recent interval
- Redis-backed state that survives service restart
- Race-condition-safe behavior under concurrency
- Standard headers:
  - `X-RateLimit-Limit`
  - `X-RateLimit-Remaining`
  - `X-RateLimit-Reset`
- Dashboard and metrics endpoints for observability
- Distributed deployment behind Nginx with multiple API replicas

## Quick start

### Prerequisites

Make sure the following are installed on your machine:

- Docker Desktop or Docker Engine with Compose
- Git
- PowerShell, CMD, Git Bash, or WSL

### 1. Clone the repository

```bash
git clone https://github.com/TirrthM/token_bucket_rate_limiter.git
cd token_bucket_rate_limiter
```

### 2. Start the services

```bash
docker compose up --build -d
docker compose ps
```

You should see five running containers:

- `api1`
- `api2`
- `api3`
- `nginx`
- `redis`

### 3. Verify the service is healthy

```bash
curl http://localhost:8000/health
```

Example response:

```json
{"status":"ok","redis_connected":true,"instance":"api-1"}
```

### 4. Open the user-facing endpoints

- API docs: http://localhost:8000/docs
- Dashboard: http://localhost:8000/dashboard

## Example usage

### Configure a token-bucket client

```bash
curl -X POST http://localhost:8000/admin/config \
  -H "Content-Type: application/json" \
  -d '{"client_key":"checkout","mode":"token_bucket","requests_per_second":5,"burst_size":20}'
```

### Configure a sliding-window client

```bash
curl -X POST http://localhost:8000/admin/config \
  -H "Content-Type: application/json" \
  -d '{"client_key":"reports","mode":"sliding_window","max_requests":100,"window_seconds":60}'
```

### Ask for a decision

```bash
curl -i -X POST http://localhost:8000/check \
  -H "Content-Type: application/json" \
  -d '{"client_key":"checkout"}'
```

Expected behavior:

- `200` means ALLOW
- `429` means DENY
- Both responses include the three standard rate-limit headers

## API reference

### Public endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/check` | `POST` | Returns ALLOW or DENY for a client key |
| `/health` | `GET` | Health check for the service |
| `/admin/config` | `POST` | Saves or updates per-client config |
| `/admin/config/{client_key}` | `GET` | Reads effective config for a client |
| `/admin/metrics` | `GET` | Shows recently active clients |
| `/admin/metrics/{client_key}` | `GET` | Shows metrics for one client |
| `/dashboard` | `GET` | Serves the live dashboard UI |

## Testing

Run the full test suite:

```bash
docker compose exec api1 python -m pytest -q
```

Run the distributed correctness load test:

```bash
docker compose run --rm -e BASE_URL=http://nginx api1 python scripts/load_test.py
```

Expected PASS lines:

```text
PASS: 500+ requests/sec
PASS: no request errors
PASS: no token over-allocation
PASS: traffic reached 3+ API instances
```

## Verified results

Latest verified distributed run:

| Metric | Result |
|---|---:|
| Duration | 10.01 s |
| Concurrent connections | 100 |
| Total requests sent | 72,074 |
| ALLOW | 1,044 |
| DENY | 71,030 |
| Request errors | 0 |
| API instances reached | 3 |
| p50 / p95 / p99 latency | 11.8 / 19.3 / 36.6 ms |
| Strict ALLOW upper bound | 1,051 |

Result: PASS. The service stayed below the mathematical allowance ceiling while serving traffic well above 500 RPS and reaching all three API replicas.

## Why this project is useful

This project is useful because it demonstrates several real backend engineering concerns in one place:

- Concurrency safety under high load
- Atomic operations in a distributed system
- Shared state across multiple replicas
- Algorithm choice between token bucket and sliding window
- Correctness testing under 500+ RPS
- Operational visibility through metrics and a dashboard

## Stop the stack

Stop the containers without deleting Redis data:

```bash
docker compose down
```

Delete all persisted data if you want a fresh start:

```bash
docker compose down -v
```

## Resume-ready summary

Built a distributed FastAPI/Redis rate-limiter with per-client token-bucket and sliding-window policies, three Nginx-balanced API replicas, atomic Lua decisions, and a live metrics dashboard; validated 500+ RPS with zero errors and no token over-allocation in a distributed correctness test.

