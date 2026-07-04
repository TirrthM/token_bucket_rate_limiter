"""
Token Bucket Rate Limiter Service.

Phase 4: race-condition-safe. The whole read->refill->decide->write
cycle now runs as ONE atomic Lua script inside Redis. There is no
longer any gap for a concurrent request to exploit.

Design notes:
- Mutual exclusion lives where the state lives (Redis), because app-level
  locks don't span processes or machines.
- Time comes from Redis's own clock (TIME) so multiple app instances
  share one time source (no clock skew between instances).
- app/token_bucket.py remains the unit-tested reference spec of the
  algorithm; this Lua script is its production twin.
"""

import os

import redis
from fastapi import FastAPI, Response
from pydantic import BaseModel, Field

from app.token_bucket import BucketConfig

app = FastAPI(title="Token Bucket Rate Limiter")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)

DEFAULT_CONFIG = BucketConfig(capacity=5, refill_rate=1)

# ---------------------------------------------------------------------------
# The atomic token bucket, as a Lua script executed INSIDE Redis.
# Redis runs the entire script as one uninterruptible command:
# no other client's command can interleave. The race window is gone.
# ---------------------------------------------------------------------------
TOKEN_BUCKET_LUA = """
local key         = KEYS[1]
local capacity    = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])

-- Redis's own clock: seconds + microseconds -> float seconds.
local t   = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000

-- READ (inside the atomic block)
local data = redis.call('HMGET', key, 'tokens', 'last_refill')
local tokens, last_refill
if data[1] then
  tokens      = tonumber(data[1])
  last_refill = tonumber(data[2])
else
  -- first sight of this client: full bucket
  tokens      = capacity
  last_refill = now
end

-- LAZY REFILL (same math as token_bucket.py)
local elapsed = math.max(0, now - last_refill)
tokens = math.min(capacity, tokens + elapsed * refill_rate)

-- DECIDE + SPEND
local allowed = 0
if tokens >= 1 then
  tokens  = tokens - 1
  allowed = 1
end

-- WRITE (still inside the same atomic block)
redis.call('HSET', key, 'tokens', tokens, 'last_refill', now)

-- tostring(): Lua->Redis converts numbers to INTEGERS (would truncate
-- 0.84 to 0!). Strings survive the trip intact.
return {allowed, tostring(tokens)}
"""

# register_script: uploads once, then each call sends only the script's
# SHA1 hash (EVALSHA) instead of re-sending the whole text. Fast.
check_script = redis_client.register_script(TOKEN_BUCKET_LUA)


# ---------- models ----------

class CheckRequest(BaseModel):
    client_key: str


class ConfigRequest(BaseModel):
    client_key: str
    requests_per_second: float = Field(gt=0)
    burst_size: float = Field(ge=1)


# ---------- redis key helpers ----------

def bucket_key(client_key: str) -> str:
    return f"bucket:{client_key}"


def config_key(client_key: str) -> str:
    return f"config:{client_key}"


def load_config(client_key: str) -> BucketConfig:
    raw = redis_client.hgetall(config_key(client_key))
    if not raw:
        return DEFAULT_CONFIG
    return BucketConfig(
        capacity=float(raw["burst_size"]),
        refill_rate=float(raw["requests_per_second"]),
    )


# ---------- endpoints ----------

@app.post("/check")
def check_rate_limit(body: CheckRequest, response: Response) -> dict:
    config = load_config(body.client_key)

    # ONE atomic operation replaces the old READ -> DECIDE -> WRITE.
    allowed, remaining = check_script(
        keys=[bucket_key(body.client_key)],
        args=[config.capacity, config.refill_rate],
    )

    if not allowed:
        response.status_code = 429

    return {
        "decision": "ALLOW" if allowed else "DENY",
        "client_key": body.client_key,
        "remaining": round(float(remaining), 3),
    }


@app.post("/admin/config")
def set_config(body: ConfigRequest) -> dict:
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