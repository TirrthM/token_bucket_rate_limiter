"""
Token Bucket Rate Limiter Service.

Phase 7 (perf): converted to ASYNC I/O.
- endpoints are `async def`
- uses redis.asyncio (non-blocking client)
While one request awaits Redis, the event loop serves others on the
same thread -> massively higher throughput than the sync thread-pool model.
Logic/algorithms are unchanged from Phase 6.
"""

import math
import os
from typing import Literal

import redis.asyncio as redis          # CHANGED: async Redis client
from fastapi import FastAPI, Response
from pydantic import BaseModel, Field

app = FastAPI(title="Token Bucket Rate Limiter")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
redis_client = redis.from_url(REDIS_URL, decode_responses=True)   # async pool

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

local reset = 0
if tokens < 1 then
  reset = (1 - tokens) / refill_rate
end

return {allowed, tostring(tokens), tostring(reset)}
"""

SLIDING_WINDOW_LUA = """
local key          = KEYS[1]
local max_requests = tonumber(ARGV[1])
local window       = tonumber(ARGV[2])

local t   = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000

redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)

local count   = redis.call('ZCARD', key)
local allowed = 0
if count < max_requests then
  allowed = 1
  redis.call('ZADD', key, now, tostring(now) .. '-' .. tostring(count))
  count = count + 1
end

local reset = 0
if count >= max_requests then
  local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
  if oldest[2] then
    reset = math.max(0, tonumber(oldest[2]) + window - now)
  end
end

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

# Phase 7 combines config lookup and the rate-limit decision into one Redis
# round trip. The operation is also atomic with respect to config changes.
RATE_LIMIT_LUA = """
local config_key = KEYS[1]
local bucket_key = KEYS[2]
local window_key = KEYS[3]

local config = redis.call(
  'HMGET', config_key, 'mode', 'requests_per_second', 'burst_size',
  'max_requests', 'window_seconds'
)
local mode = config[1] or ARGV[1]

local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000

if mode == 'sliding_window' then
  local max_requests = tonumber(config[4] or ARGV[4])
  local window = tonumber(config[5] or ARGV[5])
  redis.call('ZREMRANGEBYSCORE', window_key, '-inf', now - window)

  local count = redis.call('ZCARD', window_key)
  local allowed = 0
  if count < max_requests then
    allowed = 1
    redis.call('ZADD', window_key, now, tostring(now) .. '-' .. tostring(count))
    count = count + 1
  end

  local reset = 0
  if count >= max_requests then
    local oldest = redis.call('ZRANGE', window_key, 0, 0, 'WITHSCORES')
    if oldest[2] then
      reset = math.max(0, tonumber(oldest[2]) + window - now)
    end
  end

  redis.call('EXPIRE', window_key, math.ceil(window) + 1)
  return {
    allowed, tostring(math.max(0, max_requests - count)),
    tostring(reset), tostring(max_requests), mode
  }
end

local refill_rate = tonumber(config[2] or ARGV[2])
local capacity = tonumber(config[3] or ARGV[3])
local data = redis.call('HMGET', bucket_key, 'tokens', 'last_refill')
local tokens = capacity
local last_refill = now
if data[1] then
  tokens = tonumber(data[1])
  last_refill = tonumber(data[2])
end

local elapsed = math.max(0, now - last_refill)
tokens = math.min(capacity, tokens + elapsed * refill_rate)
local allowed = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
end
redis.call('HSET', bucket_key, 'tokens', tokens, 'last_refill', now)

local reset = 0
if tokens < 1 then
  reset = (1 - tokens) / refill_rate
end
return {allowed, tostring(tokens), tostring(reset), tostring(capacity), mode}
"""

rate_limit_script = redis_client.register_script(RATE_LIMIT_LUA)


class CheckRequest(BaseModel):
    client_key: str


class ConfigRequest(BaseModel):
    client_key: str
    mode: Literal["token_bucket", "sliding_window"] = DEFAULT_MODE
    requests_per_second: float = Field(default=DEFAULT_RPS, gt=0)
    burst_size: float = Field(default=DEFAULT_BURST, ge=1)
    max_requests: int = Field(default=DEFAULT_MAX_REQUESTS, ge=1)
    window_seconds: float = Field(default=DEFAULT_WINDOW_SECONDS, gt=0)


def bucket_key(client_key: str) -> str:
    return f"bucket:{client_key}"


def window_key(client_key: str) -> str:
    return f"window:{client_key}"


def config_key(client_key: str) -> str:
    return f"config:{client_key}"


async def load_config(client_key: str) -> dict:          # CHANGED: async + await
    raw = await redis_client.hgetall(config_key(client_key))
    return {
        "mode": raw.get("mode", DEFAULT_MODE),
        "requests_per_second": float(raw.get("requests_per_second", DEFAULT_RPS)),
        "burst_size": float(raw.get("burst_size", DEFAULT_BURST)),
        "max_requests": int(raw.get("max_requests", DEFAULT_MAX_REQUESTS)),
        "window_seconds": float(raw.get("window_seconds", DEFAULT_WINDOW_SECONDS)),
    }


@app.post("/check")
async def check_rate_limit(body: CheckRequest, response: Response) -> dict:  # async
    allowed, remaining, reset, limit, mode = await rate_limit_script(
        keys=[
            config_key(body.client_key),
            bucket_key(body.client_key),
            window_key(body.client_key),
        ],
        args=[
            DEFAULT_MODE,
            DEFAULT_RPS,
            DEFAULT_BURST,
            DEFAULT_MAX_REQUESTS,
            DEFAULT_WINDOW_SECONDS,
        ],
    )

    response.headers["X-RateLimit-Limit"] = str(int(limit))
    response.headers["X-RateLimit-Remaining"] = str(int(float(remaining)))
    response.headers["X-RateLimit-Reset"] = str(math.ceil(float(reset)))

    if not allowed:
        response.status_code = 429

    return {
        "decision": "ALLOW" if allowed else "DENY",
        "client_key": body.client_key,
        "mode": mode,
        "remaining": round(float(remaining), 3),
    }


@app.post("/admin/config")
async def set_config(body: ConfigRequest) -> dict:       # async + await
    await redis_client.hset(
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
async def get_config(client_key: str) -> dict:           # async + await
    cfg = await load_config(client_key)
    is_custom = bool(await redis_client.exists(config_key(client_key)))
    return {"client_key": client_key, **cfg,
            "source": "custom" if is_custom else "default"}


@app.get("/health")
async def health() -> dict:                              # async + await
    try:
        redis_ok = await redis_client.ping()
    except redis.ConnectionError:
        redis_ok = False
    return {"status": "ok" if redis_ok else "degraded", "redis_connected": redis_ok}
