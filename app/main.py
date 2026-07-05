"""
Token Bucket Rate Limiter Service.

Phase 6: standard rate-limit headers on every response:
  X-RateLimit-Limit, X-RateLimit-Remaining, X-RateLimit-Reset
Both Lua scripts now also compute and return "reset" =
seconds until the client regains capacity.
"""

import math
import os
from typing import Literal

import redis
from fastapi import FastAPI, Response
from pydantic import BaseModel, Field

app = FastAPI(title="Token Bucket Rate Limiter")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)

# ---------------------------------------------------------------------------
# Lua script 1: token bucket (atomic). Returns {allowed, remaining, reset}.
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

-- reset: seconds until at least 1 token exists (0 if one already does)
local reset = 0
if tokens < 1 then
  reset = (1 - tokens) / refill_rate
end

return {allowed, tostring(tokens), tostring(reset)}
"""

# ---------------------------------------------------------------------------
# Lua script 2: sliding window (atomic). Returns {allowed, remaining, reset}.
# ---------------------------------------------------------------------------
SLIDING_WINDOW_LUA = """
local key          = KEYS[1]
local max_requests = tonumber(ARGV[1])
local window       = tonumber(ARGV[2])

local t   = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000

-- slide: drop entries older than the window
redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)

local count   = redis.call('ZCARD', key)
local allowed = 0
if count < max_requests then
  allowed = 1
  redis.call('ZADD', key, now, tostring(now) .. '-' .. tostring(count))
  count = count + 1
end

-- reset: when the OLDEST logged request ages out of the window
local reset = 0
if count >= max_requests then
  local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
  if oldest[2] then
    reset = math.max(0, tonumber(oldest[2]) + window - now)
  end
end

-- self-cleanup: idle clients' logs expire on their own
redis.call('EXPIRE', key, math.ceil(window) + 1)

local remaining = math.max(0, max_requests - count)
return {allowed, tostring(remaining), tostring(reset)}
"""

token_bucket_script = redis_client.register_script(TOKEN_BUCKET_LUA)
sliding_window_script = redis_client.register_script(SLIDING_WINDOW_LUA)

DEFAULT_MODE = "token_bucket"
DEFAULT_RPS = 1.0
DEFAULT_BURST = 5.0
DEFAULT_MAX_REQUESTS = 10
DEFAULT_WINDOW_SECONDS = 60.0


# ---------- models ----------

class CheckRequest(BaseModel):
    client_key: str


class ConfigRequest(BaseModel):
    client_key: str
    mode: Literal["token_bucket", "sliding_window"] = DEFAULT_MODE
    # token bucket settings
    requests_per_second: float = Field(default=DEFAULT_RPS, gt=0)
    burst_size: float = Field(default=DEFAULT_BURST, ge=1)
    # sliding window settings
    max_requests: int = Field(default=DEFAULT_MAX_REQUESTS, ge=1)
    window_seconds: float = Field(default=DEFAULT_WINDOW_SECONDS, gt=0)


# ---------- redis keys ----------

def bucket_key(client_key: str) -> str:
    return f"bucket:{client_key}"


def window_key(client_key: str) -> str:
    return f"window:{client_key}"


def config_key(client_key: str) -> str:
    return f"config:{client_key}"


def load_config(client_key: str) -> dict:
    raw = redis_client.hgetall(config_key(client_key))
    return {
        "mode": raw.get("mode", DEFAULT_MODE),
        "requests_per_second": float(raw.get("requests_per_second", DEFAULT_RPS)),
        "burst_size": float(raw.get("burst_size", DEFAULT_BURST)),
        "max_requests": int(raw.get("max_requests", DEFAULT_MAX_REQUESTS)),
        "window_seconds": float(raw.get("window_seconds", DEFAULT_WINDOW_SECONDS)),
    }


# ---------- endpoints ----------

@app.post("/check")
def check_rate_limit(body: CheckRequest, response: Response) -> dict:
    cfg = load_config(body.client_key)

    if cfg["mode"] == "sliding_window":
        limit = cfg["max_requests"]
        allowed, remaining, reset = sliding_window_script(
            keys=[window_key(body.client_key)],
            args=[cfg["max_requests"], cfg["window_seconds"]],
        )
    else:
        limit = cfg["burst_size"]
        allowed, remaining, reset = token_bucket_script(
            keys=[bucket_key(body.client_key)],
            args=[cfg["burst_size"], cfg["requests_per_second"]],
        )

    # --- Req #6: standard headers on EVERY response (allow and deny) ---
    # Headers are strings by spec. Remaining is floored (a client with 0.8
    # tokens can't make a request, so advertising "0" is the honest value).
    response.headers["X-RateLimit-Limit"] = str(int(limit))
    response.headers["X-RateLimit-Remaining"] = str(int(float(remaining)))
    response.headers["X-RateLimit-Reset"] = str(math.ceil(float(reset)))

    if not allowed:
        response.status_code = 429

    return {
        "decision": "ALLOW" if allowed else "DENY",
        "client_key": body.client_key,
        "mode": cfg["mode"],
        "remaining": round(float(remaining), 3),
    }


@app.post("/admin/config")
def set_config(body: ConfigRequest) -> dict:
    redis_client.hset(
        config_key(body.client_key),
        mapping={
            "mode": body.mode,
            "requests_per_second": body.requests_per_second,
            "burst_size": body.burst_size,
            "max_requests": body.max_requests,
            "window_seconds": body.window_seconds,
        },
    )
    return {
        "message": f"config saved for '{body.client_key}'",
        "mode": body.mode,
        "max_requests": body.max_requests,
        "window_seconds": body.window_seconds,
    }


@app.get("/admin/config/{client_key}")
def get_config(client_key: str) -> dict:
    cfg = load_config(client_key)
    is_custom = bool(redis_client.exists(config_key(client_key)))
    return {"client_key": client_key, **cfg,
            "source": "custom" if is_custom else "default"}


@app.get("/health")
def health() -> dict:
    try:
        redis_ok = redis_client.ping()
    except redis.exceptions.ConnectionError:
        redis_ok = False
    return {"status": "ok" if redis_ok else "degraded", "redis_connected": redis_ok}