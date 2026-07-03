"""
Token Bucket Rate Limiter Service.

Phase 0: scaffold only — a FastAPI app that can talk to Redis.
The /health endpoint proves the whole pipeline works:
HTTP request -> our app -> Redis -> back to the caller.
"""

import os

import redis
from fastapi import FastAPI

app = FastAPI(title="Token Bucket Rate Limiter")

# Where is Redis? Read from an environment variable so the SAME code works
# everywhere: in Docker (where the host is "redis") and locally
# (where it would be "localhost"). Hardcoding hosts is a classic beginner trap.
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# One shared connection pool for the whole app (creating a new connection
# per request would be slow and wasteful).
# decode_responses=True -> Redis replies arrive as normal Python strings,
# not raw bytes (b"3" vs "3").
redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)


@app.get("/health")
def health() -> dict:
    """
    Liveness check. Returns ok only if BOTH our app AND Redis are reachable.
    PING is Redis's built-in "are you alive?" command -> replies True.
    """
    try:
        redis_ok = redis_client.ping()
    except redis.exceptions.ConnectionError:
        redis_ok = False

    return {
        "status": "ok" if redis_ok else "degraded",
        "redis_connected": redis_ok,
    }