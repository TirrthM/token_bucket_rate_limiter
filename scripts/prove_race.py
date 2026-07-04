"""
Race condition prover.

Fires N truly-concurrent requests at ONE client whose bucket allows
exactly 5. If the limiter is correct: exactly 5 ALLOWs.
If the naive read->decide->write race exists: MORE than 5. Busted.
"""

import time
from concurrent.futures import ThreadPoolExecutor

import httpx

BASE = "http://localhost:8000"
N_REQUESTS = 100
CONCURRENCY = 50

# Fresh client key every run -> always starts with a full bucket.
CLIENT = f"race_{int(time.time())}"


def setup() -> None:
    """Burst of 5, near-zero refill so no tokens are earned mid-test."""
    r = httpx.post(f"{BASE}/admin/config", json={
        "client_key": CLIENT,
        "requests_per_second": 0.001,
        "burst_size": 5,
    })
    r.raise_for_status()


def fire(_: int) -> int:
    return httpx.post(f"{BASE}/check",
                      json={"client_key": CLIENT}, timeout=10).status_code


def main() -> None:
    setup()
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        codes = list(pool.map(fire, range(N_REQUESTS)))

    allowed = codes.count(200)
    denied = codes.count(429)
    print(f"client:  {CLIENT}")
    print(f"sent:    {N_REQUESTS} concurrent requests (limit is 5)")
    print(f"ALLOWED: {allowed}")
    print(f"DENIED:  {denied}")
    if allowed > 5:
        print(f"\n💥 RACE CONDITION: {allowed - 5} token(s) double-spent!")
    else:
        print("\n✅ correct: exactly the configured limit was allowed")


if __name__ == "__main__":
    main()