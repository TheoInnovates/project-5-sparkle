"""boto3 EC2 + CloudTrail queries with in-memory TTL cache."""
from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone

import boto3
import botocore.config
import botocore.exceptions

_BOTO_CONFIG = botocore.config.Config(
    connect_timeout=5,
    read_timeout=30,
    retries={"max_attempts": 1},
)

CACHE_TTL = 300  # seconds
# Cache key: (region, access_key_id_or_empty)
_cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}
_events_cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}
_executor = ThreadPoolExecutor(max_workers=20)

# CloudTrail rate limit: 2 req/s per account region
_ct_semaphore = asyncio.Semaphore(2)


@dataclass
class Credentials:
    access_key_id: str
    secret_access_key: str
    session_token: str | None = None


@dataclass
class InstanceEvent:
    event_time: str
    event_name: str
    instance_id: str
    username: str | None
    source_ip: str | None


@dataclass
class InstanceRecord:
    instance_id: str
    name: str
    state: str
    instance_type: str
    availability_zone: str
    launch_time: str
    first_started: str | None
    username: str | None


def _make_session(creds: Credentials | None) -> boto3.Session:
    if creds:
        return boto3.Session(
            aws_access_key_id=creds.access_key_id,
            aws_secret_access_key=creds.secret_access_key,
            aws_session_token=creds.session_token,
        )
    return boto3.Session()


def _get_ec2_instances(region: str, creds: Credentials | None) -> list[dict]:
    session = _make_session(creds)
    ec2 = session.client("ec2", region_name=region, config=_BOTO_CONFIG)
    paginator = ec2.get_paginator("describe_instances")
    instances = []
    for page in paginator.paginate():
        for reservation in page["Reservations"]:
            instances.extend(reservation["Instances"])
    return instances


def _lookup_first_run(
    region: str, instance_id: str, creds: Credentials | None
) -> tuple[str | None, str | None]:
    """Return (first_started_iso, username) from CloudTrail, or (None, None)."""
    session = _make_session(creds)
    ct = session.client("cloudtrail", region_name=region, config=_BOTO_CONFIG)
    run_events: list[dict] = []
    kwargs: dict = {
        "LookupAttributes": [{"AttributeKey": "ResourceName", "AttributeValue": instance_id}],
        "MaxResults": 50,
    }
    retries = 3
    delay = 1.0
    while True:
        try:
            resp = ct.lookup_events(**kwargs)
        except botocore.exceptions.ClientError as e:
            code = e.response["Error"]["Code"]
            if code in ("ThrottlingException", "RateExceededException") and retries > 0:
                time.sleep(delay)
                delay *= 2
                retries -= 1
                continue
            return None, None
        for event in resp.get("Events", []):
            if event.get("EventName") == "RunInstances":
                run_events.append(event)
        next_token = resp.get("NextToken")
        if not next_token:
            break
        kwargs["NextToken"] = next_token

    if not run_events:
        return None, None

    oldest = min(run_events, key=lambda e: e["EventTime"])
    first_started = oldest["EventTime"]
    if isinstance(first_started, datetime):
        if first_started.tzinfo is None:
            first_started = first_started.replace(tzinfo=timezone.utc)
        first_started_iso = first_started.isoformat()
    else:
        first_started_iso = str(first_started)

    # Parse username from CloudTrail userIdentity (nested in raw CloudTrail JSON)
    username: str | None = None
    import json as _json
    raw = oldest.get("CloudTrailEvent")
    if raw:
        try:
            ct_event = _json.loads(raw)
            identity = ct_event.get("userIdentity", {})
            username = (
                identity.get("userName")
                or identity.get("arn")
                or identity.get("type")
            )
        except Exception:  # nosec B110 — malformed CloudTrail JSON leaves username as None
            pass

    return first_started_iso, username


async def _fetch_cloudtrail(
    region: str, instance_id: str, creds: Credentials | None
) -> tuple[str | None, str | None]:
    loop = asyncio.get_event_loop()
    async with _ct_semaphore:
        return await loop.run_in_executor(
            _executor, _lookup_first_run, region, instance_id, creds
        )


def _name_from_tags(tags: list[dict] | None) -> str | None:
    if not tags:
        return None
    for tag in tags:
        if tag.get("Key") == "Name":
            return tag.get("Value") or None
    return None


async def list_instances(region: str, creds: Credentials | None = None) -> list[InstanceRecord]:
    cache_key = (region, creds.access_key_id if creds else "")
    now = time.monotonic()
    if cache_key in _cache:
        fetched_at, cached = _cache[cache_key]
        if now - fetched_at < CACHE_TTL:
            return [InstanceRecord(**r) for r in cached]

    loop = asyncio.get_event_loop()
    try:
        raw_instances = await loop.run_in_executor(
            _executor, _get_ec2_instances, region, creds
        )
    except botocore.exceptions.NoCredentialsError as e:
        raise CredentialsError(str(e)) from e
    except botocore.exceptions.ClientError as e:
        raise AWSError(str(e)) from e

    tasks = [_fetch_cloudtrail(region, inst["InstanceId"], creds) for inst in raw_instances]
    ct_results = await asyncio.gather(*tasks, return_exceptions=True)

    records: list[InstanceRecord] = []
    for inst, ct in zip(raw_instances, ct_results):
        iid = inst["InstanceId"]
        launch_time = inst.get("LaunchTime")
        if isinstance(launch_time, datetime):
            if launch_time.tzinfo is None:
                launch_time = launch_time.replace(tzinfo=timezone.utc)
            launch_time_iso = launch_time.isoformat()
        else:
            launch_time_iso = str(launch_time) if launch_time else ""

        if isinstance(ct, Exception):
            first_started, username = None, None
        else:
            first_started, username = ct

        records.append(
            InstanceRecord(
                instance_id=iid,
                name=_name_from_tags(inst.get("Tags")) or iid,
                state=inst.get("State", {}).get("Name", "unknown"),
                instance_type=inst.get("InstanceType", ""),
                availability_zone=inst.get("Placement", {}).get("AvailabilityZone", ""),
                launch_time=launch_time_iso,
                first_started=first_started,
                username=username,
            )
        )

    records.sort(key=lambda r: r.name.lower())
    _cache[cache_key] = (now, [r.__dict__ for r in records])
    return records


def _parse_instance_events(raw_events: list[dict]) -> list[InstanceEvent]:
    import json as _json

    result: list[InstanceEvent] = []
    for event in raw_events:
        raw_json = event.get("CloudTrailEvent", "{}")
        try:
            ct = _json.loads(raw_json)
        except Exception:  # nosec B110
            continue

        event_name = ct.get("eventName", event.get("EventName", ""))

        event_time_raw = ct.get("eventTime") or event.get("EventTime")
        if isinstance(event_time_raw, datetime):
            if event_time_raw.tzinfo is None:
                event_time_raw = event_time_raw.replace(tzinfo=timezone.utc)
            event_time_str = event_time_raw.isoformat()
        else:
            event_time_str = str(event_time_raw) if event_time_raw else ""

        identity = ct.get("userIdentity", {})
        username: str | None = (
            identity.get("userName") or identity.get("arn") or identity.get("type")
        )
        source_ip: str | None = ct.get("sourceIPAddress")

        # RunInstances: IDs are in responseElements (instance didn't exist at request time)
        # Start/Stop/Terminate: IDs are in requestParameters
        if event_name == "RunInstances":
            items = ((ct.get("responseElements") or {})
                     .get("instancesSet", {})
                     .get("items", []))
        else:
            items = ((ct.get("requestParameters") or {})
                     .get("instancesSet", {})
                     .get("items", []))

        for item in items:
            iid = item.get("instanceId")
            if iid:
                result.append(InstanceEvent(
                    event_time=event_time_str,
                    event_name=event_name,
                    instance_id=iid,
                    username=username,
                    source_ip=source_ip,
                ))

    return result


async def list_events(region: str, creds: Credentials | None = None) -> list[InstanceEvent]:
    cache_key = (region, creds.access_key_id if creds else "")
    now = time.monotonic()
    if cache_key in _events_cache:
        fetched_at, cached = _events_cache[cache_key]
        if now - fetched_at < CACHE_TTL:
            return [InstanceEvent(**e) for e in cached]

    session = _make_session(creds)
    loop = asyncio.get_event_loop()

    def _fetch_by_event_name(event_name: str) -> list[dict]:
        ct_client = session.client("cloudtrail", region_name=region, config=_BOTO_CONFIG)
        all_events: list[dict] = []
        kwargs: dict = {
            "LookupAttributes": [{"AttributeKey": "EventName", "AttributeValue": event_name}],
            "MaxResults": 50,
        }
        retries, delay = 3, 1.0
        while True:
            try:
                resp = ct_client.lookup_events(**kwargs)
            except botocore.exceptions.ClientError as e:
                code = e.response["Error"]["Code"]
                if code in ("ThrottlingException", "RateExceededException") and retries > 0:
                    time.sleep(delay)
                    delay *= 2
                    retries -= 1
                    continue
                raise
            all_events.extend(resp.get("Events", []))
            next_token = resp.get("NextToken")
            if not next_token:
                break
            kwargs["NextToken"] = next_token
        return all_events

    all_raw: list[dict] = []
    try:
        # Sequential to stay within CloudTrail rate limits
        for event_name in ("RunInstances", "StartInstances", "StopInstances", "TerminateInstances"):
            raw = await loop.run_in_executor(_executor, _fetch_by_event_name, event_name)
            all_raw.extend(raw)
    except botocore.exceptions.NoCredentialsError as e:
        raise CredentialsError(str(e)) from e
    except botocore.exceptions.ClientError as e:
        raise AWSError(str(e)) from e

    events = _parse_instance_events(all_raw)
    events.sort(key=lambda e: e.event_time)
    _events_cache[cache_key] = (now, [e.__dict__ for e in events])
    return events


def list_regions(creds: Credentials | None = None) -> list[str]:
    session = _make_session(creds)
    ec2 = session.client("ec2", region_name="us-east-1", config=_BOTO_CONFIG)
    resp = ec2.describe_regions(
        Filters=[{"Name": "opt-in-status", "Values": ["opt-in-not-required", "opted-in"]}]
    )
    return sorted(r["RegionName"] for r in resp["Regions"])


class CredentialsError(Exception):
    pass


class AWSError(Exception):
    pass
