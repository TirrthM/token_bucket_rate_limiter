"""Black-box integration tests for the running distributed service.

These tests deliberately use HTTP instead of importing FastAPI internals. They
verify that Nginx, each API replica, Redis, Lua, and response serialization work
together. Start Docker Compose before running this module.
"""

import os
from uuid import uuid4

import httpx
import pytest

BASE_URL = os.getenv("INTEGRATION_BASE_URL", "http://nginx")
REPLICA_URLS = os.getenv(
    "INTEGRATION_REPLICA_URLS",
    "http://api1:8000,http://api2:8000,http://api3:8000",
).split(",")
TIMEOUT = 10.0

pytestmark = pytest.mark.integration


def unique_client(prefix: str) -> str:
    """Return an isolated Redis namespace for one test."""
    return f"integration_{prefix}_{uuid4().hex}"


def save_config(client_key: str, **overrides: object) -> httpx.Response:
    payload: dict[str, object] = {
        "client_key": client_key,
        "mode": "token_bucket",
        "requests_per_second": 0.001,
        "burst_size": 3,
    }
    payload.update(overrides)
    response = httpx.post(
        f"{BASE_URL}/admin/config", json=payload, timeout=TIMEOUT
    )
    response.raise_for_status()
    return response


def check(client_key: str, base_url: str = BASE_URL) -> httpx.Response:
    return httpx.post(
        f"{base_url}/check",
        json={"client_key": client_key},
        timeout=TIMEOUT,
    )


def assert_rate_limit_headers(response: httpx.Response) -> None:
    assert response.headers["X-RateLimit-Limit"]
    assert response.headers["X-RateLimit-Remaining"]
    assert response.headers["X-RateLimit-Reset"]
    assert response.headers["X-RateLimiter-Instance"] in {
        "api-1",
        "api-2",
        "api-3",
    }


def test_health_reaches_redis_through_nginx():
    response = httpx.get(f"{BASE_URL}/health", timeout=TIMEOUT)

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["redis_connected"] is True
    assert response.json()["instance"] in {"api-1", "api-2", "api-3"}


def test_admin_config_round_trip():
    client_key = unique_client("config")
    save_config(
        client_key,
        mode="sliding_window",
        max_requests=7,
        window_seconds=30,
    )

    response = httpx.get(
        f"{BASE_URL}/admin/config/{client_key}", timeout=TIMEOUT
    )

    assert response.status_code == 200
    assert response.json() == {
        "client_key": client_key,
        "mode": "sliding_window",
        "requests_per_second": 0.001,
        "burst_size": 3.0,
        "max_requests": 7,
        "window_seconds": 30.0,
        "source": "custom",
    }


def test_token_bucket_enforces_burst_and_headers():
    client_key = unique_client("token")
    save_config(client_key, burst_size=3)

    responses = [check(client_key) for _ in range(4)]

    assert [response.status_code for response in responses] == [200, 200, 200, 429]
    assert [response.json()["decision"] for response in responses] == [
        "ALLOW",
        "ALLOW",
        "ALLOW",
        "DENY",
    ]
    for response in responses:
        assert_rate_limit_headers(response)


def test_sliding_window_enforces_request_count_and_headers():
    client_key = unique_client("window")
    save_config(
        client_key,
        mode="sliding_window",
        max_requests=2,
        window_seconds=60,
    )

    responses = [check(client_key) for _ in range(3)]

    assert [response.status_code for response in responses] == [200, 200, 429]
    assert all(response.json()["mode"] == "sliding_window" for response in responses)
    for response in responses:
        assert_rate_limit_headers(response)


def test_replicas_share_one_bucket_in_redis():
    client_key = unique_client("shared")
    save_config(client_key, burst_size=3)

    responses = [
        check(client_key, replica_url)
        for replica_url in REPLICA_URLS
    ]
    denied = check(client_key, REPLICA_URLS[0])

    assert [response.status_code for response in responses] == [200, 200, 200]
    assert denied.status_code == 429
    assert {response.json()["instance"] for response in responses} == {
        "api-1",
        "api-2",
        "api-3",
    }


def test_metrics_match_allow_and_deny_decisions():
    client_key = unique_client("metrics")
    save_config(client_key, burst_size=1)

    allowed = check(client_key)
    denied = check(client_key)
    metrics = httpx.get(
        f"{BASE_URL}/admin/metrics/{client_key}", timeout=TIMEOUT
    )

    assert allowed.status_code == 200
    assert denied.status_code == 429
    assert metrics.status_code == 200
    data = metrics.json()
    assert data["client_key"] == client_key
    assert data["total"] == 2
    assert data["allowed"] == 1
    assert data["denied"] == 1
    assert data["denial_rate"] == 0.5
    assert sum(data["instances"].values()) == 2


def test_dashboard_is_served_through_nginx():
    response = httpx.get(f"{BASE_URL}/dashboard", timeout=TIMEOUT)

    assert response.status_code == 200
    assert "Rate Limiter Control Plane" in response.text
    assert "Traffic distribution" in response.text
