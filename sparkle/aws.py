"""boto3 EC2 + CloudTrail queries with in-memory TTL cache."""
from __future__ import annotations

import asyncio
import gzip
import io
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import boto3
import botocore.config
import botocore.exceptions

_BOTO_CONFIG = botocore.config.Config(
    connect_timeout=5,
    read_timeout=30,
    retries={"max_attempts": 1},
)

# Longer read timeout for S3 object downloads (CloudTrail log files can be several MB)
_S3_BOTO_CONFIG = botocore.config.Config(
    connect_timeout=5,
    read_timeout=120,
    retries={"max_attempts": 2},
)

CACHE_TTL = 300  # seconds
# Cache key: (region, access_key_id_or_empty)
_cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}
_events_cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}
_executor = ThreadPoolExecutor(max_workers=20)


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
    # Extended fields — populated from describe_instances, all nullable
    private_ip: str | None = None
    public_ip: str | None = None
    vpc_id: str | None = None
    subnet_id: str | None = None
    security_groups: list | None = None  # [{id, name}, ...]
    image_id: str | None = None
    key_name: str | None = None
    iam_profile: str | None = None       # IamInstanceProfile ARN
    architecture: str | None = None
    tags: list | None = None             # [{Key, Value}, ...]


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

    records: list[InstanceRecord] = []
    for inst in raw_instances:
        iid = inst["InstanceId"]
        launch_time = inst.get("LaunchTime")
        if isinstance(launch_time, datetime):
            if launch_time.tzinfo is None:
                launch_time = launch_time.replace(tzinfo=timezone.utc)
            launch_time_iso = launch_time.isoformat()
        else:
            launch_time_iso = str(launch_time) if launch_time else ""

        iam = inst.get("IamInstanceProfile")
        sgs = [
            {"id": sg["GroupId"], "name": sg["GroupName"]}
            for sg in inst.get("SecurityGroups", [])
        ]
        records.append(
            InstanceRecord(
                instance_id=iid,
                name=_name_from_tags(inst.get("Tags")) or iid,
                state=inst.get("State", {}).get("Name", "unknown"),
                instance_type=inst.get("InstanceType", ""),
                availability_zone=inst.get("Placement", {}).get("AvailabilityZone", ""),
                launch_time=launch_time_iso,
                first_started=None,
                username=None,
                private_ip=inst.get("PrivateIpAddress"),
                public_ip=inst.get("PublicIpAddress"),
                vpc_id=inst.get("VpcId"),
                subnet_id=inst.get("SubnetId"),
                security_groups=sgs or None,
                image_id=inst.get("ImageId"),
                key_name=inst.get("KeyName"),
                iam_profile=iam.get("Arn") if iam else None,
                architecture=inst.get("Architecture"),
                tags=inst.get("Tags") or None,
            )
        )

    records.sort(key=lambda r: r.name.lower())
    _cache[cache_key] = (now, [r.__dict__ for r in records])
    return records


def _parse_instance_events(raw_events: list[dict]) -> list[InstanceEvent]:
    result: list[InstanceEvent] = []
    for event in raw_events:
        raw_json = event.get("CloudTrailEvent", "{}")
        try:
            ct = json.loads(raw_json)
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


def list_regions(creds: Credentials | None = None, hint_region: str = "us-east-1") -> list[str]:
    """Return all enabled regions reachable from hint_region.

    Pass a GovCloud hint_region (e.g. us-gov-east-1) when using GovCloud credentials —
    the commercial us-east-1 endpoint rejects GovCloud creds and never lists gov regions.
    """
    session = _make_session(creds)
    ec2 = session.client("ec2", region_name=hint_region, config=_BOTO_CONFIG)
    resp = ec2.describe_regions(
        Filters=[{"Name": "opt-in-status", "Values": ["opt-in-not-required", "opted-in"]}]
    )
    return sorted(r["RegionName"] for r in resp["Regions"])


def _clear_instance_cache(region: str, creds: Credentials | None) -> None:
    key = (region, creds.access_key_id if creds else "")
    _cache.pop(key, None)


def start_instance(region: str, instance_id: str, creds: Credentials | None) -> dict:
    session = _make_session(creds)
    ec2 = session.client("ec2", region_name=region, config=_BOTO_CONFIG)
    resp = ec2.start_instances(InstanceIds=[instance_id])
    _clear_instance_cache(region, creds)
    change = resp["StartingInstances"][0]
    return {"previous_state": change["PreviousState"]["Name"],
            "current_state": change["CurrentState"]["Name"]}


def stop_instance(region: str, instance_id: str, creds: Credentials | None) -> dict:
    session = _make_session(creds)
    ec2 = session.client("ec2", region_name=region, config=_BOTO_CONFIG)
    resp = ec2.stop_instances(InstanceIds=[instance_id])
    _clear_instance_cache(region, creds)
    change = resp["StoppingInstances"][0]
    return {"previous_state": change["PreviousState"]["Name"],
            "current_state": change["CurrentState"]["Name"]}


def terminate_instance(region: str, instance_id: str, creds: Credentials | None) -> dict:
    session = _make_session(creds)
    ec2 = session.client("ec2", region_name=region, config=_BOTO_CONFIG)
    resp = ec2.terminate_instances(InstanceIds=[instance_id])
    _clear_instance_cache(region, creds)
    change = resp["TerminatingInstances"][0]
    return {"previous_state": change["PreviousState"]["Name"],
            "current_state": change["CurrentState"]["Name"]}


def reboot_instance(region: str, instance_id: str, creds: Credentials | None) -> dict:
    session = _make_session(creds)
    ec2 = session.client("ec2", region_name=region, config=_BOTO_CONFIG)
    ec2.reboot_instances(InstanceIds=[instance_id])
    _clear_instance_cache(region, creds)
    return {"message": "ok", "instance_id": instance_id}


def set_instance_tags(
    region: str,
    instance_id: str,
    upsert: list[dict],
    delete_keys: list[str],
    creds: Credentials | None,
) -> None:
    session = _make_session(creds)
    ec2 = session.client("ec2", region_name=region, config=_BOTO_CONFIG)
    if upsert:
        ec2.create_tags(Resources=[instance_id], Tags=upsert)
    if delete_keys:
        ec2.delete_tags(Resources=[instance_id], Tags=[{"Key": k} for k in delete_keys])
    _clear_instance_cache(region, creds)


_WANTED_EVENTS = frozenset({"RunInstances", "StartInstances", "StopInstances", "TerminateInstances"})


def _get_account_id(region: str, creds: Credentials | None) -> str:
    session = _make_session(creds)
    sts = session.client("sts", region_name=region, config=_BOTO_CONFIG)
    return sts.get_caller_identity()["Account"]


def _parse_s3_records(records: list[dict]) -> list[InstanceEvent]:
    """Parse raw CloudTrail records from S3 log files (already-decoded JSON objects)."""
    result: list[InstanceEvent] = []
    for rec in records:
        event_name = rec.get("eventName", "")
        if event_name not in _WANTED_EVENTS:
            continue
        event_time: str = rec.get("eventTime", "")
        identity = rec.get("userIdentity", {})
        username: str | None = (
            identity.get("userName") or identity.get("arn") or identity.get("type")
        )
        source_ip: str | None = rec.get("sourceIPAddress")
        if event_name == "RunInstances":
            items = (rec.get("responseElements") or {}).get("instancesSet", {}).get("items", [])
        else:
            items = (rec.get("requestParameters") or {}).get("instancesSet", {}).get("items", [])
        for item in items:
            iid = item.get("instanceId")
            if iid:
                result.append(InstanceEvent(
                    event_time=event_time,
                    event_name=event_name,
                    instance_id=iid,
                    username=username,
                    source_ip=source_ip,
                ))
    return result


def _list_s3_keys(
    s3_client,
    bucket: str,
    account_id: str,
    trail_region: str,
    prefix: str,
    start: date,
    end: date,
) -> list[str]:
    """Return all CloudTrail .json.gz S3 keys for the given date range."""
    keys: list[str] = []
    current = start
    while current <= end:
        day_prefix = (
            f"{prefix}AWSLogs/{account_id}/CloudTrail/{trail_region}/"
            f"{current.year}/{current.month:02d}/{current.day:02d}/"
        )
        try:
            paginator = s3_client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=bucket, Prefix=day_prefix):
                for obj in page.get("Contents", []):
                    if obj["Key"].endswith(".json.gz"):
                        keys.append(obj["Key"])
        except botocore.exceptions.ClientError:
            pass
        current += timedelta(days=1)
    return keys


def _download_s3_key(s3_client, bucket: str, key: str) -> list[dict]:
    """Download and decompress one CloudTrail log file; return its Records list."""
    try:
        resp = s3_client.get_object(Bucket=bucket, Key=key)
        compressed = resp["Body"].read()
        with gzip.open(io.BytesIO(compressed)) as f:
            data = json.loads(f.read())
        return data.get("Records", [])
    except Exception:  # nosec B110 — corrupt/missing file → skip silently
        return []


async def fetch_s3_events(
    bucket: str,
    trail_region: str,
    bucket_region: str,
    prefix: str = "",
    start_date_str: str | None = None,
    end_date_str: str | None = None,
    creds: Credentials | None = None,
) -> list[InstanceEvent]:
    """Fetch CloudTrail instance events from S3 archive (beyond the 90-day lookup_events limit).

    Args:
        bucket: S3 bucket name containing CloudTrail logs.
        trail_region: AWS region whose CloudTrail logs to query (used in the S3 path).
        bucket_region: AWS region where the S3 bucket lives (may differ from trail_region).
        prefix: Optional path prefix before AWSLogs/ (e.g. for org trails or custom layouts).
        start_date_str: ISO date string YYYY-MM-DD (defaults to 1 year ago).
        end_date_str: ISO date string YYYY-MM-DD (defaults to today).
        creds: AWS credentials.
    """
    loop = asyncio.get_event_loop()

    try:
        account_id = await loop.run_in_executor(_executor, _get_account_id, trail_region, creds)
    except botocore.exceptions.NoCredentialsError as e:
        raise CredentialsError(str(e)) from e
    except botocore.exceptions.ClientError as e:
        raise AWSError(str(e)) from e

    today = date.today()
    start = date.fromisoformat(start_date_str) if start_date_str else today - timedelta(days=365)
    end = date.fromisoformat(end_date_str) if end_date_str else today
    start = max(start, today - timedelta(days=3650))  # cap at 10 years
    end = min(end, today)

    if prefix and not prefix.endswith("/"):
        prefix += "/"

    session = _make_session(creds)
    s3 = session.client("s3", region_name=bucket_region, config=_S3_BOTO_CONFIG)

    try:
        keys = await loop.run_in_executor(
            _executor, _list_s3_keys, s3, bucket, account_id, trail_region, prefix, start, end
        )
    except botocore.exceptions.NoCredentialsError as e:
        raise CredentialsError(str(e)) from e
    except botocore.exceptions.ClientError as e:
        raise AWSError(str(e)) from e

    # Download all files in parallel
    tasks = [
        loop.run_in_executor(_executor, _download_s3_key, s3, bucket, key)
        for key in keys
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_records: list[dict] = []
    for r in results:
        if not isinstance(r, Exception):
            all_records.extend(r)

    events = _parse_s3_records(all_records)
    events.sort(key=lambda e: e.event_time)
    return events


def search_resources_by_tag(
    key: str,
    value: str | None,
    region: str,
    creds: Credentials | None,
) -> list[dict]:
    """Return all tagged resources matching key (and optionally value) via Resource Groups Tagging API."""
    session = _make_session(creds)
    client = session.client("resourcegroupstaggingapi", region_name=region, config=_BOTO_CONFIG)
    tag_filter: dict = {"Key": key}
    if value:
        tag_filter["Values"] = [value]
    results: list[dict] = []
    try:
        paginator = client.get_paginator("get_resources")
        for page in paginator.paginate(TagFilters=[tag_filter]):
            for r in page.get("ResourceTagMappingList", []):
                arn = r["ResourceARN"]
                parts = arn.split(":")
                service = parts[2] if len(parts) > 2 else "unknown"
                resource_part = parts[-1] if parts else arn
                name_tag = next((t["Value"] for t in r.get("Tags", []) if t["Key"] == "Name"), None)
                results.append({
                    "arn": arn,
                    "service": service,
                    "resource": resource_part,
                    "name": name_tag,
                    "tags": r.get("Tags", []),
                })
    except botocore.exceptions.NoCredentialsError as e:
        raise CredentialsError(str(e)) from e
    except botocore.exceptions.ClientError as e:
        raise AWSError(str(e)) from e
    return results


def list_volumes(region: str, creds: Credentials | None) -> list[dict]:
    """Return all EBS volumes in the region."""
    session = _make_session(creds)
    ec2 = session.client("ec2", region_name=region, config=_BOTO_CONFIG)
    vols: list[dict] = []
    try:
        paginator = ec2.get_paginator("describe_volumes")
        for page in paginator.paginate():
            for v in page.get("Volumes", []):
                name_tag = next((t["Value"] for t in v.get("Tags", []) if t["Key"] == "Name"), None)
                vols.append({
                    "volume_id": v["VolumeId"],
                    "state": v["State"],
                    "size_gb": v["Size"],
                    "volume_type": v["VolumeType"],
                    "iops": v.get("Iops"),
                    "throughput": v.get("Throughput"),
                    "create_time": v["CreateTime"].isoformat(),
                    "name": name_tag,
                    "attachments": [
                        {"instance_id": a["InstanceId"], "device": a["Device"]}
                        for a in v.get("Attachments", [])
                    ],
                    "tags": v.get("Tags", []),
                })
    except botocore.exceptions.NoCredentialsError as e:
        raise CredentialsError(str(e)) from e
    except botocore.exceptions.ClientError as e:
        raise AWSError(str(e)) from e
    return vols


class CredentialsError(Exception):
    pass


class AWSError(Exception):
    pass
