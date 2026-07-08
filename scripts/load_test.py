"""
Load test: sustained concurrent fire at ONE client, proving the limiter
never allows more than the mathematical ceiling:
    max_allowed = capacity + refill_rate * elapsed_seconds
Also reports throughput (RPS) and latency percentiles.
"""

import asyncio
import math
import os
import sys
import time
from urllib.parse import urlparse

import httpx

BASE = os.getenv("BASE_URL", "http://localhost:8000")
CLIENT = f"load_{time.time_ns()}"   # fresh client every run

DURATION = float(os.getenv("DURATION_SECONDS", "10"))
CONCURRENCY = int(os.getenv("CONCURRENCY", "100"))
CAPACITY = 50          # burst size for the test client
REFILL = 100           # tokens/sec for the test client
MINIMUM_RPS = 500      # Phase 7 acceptance threshold

results: list[tuple[int, float]] = []   # (status_code, latency_seconds)


async def setup() -> None:
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{BASE}/admin/config", json={
            "client_key": CLIENT,
            "mode": "token_bucket",
            "requests_per_second": REFILL,
            "burst_size": CAPACITY,
        })
        r.raise_for_status()


async def worker(host: str, port: int, deadline: float) -> None:
    """Send requests over one persistent connection until the deadline."""
    body = f'{{"client_key":"{CLIENT}"}}'.encode()
    request = (
        f"POST /check HTTP/1.1\r\nHost: {host}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\nConnection: keep-alive\r\n\r\n"
    ).encode() + body

    try:
        reader, writer = await asyncio.open_connection(host, port)
        while time.perf_counter() < deadline:
            t0 = time.perf_counter()
            writer.write(request)
            await writer.drain()
            raw_headers = await reader.readuntil(b"\r\n\r\n")
            header_lines = raw_headers.decode("latin-1").split("\r\n")
            status_code = int(header_lines[0].split()[1])
            content_length = next(
                int(line.split(":", 1)[1])
                for line in header_lines[1:]
                if line.lower().startswith("content-length:")
            )
            await reader.readexactly(content_length)
            results.append((status_code, time.perf_counter() - t0))
        writer.close()
        await writer.wait_closed()
    except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        results.append((0, 0.0))


async def main() -> None:
    await setup()
    parsed_base = urlparse(BASE)
    if parsed_base.scheme != "http" or not parsed_base.hostname:
        raise ValueError("BASE_URL must be an http:// URL")
    host = parsed_base.hostname
    port = parsed_base.port or 80
    start = time.perf_counter()
    deadline = start + DURATION

    await asyncio.gather(*(worker(host, port, deadline)
                           for _ in range(CONCURRENCY)))

    elapsed = time.perf_counter() - start
    total = len(results)
    allowed = sum(1 for s, _ in results if s == 200)
    denied = sum(1 for s, _ in results if s == 429)
    errors = total - allowed - denied

    lat = sorted(l for _, l in results)
    p50 = lat[int(0.50 * len(lat))] * 1000
    p95 = lat[int(0.95 * len(lat))] * 1000
    p99 = lat[int(0.99 * len(lat))] * 1000

    # This interval surrounds every server decision, so it is already a
    # conservative upper bound and needs no arbitrary timing tolerance.
    ceiling = math.floor(CAPACITY + REFILL * elapsed)
    throughput = total / elapsed

    print(f"client:        {CLIENT}")
    print(f"duration:      {elapsed:.2f}s   concurrency: {CONCURRENCY}")
    print(f"total sent:    {total}   ({throughput:.0f} req/sec)")
    print(f"ALLOWED:       {allowed}")
    print(f"DENIED:        {denied}")
    print(f"errors:        {errors}")
    print(f"latency ms:    p50={p50:.1f}  p95={p95:.1f}  p99={p99:.1f}")
    print(f"ceiling:       {ceiling} (= floor({CAPACITY} burst "
          f"+ {REFILL}/s x {elapsed:.2f}s))")

    checks = {
        "500+ requests/sec": throughput >= MINIMUM_RPS,
        "no request errors": errors == 0,
        "no token over-allocation": allowed <= ceiling,
    }
    for name, passed in checks.items():
        print(f"{'PASS' if passed else 'FAIL'}: {name}")

    if not all(checks.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
