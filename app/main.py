"""
Token Bucket Rate Limiter Service.

Phase 2: the /check endpoint — read state from Redis, decide with the
pure algorithm, write state back.

KNOWN LIMITATION (deliberate): the read->decide->write cycle is NOT
atomic yet. Two concurrent requests for the same client can read the
same state and double-spend a token. Proven and fixed in Phase 4.
"""

import os
import time

import redis
from fastapi import FastAPI, Response
from pydantic import BaseModel

from app.token_bucket import BucketConfig, BucketState, check, new_bucket

app = FastAPI(title="Token Bucket Rate Limiter")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)

# Phase 2: one shared default config for every client.
# Phase 3 replaces this with per-client config via an admin endpoint.
DEFAULT_CONFIG = BucketConfig(capacity=5, refill_rate=1)


class CheckRequest(BaseModel):
    """Body of POST /check. Pydantic validates it automatically:
    a request without client_key is rejected with 422 before our code runs."""
    client_key: str


def bucket_key(client_key: str) -> str:
    """Namespaced Redis key: one flat keyspace, so prefixes act as folders."""
    return f"bucket:{client_key}"


def load_state(client_key: str, now: float) -> BucketState:
    """READ step. Missing hash = first time we see this client = full bucket."""
    raw = redis_client.hgetall(bucket_key(client_key))
    if not raw:
        return new_bucket(DEFAULT_CONFIG, now)
    # Redis stores strings; convert back to floats.
    return BucketState(
        tokens=float(raw["tokens"]),
        last_refill=float(raw["last_refill"]),
    )


def save_state(client_key: str, state: BucketState) -> None:
    """WRITE step."""
    redis_client.hset(
        bucket_key(client_key),
        mapping={"tokens": state.tokens, "last_refill": state.last_refill},
    )


@app.post("/check")
def check_rate_limit(body: CheckRequest, response: Response) -> dict:
    """
    The core endpoint: ALLOW or DENY for one client's request.
    HTTP 200 = ALLOW, HTTP 429 (Too Many Requests) = DENY.
    """
    now = time.time()  # the ONE place we read the real clock

    state = load_state(body.client_key, now)          # READ
    # <-- race window: another request can read the same state right here
    decision = check(DEFAULT_CONFIG, state, now)      # DECIDE (pure, tested)
    save_state(body.client_key, decision.new_state)   # WRITE

    if not decision.allowed:
        response.status_code = 429  # standard "Too Many Requests"

    return {
        "decision": "ALLOW" if decision.allowed else "DENY",
        "client_key": body.client_key,
        "remaining": round(decision.remaining, 3),
    }


@app.get("/health")
def health() -> dict:
    """Liveness check: proves app AND Redis are reachable."""
    try:
        redis_ok = redis_client.ping()
    except redis.exceptions.ConnectionError:
        redis_ok = False
    return {"status": "ok" if redis_ok else "degraded", "redis_connected": redis_ok}