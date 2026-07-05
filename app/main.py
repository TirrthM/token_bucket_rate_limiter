"""
Token Bucket Rate Limiter Service.

Phase 5: two algorithms, selectable per client via admin config.
- token_bucket   : allows bursts, smooth sustained rate (default)
- sliding_window : hard cap on requests in ANY rolling window (exact,
                   implemented as a timestamp log in a Redis sorted set)

Both algorithms run as atomic Lua scripts inside Redis (Phase 4 lesson:
mutual exclusion lives where the state lives).
"""

import os
from typing import Literal, Optional

import redis
from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, Field, model_validator

app = FastAPI(title="Token Bucket Rate Limiter")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)

# ---------------------------------------------------------------------------
# Lua script 1: token bucket (unchanged from Phase 4)
# ---------------------------------------------------------------------------
TOKEN_BUCKET_LUA = """
local key         = KEYS[1]
local capacity    = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])

local t   = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000

local data = redis.call('HMGET', key, 'tokens', 'last_refill')
local tokens, last_refill
if data[1] then
  tokens      = tonumber(data[1])
  last_refill = tonumber(data[2])
else
  tokens      = capacity
  last_refill = now
end

local elapsed = math.max(0, now - last_refill)
tokens = math.min(capacity, tokens + elapsed * refill_rate)

local allowed = 0
if tokens >= 1 then
  tokens  = tokens - 1
  allowed = 1
end

redis.call('HSET', key, 'tokens', tokens, 'last_refill', now)
return {allowed, tostring(tokens)}
"""

# ---------------------------------------------------------------------------
# Lua script 2: sliding window log (NEW)
# Sorted set: score = timestamp of each allowed request.
#   1) drop entries older than the window
#   2) count survivors
#   3) if under the limit -> record this request -> ALLOW
# All three steps atomic. EXPIRE cleans up clients that go silent forever.
# ---------------------------------------------------------------------------
SLIDING_WINDOW_LUA = """
local key          = KEYS[1]
local max_requests = tonumber(ARGV[1])
local window       = tonumber(ARGV[2])

local t   = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000

redis.call('ZREMRANGEBYSCORE', key, 0, now - window)

local count = redis.call('ZCARD', key)
local allowed = 0
if count < max_requests then
  -- member must be unique; a per-key counter guarantees it even if two
  -- requests share the same microsecond timestamp
  local seq = redis.call('INCR', key .. ':seq')
  redis.call('ZADD', key, now, tostring(now) .. '-' .. tostring(seq))
  allowed = 1
  count = count + 1
end

-- auto-delete state for clients that vanish (no memory leak)
redis.call('EXPIRE', key, math.ceil(window) + 60)
redis.call('EXPIRE', key .. ':seq', math.ceil(window) + 60)

return {allowed, max_requests - count}
"""

token_bucket_script = redis_client.register_script(TOKEN_BUCKET_LUA)
sliding_window_script = redis_client.register_script(SLIDING_WINDOW_LUA)


# ---------- models ----------

class CheckRequest(BaseModel):
    client_key: str


class ConfigRequest(BaseModel):
    """Admin config. Which fields are required depends on the mode."""
    client_key: str
    mode: Literal["token_bucket", "sliding_window"] = "token_bucket"
    # token_bucket fields
    requests_per_second: Optional[float] = Field(default=None, gt=0)
    burst_size: Optional[float] = Field(default=None, ge=1)
    # sliding_window fields
    max_requests: Optional[int] = Field(default=None, ge=1)
    window_seconds: Optional[float] = Field(default=None, gt=0)

    @model_validator(mode="after")
    def check_fields_for_mode(self) -> "ConfigRequest":
        """Reject configs missing the fields their mode needs."""
        if self.mode == "token_bucket":
            if self.requests_per_second is None or self.burst_size is None:
                raise ValueError(
                    "token_bucket mode requires requests_per_second and burst_size")
        else:
            if self.max_requests is None or self.window_seconds is None:
                raise ValueError(
                    "sliding_window mode requires max_requests and window_seconds")
        return self


# ---------- redis key helpers ----------

def bucket_key(client_key: str) -> str:
    return f"bucket:{client_key}"


def window_key(client_key: str) -> str:
    return f"window:{client_key}"


def config_key(client_key: str) -> str:
    return f"config:{client_key}"


# ---------- config load ----------

DEFAULT_CONFIG = {"mode": "token_bucket",
                  "requests_per_second": 1.0, "burst_size": 5.0}


def load_config(client_key: str) -> dict:
    raw = redis_client.hgetall(config_key(client_key))
    if not raw:
        return DEFAULT_CONFIG
    return raw  # values are strings; converted where used


# ---------- endpoints ----------

@app.post("/check")
def check_rate_limit(body: CheckRequest, response: Response) -> dict:
    cfg = load_config(body.client_key)
    mode = cfg["mode"]

    if mode == "token_bucket":
        allowed, remaining = token_bucket_script(
            keys=[bucket_key(body.client_key)],
            args=[float(cfg["burst_size"]), float(cfg["requests_per_second"])],
        )
        remaining = round(float(remaining), 3)
    else:  # sliding_window
        allowed, remaining = sliding_window_script(
            keys=[window_key(body.client_key)],
            args=[int(cfg["max_requests"]), float(cfg["window_seconds"])],
        )

    if not allowed:
        response.status_code = 429

    return {
        "decision": "ALLOW" if allowed else "DENY",
        "client_key": body.client_key,
        "mode": mode,
        "remaining": remaining,
    }


@app.post("/admin/config")
def set_config(body: ConfigRequest) -> dict:
    mapping = {"mode": body.mode}
    if body.mode == "token_bucket":
        mapping["requests_per_second"] = body.requests_per_second
        mapping["burst_size"] = body.burst_size
    else:
        mapping["max_requests"] = body.max_requests
        mapping["window_seconds"] = body.window_seconds

    # Replace (not merge): delete leftovers from a previous mode first,
    # and clear old live state so the client starts clean under new rules.
    redis_client.delete(config_key(body.client_key),
                        bucket_key(body.client_key),
                        window_key(body.client_key))
    redis_client.hset(config_key(body.client_key), mapping=mapping)
    return {"message": f"config saved for '{body.client_key}'", **mapping}


@app.get("/admin/config/{client_key}")
def get_config(client_key: str) -> dict:
    cfg = load_config(client_key)
    is_custom = bool(redis_client.exists(config_key(client_key)))
    return {"client_key": client_key,
            "source": "custom" if is_custom else "default", **cfg}


@app.get("/health")
def health() -> dict:
    try:
        redis_ok = redis_client.ping()
    except redis.exceptions.ConnectionError:
        redis_ok = False
    return {"status": "ok" if redis_ok else "degraded",
            "redis_connected": redis_ok}