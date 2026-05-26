"""API endpoint tests using FastAPI TestClient."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from sparkle.aws import AWSError, CredentialsError, InstanceEvent, InstanceRecord
from sparkle.server.app import app

client = TestClient(app)

_sample_record = InstanceRecord(
    instance_id="i-0abc123def456789a",
    name="web-server-01",
    state="running",
    instance_type="t3.medium",
    availability_zone="us-east-1a",
    launch_time="2026-05-01T12:00:00+00:00",
    first_started="2025-11-10T09:15:00+00:00",
    username="alice",
)


def test_get_instances_success():
    with patch("sparkle.server.app.list_instances", new=AsyncMock(return_value=[_sample_record])):
        r = client.get("/api/instances?region=us-east-1")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1
    assert data[0]["name"] == "web-server-01"
    assert data[0]["username"] == "alice"
    assert data[0]["first_started"] == "2025-11-10T09:15:00+00:00"


def test_get_instances_missing_region():
    r = client.get("/api/instances")
    assert r.status_code == 422


def test_get_instances_credentials_error():
    with patch("sparkle.server.app.list_instances", new=AsyncMock(side_effect=CredentialsError("no creds"))):
        r = client.get("/api/instances?region=us-east-1")
    assert r.status_code == 503


def test_get_instances_aws_error():
    with patch("sparkle.server.app.list_instances", new=AsyncMock(side_effect=AWSError("invalid region"))):
        r = client.get("/api/instances?region=bad-region")
    assert r.status_code == 502


def test_get_regions_success():
    with patch("sparkle.server.app.list_regions", return_value=["us-east-1", "eu-west-1"]):
        r = client.get("/api/regions")
    assert r.status_code == 200
    assert "us-east-1" in r.json()


def test_get_config():
    r = client.get("/api/config")
    assert r.status_code == 200
    data = r.json()
    assert "default_region" in data
    assert "env_creds_configured" in data


def test_cred_source_manual():
    """source=manual with key headers → credentials forwarded."""
    captured = {}

    async def mock_list(region, creds=None):
        captured["creds"] = creds
        return [_sample_record]

    with patch("sparkle.server.app.list_instances", new=mock_list):
        r = client.get(
            "/api/instances?region=us-east-1",
            headers={
                "x-aws-cred-source": "manual",
                "x-aws-access-key-id": "AKIATEST",
                "x-aws-secret-access-key": "secretkey",
                "x-aws-session-token": "tokenabc",
            },
        )
    assert r.status_code == 200
    assert captured["creds"].access_key_id == "AKIATEST"
    assert captured["creds"].secret_access_key == "secretkey"
    assert captured["creds"].session_token == "tokenabc"


def test_cred_source_local():
    """source=local → creds=None (boto3 default chain)."""
    captured = {}

    async def mock_list(region, creds=None):
        captured["creds"] = creds
        return []

    with patch("sparkle.server.app.list_instances", new=mock_list):
        r = client.get(
            "/api/instances?region=us-east-1",
            headers={"x-aws-cred-source": "local"},
        )
    assert r.status_code == 200
    assert captured["creds"] is None


def test_cred_source_env_configured(monkeypatch):
    """source=env reads from os.environ when configured."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ENVKEY")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "ENVSECRET")
    captured = {}

    async def mock_list(region, creds=None):
        captured["creds"] = creds
        return []

    with patch("sparkle.server.app.list_instances", new=mock_list):
        r = client.get(
            "/api/instances?region=us-east-1",
            headers={"x-aws-cred-source": "env"},
        )
    assert r.status_code == 200
    assert captured["creds"].access_key_id == "ENVKEY"
    assert captured["creds"].secret_access_key == "ENVSECRET"


def test_cred_source_env_not_configured():
    """source=env with no env vars → 503."""
    import os
    # Ensure the vars are absent
    env_backup = {k: os.environ.pop(k, None) for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")}
    try:
        with patch("sparkle.server.app.list_instances", new=AsyncMock(return_value=[])):
            r = client.get(
                "/api/instances?region=us-east-1",
                headers={"x-aws-cred-source": "env"},
            )
        assert r.status_code == 503
    finally:
        for k, v in env_backup.items():
            if v is not None:
                os.environ[k] = v


def test_no_cred_source_header_defaults_to_local():
    """No x-aws-cred-source header → treated as local (creds=None)."""
    captured = {}

    async def mock_list(region, creds=None):
        captured["creds"] = creds
        return []

    with patch("sparkle.server.app.list_instances", new=mock_list):
        r = client.get("/api/instances?region=us-east-1")
    assert r.status_code == 200
    assert captured["creds"] is None


_sample_event = InstanceEvent(
    event_time="2026-05-01T12:00:00+00:00",
    event_name="RunInstances",
    instance_id="i-0abc123def456789a",
    username="alice",
    source_ip="10.0.0.1",
)


def test_get_events_success():
    with patch("sparkle.server.app.list_events", new=AsyncMock(return_value=[_sample_event])):
        r = client.get("/api/events?region=us-east-1")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1
    assert data[0]["event_name"] == "RunInstances"
    assert data[0]["instance_id"] == "i-0abc123def456789a"
    assert data[0]["username"] == "alice"
    assert data[0]["source_ip"] == "10.0.0.1"


def test_get_events_missing_region():
    r = client.get("/api/events")
    assert r.status_code == 422


def test_get_events_credentials_error():
    with patch("sparkle.server.app.list_events", new=AsyncMock(side_effect=CredentialsError("no creds"))):
        r = client.get("/api/events?region=us-east-1")
    assert r.status_code == 503


def test_get_events_aws_error():
    with patch("sparkle.server.app.list_events", new=AsyncMock(side_effect=AWSError("bad region"))):
        r = client.get("/api/events?region=bad-region")
    assert r.status_code == 502
