"""Unit tests for sparkle.aws using moto mocks."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _reset_cache():
    from sparkle import aws
    aws._cache.clear()
    yield
    aws._cache.clear()


def _make_instance(
    iid: str = "i-0abc123def456789a",
    state: str = "running",
    tags: list[dict] | None = None,
    launch_time: datetime | None = None,
) -> dict:
    return {
        "InstanceId": iid,
        "State": {"Name": state},
        "InstanceType": "t3.medium",
        "Placement": {"AvailabilityZone": "us-east-1a"},
        "LaunchTime": launch_time or datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc),
        "Tags": tags or [],
    }


def _make_ct_event(iid: str, username: str, event_time: datetime) -> dict:
    raw = json.dumps({
        "userIdentity": {"userName": username, "type": "IAMUser"},
        "eventName": "RunInstances",
    })
    return {
        "EventName": "RunInstances",
        "EventTime": event_time,
        "CloudTrailEvent": raw,
        "Resources": [{"ResourceName": iid}],
    }


@pytest.mark.asyncio
async def test_instance_with_name_tag():
    inst = _make_instance(tags=[{"Key": "Name", "Value": "web-server-01"}])
    with (
        patch("sparkle.aws._get_ec2_instances", return_value=[inst]),
        patch("sparkle.aws._lookup_first_run", return_value=(None, None)),
    ):
        from sparkle.aws import list_instances
        records = await list_instances("us-east-1")
    assert len(records) == 1
    assert records[0].name == "web-server-01"


@pytest.mark.asyncio
async def test_instance_without_name_tag_falls_back_to_id():
    inst = _make_instance(iid="i-0deadbeef0000001", tags=[])
    with (
        patch("sparkle.aws._get_ec2_instances", return_value=[inst]),
        patch("sparkle.aws._lookup_first_run", return_value=(None, None)),
    ):
        from sparkle.aws import list_instances
        records = await list_instances("us-east-1")
    assert records[0].name == "i-0deadbeef0000001"


@pytest.mark.asyncio
async def test_cloudtrail_event_found():
    iid = "i-0abc123def456789b"
    inst = _make_instance(iid=iid)
    first_time = datetime(2025, 11, 10, 9, 15, 0, tzinfo=timezone.utc)
    with (
        patch("sparkle.aws._get_ec2_instances", return_value=[inst]),
        patch("sparkle.aws._lookup_first_run", return_value=(first_time.isoformat(), "alice")),
    ):
        from sparkle.aws import list_instances
        records = await list_instances("us-east-1")
    r = records[0]
    assert r.first_started == first_time.isoformat()
    assert r.username == "alice"


@pytest.mark.asyncio
async def test_no_cloudtrail_event_returns_null():
    inst = _make_instance()
    with (
        patch("sparkle.aws._get_ec2_instances", return_value=[inst]),
        patch("sparkle.aws._lookup_first_run", return_value=(None, None)),
    ):
        from sparkle.aws import list_instances
        records = await list_instances("us-east-1")
    assert records[0].first_started is None
    assert records[0].username is None


@pytest.mark.asyncio
async def test_results_cached():
    inst = _make_instance()
    call_count = 0

    def counting_ec2(region, creds=None):
        nonlocal call_count
        call_count += 1
        return [inst]

    with (
        patch("sparkle.aws._get_ec2_instances", side_effect=counting_ec2),
        patch("sparkle.aws._lookup_first_run", return_value=(None, None)),
    ):
        from sparkle.aws import list_instances
        await list_instances("us-east-1")
        await list_instances("us-east-1")
    assert call_count == 1


def test_name_from_tags_none():
    from sparkle.aws import _name_from_tags
    assert _name_from_tags(None) is None
    assert _name_from_tags([]) is None
    assert _name_from_tags([{"Key": "Env", "Value": "prod"}]) is None
    assert _name_from_tags([{"Key": "Name", "Value": "my-box"}]) == "my-box"
    assert _name_from_tags([{"Key": "Name", "Value": ""}]) is None
