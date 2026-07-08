"""Unit tests for the dashboard metrics response shape."""

from app.main import format_metrics


def test_format_metrics_calculates_denial_rate_and_instances():
    result = format_metrics(
        "checkout",
        {
            "mode": "token_bucket",
            "total": "20",
            "allowed": "15",
            "denied": "5",
            "last_seen": "123.5",
            "instance:api-1": "7",
            "instance:api-2": "13",
        },
    )

    assert result == {
        "client_key": "checkout",
        "mode": "token_bucket",
        "total": 20,
        "allowed": 15,
        "denied": 5,
        "denial_rate": 0.25,
        "last_seen": 123.5,
        "instances": {"api-1": 7, "api-2": 13},
    }


def test_format_metrics_handles_client_without_traffic():
    result = format_metrics("new-client", {})

    assert result["total"] == 0
    assert result["denial_rate"] == 0.0
    assert result["instances"] == {}
