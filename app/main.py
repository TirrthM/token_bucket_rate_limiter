"""
Token Bucket Rate Limiter Service.

Phase 3: per-client configuration via an admin endpoint.
- config:{client}  -> the rules   (requests_per_second, burst_size)
- bucket:{client}  -> the state   (tokens, last_refill)
Unknown clients fall back to DEFAULT_CONFIG.

KNOWN LIMITATION (deliberate): read->decide->write still not atomic.
Fixed in Phase 4.
"""

import os
import time

import redis
from fastapi import FastAPI, Response
from pydantic import BaseModel, Field

from app.token_bucket import BucketConfig, BucketState, check, new_bucket

app = FastAPI(title="Token Bucket Rate Limiter")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)

DEFAULT_CONFIG = BucketConfig(capacity=5, refill_rate=1)


# ---------- request/response models ----------

class CheckRequest(BaseModel):
    client_key: str


class ConfigRequest(BaseModel):
    """Admin sets a client's limits. Field(...) adds validation:
    gt=0 -> must be greater than 0, ge=1 -> at least 1.
    Bad input is rejected with 422 before our code even runs."""
    client_key: str
    requests_per_second: float = Field(gt=0)
    burst_size: float = Field(ge=1)


# ---------- redis key helpers ----------

def bucket_key(client_key: str) -> str:
    return f"bucket:{client_key}"


def config_key(client_key: str) -> str:
    return f"config:{client_key}"


# ---------- load/save ----------

def load_config(client_key: str) -> BucketConfig:
    """Custom config if the admin set one, otherwise the safe default."""
    raw = redis_client.hgetall(config_key(client_key))
    if not raw:
        return DEFAULT_CONFIG
    return BucketConfig(
        capacity=float(raw["burst_size"]),
        refill_rate=float(raw["requests_per_second"]),
    )


def load_state(client_key: str, config: BucketConfig, now: float) -> BucketState:
    raw = redis_client.hgetall(bucket_key(client_key))
    if not raw:
        return new_bucket(config, now)
    return BucketState(
        tokens=float(raw["tokens"]),
        last_refill=float(raw["last_refill"]),
    )


def save_state(client_key: str, state: BucketState) -> None:
    redis_client.hset(
        bucket_key(client_key),
        mapping={"tokens": state.tokens, "last_refill": state.last_refill},
    )


# ---------- endpoints ----------

@app.post("/check")
def check_rate_limit(body: CheckRequest, response: Response) -> dict:
    now = time.time()
    config = load_config(body.client_key)             # NEW: per-client rules
    state = load_state(body.client_key, config, now)  # READ
    decision = check(config, state, now)              # DECIDE (pure, tested)
    save_state(body.client_key, decision.new_state)   # WRITE

    if not decision.allowed:
        response.status_code = 429

    return {
        "decision": "ALLOW" if decision.allowed else "DENY",
        "client_key": body.client_key,
        "remaining": round(decision.remaining, 3),
    }


@app.post("/admin/config")
def set_config(body: ConfigRequest) -> dict:
    """Create or update a client's limits. Takes effect on their next request."""
    redis_client.hset(
        config_key(body.client_key),
        mapping={
            "requests_per_second": body.requests_per_second,
            "burst_size": body.burst_size,
        },
    )
    return {
        "message": f"config saved for '{body.client_key}'",
        "requests_per_second": body.requests_per_second,
        "burst_size": body.burst_size,
    }


@app.get("/admin/config/{client_key}")
def get_config(client_key: str) -> dict:
    """Inspect a client's effective limits (custom or default)."""
    cfg = load_config(client_key)
    is_custom = bool(redis_client.exists(config_key(client_key)))
    return {
        "client_key": client_key,
        "requests_per_second": cfg.refill_rate,
        "burst_size": cfg.capacity,
        "source": "custom" if is_custom else "default",
    }


@app.get("/health")
def health() -> dict:
    try:
        redis_ok = redis_client.ping()
    except redis.exceptions.ConnectionError:
        redis_ok = False
    return {"status": "ok" if redis_ok else "degraded", "redis_connected": redis_ok}