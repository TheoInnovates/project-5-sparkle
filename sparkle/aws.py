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


# ── Cost inventory price tables ───────────────────────────────────────────────

_HOURS_PER_MONTH = 730

_EC2_HOURLY: dict[str, float] = {
    "t3.nano": 0.0052, "t3.micro": 0.0104, "t3.small": 0.0208,
    "t3.medium": 0.0416, "t3.large": 0.0832, "t3.xlarge": 0.1664, "t3.2xlarge": 0.3328,
    "t3a.nano": 0.0047, "t3a.micro": 0.0094, "t3a.small": 0.0188,
    "t3a.medium": 0.0376, "t3a.large": 0.0752, "t3a.xlarge": 0.1504,
    "t4g.nano": 0.0042, "t4g.micro": 0.0084, "t4g.small": 0.0168,
    "t4g.medium": 0.0336, "t4g.large": 0.0672, "t4g.xlarge": 0.1344,
    "m5.large": 0.096, "m5.xlarge": 0.192, "m5.2xlarge": 0.384, "m5.4xlarge": 0.768,
    "m6i.large": 0.096, "m6i.xlarge": 0.192, "m6i.2xlarge": 0.384,
    "m6g.large": 0.077, "m6g.xlarge": 0.154, "m6g.2xlarge": 0.308,
    "c5.large": 0.085, "c5.xlarge": 0.17, "c5.2xlarge": 0.34, "c5.4xlarge": 0.68,
    "c6i.large": 0.085, "c6i.xlarge": 0.17, "c6i.2xlarge": 0.34,
    "c6g.large": 0.068, "c6g.xlarge": 0.136, "c6g.2xlarge": 0.272,
    "r5.large": 0.126, "r5.xlarge": 0.252, "r5.2xlarge": 0.504, "r5.4xlarge": 1.008,
    "r6i.large": 0.126, "r6i.xlarge": 0.252, "r6i.2xlarge": 0.504,
    "g4dn.xlarge": 0.526, "g4dn.2xlarge": 0.752, "g4dn.4xlarge": 1.204,
    "p3.2xlarge": 3.06, "p3.8xlarge": 12.24,
}

_RDS_HOURLY: dict[str, float] = {
    "db.t3.micro": 0.017, "db.t3.small": 0.034, "db.t3.medium": 0.068,
    "db.t3.large": 0.136, "db.t3.xlarge": 0.272, "db.t3.2xlarge": 0.544,
    "db.t4g.micro": 0.016, "db.t4g.small": 0.032, "db.t4g.medium": 0.065,
    "db.t4g.large": 0.13, "db.t4g.xlarge": 0.26,
    "db.m5.large": 0.19, "db.m5.xlarge": 0.38, "db.m5.2xlarge": 0.76,
    "db.m5.4xlarge": 1.52, "db.m6g.large": 0.171, "db.m6g.xlarge": 0.342,
    "db.r5.large": 0.24, "db.r5.xlarge": 0.48, "db.r5.2xlarge": 0.96,
    "db.r5.4xlarge": 1.92, "db.r6g.large": 0.216, "db.r6g.xlarge": 0.432,
    "db.r6i.large": 0.24, "db.r6i.xlarge": 0.48, "db.r6i.2xlarge": 0.96,
}

_ELASTICACHE_HOURLY: dict[str, float] = {
    "cache.t3.micro": 0.017, "cache.t3.small": 0.034, "cache.t3.medium": 0.068,
    "cache.t4g.micro": 0.016, "cache.t4g.small": 0.032, "cache.t4g.medium": 0.065,
    "cache.m5.large": 0.156, "cache.m5.xlarge": 0.312, "cache.m5.2xlarge": 0.624,
    "cache.m6g.large": 0.145, "cache.m6g.xlarge": 0.29,
    "cache.r5.large": 0.166, "cache.r5.xlarge": 0.332,
    "cache.r6g.large": 0.149, "cache.r6g.xlarge": 0.298,
}

_EBS_GB_MONTH: dict[str, float] = {
    "gp3": 0.08, "gp2": 0.10, "io1": 0.125, "io2": 0.125,
    "st1": 0.045, "sc1": 0.018, "standard": 0.05,
}

_NAT_GW_HOURLY = 0.045
_ELB_HOURLY = 0.008
_EIP_HOURLY = 0.005
_SNAPSHOT_GB_MONTH = 0.05
_RDS_STORAGE_GB_MONTH = 0.115


def _tags_to_list(raw: list | None) -> list[dict]:
    if not raw:
        return []
    return [{"Key": t.get("Key", ""), "Value": t.get("Value", "")} for t in raw]


def _iso_str(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _fetch_ec2_cost_items(session: boto3.Session, region: str) -> list[dict]:
    ec2 = session.client("ec2", region_name=region, config=_BOTO_CONFIG)
    results: list[dict] = []
    try:
        paginator = ec2.get_paginator("describe_instances")
        for page in paginator.paginate():
            for reservation in page.get("Reservations", []):
                for inst in reservation.get("Instances", []):
                    state = inst.get("State", {}).get("Name", "")
                    if state in ("terminated", "shutting-down"):
                        continue
                    itype = inst.get("InstanceType", "")
                    hourly = _EC2_HOURLY.get(itype, 0.0)
                    cost = hourly * _HOURS_PER_MONTH
                    name = _name_from_tags(inst.get("Tags")) or inst["InstanceId"]
                    az = inst.get("Placement", {}).get("AvailabilityZone", "")
                    launch = inst.get("LaunchTime")
                    results.append({
                        "resource_type": "ec2_instance",
                        "resource_id": inst["InstanceId"],
                        "name": name,
                        "arn": f"arn:aws:ec2:{region}::instance/{inst['InstanceId']}",
                        "state": state,
                        "region": region,
                        "created_at": _iso_str(launch),
                        "size_hint": f"{itype} / {az}",
                        "estimated_monthly_usd": round(cost, 2),
                        "tags": _tags_to_list(inst.get("Tags")),
                    })
    except Exception:  # nosec B110
        pass
    return results


def _fetch_ebs_cost_items(session: boto3.Session, region: str) -> list[dict]:
    ec2 = session.client("ec2", region_name=region, config=_BOTO_CONFIG)
    results: list[dict] = []
    try:
        paginator = ec2.get_paginator("describe_volumes")
        for page in paginator.paginate():
            for v in page.get("Volumes", []):
                if v.get("State") == "deleted":
                    continue
                size_gb = v.get("Size", 0)
                vtype = v.get("VolumeType", "gp2")
                cost = _EBS_GB_MONTH.get(vtype, 0.08) * size_gb
                name = _name_from_tags(v.get("Tags")) or v["VolumeId"]
                attached = [a["InstanceId"] for a in v.get("Attachments", [])]
                state = v.get("State", "")
                results.append({
                    "resource_type": "ebs_volume",
                    "resource_id": v["VolumeId"],
                    "name": name,
                    "arn": f"arn:aws:ec2:{region}::volume/{v['VolumeId']}",
                    "state": state,
                    "region": region,
                    "created_at": _iso_str(v.get("CreateTime")),
                    "size_hint": f"{size_gb} GB / {vtype}" + ("" if attached else " / unattached"),
                    "estimated_monthly_usd": round(cost, 2),
                    "tags": _tags_to_list(v.get("Tags")),
                })
    except Exception:  # nosec B110
        pass
    return results


def _fetch_rds_resources(session: boto3.Session, region: str) -> list[dict]:
    rds = session.client("rds", region_name=region, config=_BOTO_CONFIG)
    results: list[dict] = []
    _skip = {"creating", "deleting", "deleted", "failed", "incompatible-parameters"}

    try:
        paginator = rds.get_paginator("describe_db_instances")
        for page in paginator.paginate():
            for db in page.get("DBInstances", []):
                state = db.get("DBInstanceStatus", "")
                if state in _skip:
                    continue
                cls = db.get("DBInstanceClass", "")
                storage_gb = db.get("AllocatedStorage", 0)
                multi_az = db.get("MultiAZ", False)
                hourly = _RDS_HOURLY.get(cls, 0.10) * (2 if multi_az else 1)
                cost = (hourly * _HOURS_PER_MONTH) + (storage_gb * _RDS_STORAGE_GB_MONTH)
                engine = db.get("Engine", "")
                results.append({
                    "resource_type": "rds_instance",
                    "resource_id": db["DBInstanceIdentifier"],
                    "name": db["DBInstanceIdentifier"],
                    "arn": db.get("DBInstanceArn"),
                    "state": state,
                    "region": region,
                    "created_at": _iso_str(db.get("InstanceCreateTime")),
                    "size_hint": f"{cls} / {storage_gb} GB{' / Multi-AZ' if multi_az else ''} / {engine}",
                    "estimated_monthly_usd": round(cost, 2),
                    "tags": _tags_to_list(db.get("TagList")),
                })
    except Exception:  # nosec B110
        pass

    try:
        paginator = rds.get_paginator("describe_db_clusters")
        for page in paginator.paginate():
            for cluster in page.get("DBClusters", []):
                state = cluster.get("Status", "")
                if state in _skip:
                    continue
                members = len(cluster.get("DBClusterMembers", []))
                hourly = _RDS_HOURLY.get("db.r5.large", 0.24) * max(members, 1)
                cost = hourly * _HOURS_PER_MONTH
                engine = cluster.get("Engine", "aurora")
                results.append({
                    "resource_type": "aurora_cluster",
                    "resource_id": cluster["DBClusterIdentifier"],
                    "name": cluster["DBClusterIdentifier"],
                    "arn": cluster.get("DBClusterArn"),
                    "state": state,
                    "region": region,
                    "created_at": _iso_str(cluster.get("ClusterCreateTime")),
                    "size_hint": f"{members} node{'s' if members != 1 else ''} / {engine}",
                    "estimated_monthly_usd": round(cost, 2),
                    "tags": _tags_to_list(cluster.get("TagList")),
                })
    except Exception:  # nosec B110
        pass

    return results


def _fetch_nat_gateways(session: boto3.Session, region: str) -> list[dict]:
    ec2 = session.client("ec2", region_name=region, config=_BOTO_CONFIG)
    results: list[dict] = []
    try:
        paginator = ec2.get_paginator("describe_nat_gateways")
        for page in paginator.paginate(Filter=[{"Name": "state", "Values": ["available"]}]):
            for gw in page.get("NatGateways", []):
                name = _name_from_tags(gw.get("Tags")) or gw["NatGatewayId"]
                results.append({
                    "resource_type": "nat_gateway",
                    "resource_id": gw["NatGatewayId"],
                    "name": name,
                    "arn": None,
                    "state": gw.get("State", "available"),
                    "region": region,
                    "created_at": _iso_str(gw.get("CreateTime")),
                    "size_hint": gw.get("SubnetId", ""),
                    "estimated_monthly_usd": round(_NAT_GW_HOURLY * _HOURS_PER_MONTH, 2),
                    "tags": _tags_to_list(gw.get("Tags")),
                })
    except Exception:  # nosec B110
        pass
    return results


def _fetch_load_balancers(session: boto3.Session, region: str) -> list[dict]:
    elbv2 = session.client("elbv2", region_name=region, config=_BOTO_CONFIG)
    results: list[dict] = []
    try:
        paginator = elbv2.get_paginator("describe_load_balancers")
        for page in paginator.paginate():
            for lb in page.get("LoadBalancers", []):
                state_code = lb.get("State", {}).get("Code", "")
                if state_code not in ("active", "provisioning"):
                    continue
                lb_type = lb.get("Type", "application").upper()
                results.append({
                    "resource_type": "load_balancer",
                    "resource_id": lb["LoadBalancerName"],
                    "name": lb["LoadBalancerName"],
                    "arn": lb.get("LoadBalancerArn"),
                    "state": state_code,
                    "region": region,
                    "created_at": _iso_str(lb.get("CreatedTime")),
                    "size_hint": lb_type,
                    "estimated_monthly_usd": round(_ELB_HOURLY * _HOURS_PER_MONTH, 2),
                    "tags": [],
                })
    except Exception:  # nosec B110
        pass
    return results


def _fetch_elastic_ips(session: boto3.Session, region: str) -> list[dict]:
    ec2 = session.client("ec2", region_name=region, config=_BOTO_CONFIG)
    results: list[dict] = []
    try:
        for addr in ec2.describe_addresses().get("Addresses", []):
            if addr.get("NetworkInterfaceId") or addr.get("AssociationId"):
                continue  # attached — not charged
            name = _name_from_tags(addr.get("Tags")) or addr.get("PublicIp", "")
            results.append({
                "resource_type": "elastic_ip",
                "resource_id": addr.get("AllocationId") or addr.get("PublicIp", ""),
                "name": name,
                "arn": None,
                "state": "unattached",
                "region": region,
                "created_at": None,
                "size_hint": addr.get("PublicIp", ""),
                "estimated_monthly_usd": round(_EIP_HOURLY * _HOURS_PER_MONTH, 2),
                "tags": _tags_to_list(addr.get("Tags")),
            })
    except Exception:  # nosec B110
        pass
    return results


def _fetch_snapshots(session: boto3.Session, region: str) -> list[dict]:
    ec2 = session.client("ec2", region_name=region, config=_BOTO_CONFIG)
    results: list[dict] = []
    try:
        paginator = ec2.get_paginator("describe_snapshots")
        for page in paginator.paginate(OwnerIds=["self"]):
            for snap in page.get("Snapshots", []):
                if snap.get("State") not in ("completed", "pending"):
                    continue
                size_gb = snap.get("VolumeSize", 0)
                raw_name = _name_from_tags(snap.get("Tags")) or snap.get("Description") or snap["SnapshotId"]
                name = raw_name[:57] + "…" if len(raw_name) > 60 else raw_name
                results.append({
                    "resource_type": "ebs_snapshot",
                    "resource_id": snap["SnapshotId"],
                    "name": name,
                    "arn": None,
                    "state": snap.get("State", "completed"),
                    "region": region,
                    "created_at": _iso_str(snap.get("StartTime")),
                    "size_hint": f"{size_gb} GB",
                    "estimated_monthly_usd": round(size_gb * _SNAPSHOT_GB_MONTH, 2),
                    "tags": _tags_to_list(snap.get("Tags")),
                })
    except Exception:  # nosec B110
        pass
    return results


def _fetch_elasticache_resources(session: boto3.Session, region: str) -> list[dict]:
    ec = session.client("elasticache", region_name=region, config=_BOTO_CONFIG)
    results: list[dict] = []
    try:
        paginator = ec.get_paginator("describe_cache_clusters")
        for page in paginator.paginate():
            for cluster in page.get("CacheClusters", []):
                state = cluster.get("CacheClusterStatus", "")
                if state not in ("available", "modifying"):
                    continue
                node_type = cluster.get("CacheNodeType", "")
                num_nodes = cluster.get("NumCacheNodes", 1)
                hourly = _ELASTICACHE_HOURLY.get(node_type, 0.05) * num_nodes
                engine = cluster.get("Engine", "")
                cluster_id = cluster.get("CacheClusterId", "")
                results.append({
                    "resource_type": "elasticache",
                    "resource_id": cluster_id,
                    "name": cluster_id,
                    "arn": cluster.get("ARN"),
                    "state": state,
                    "region": region,
                    "created_at": _iso_str(cluster.get("CacheClusterCreateTime")),
                    "size_hint": f"{node_type} × {num_nodes} / {engine}",
                    "estimated_monthly_usd": round(hourly * _HOURS_PER_MONTH, 2),
                    "tags": [],
                })
    except Exception:  # nosec B110
        pass
    return results


async def list_cost_resources(region: str, creds: Credentials | None) -> list[dict]:
    """Return all billable resources in a region with cost estimates, sorted by cost descending.

    Each service fetcher catches its own exceptions so one missing IAM permission
    does not prevent other services from returning results.
    """
    session = _make_session(creds)
    loop = asyncio.get_event_loop()

    tasks = [
        loop.run_in_executor(_executor, fn, session, region)
        for fn in (
            _fetch_ec2_cost_items,
            _fetch_ebs_cost_items,
            _fetch_rds_resources,
            _fetch_nat_gateways,
            _fetch_load_balancers,
            _fetch_elastic_ips,
            _fetch_snapshots,
            _fetch_elasticache_resources,
        )
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)
    resources: list[dict] = []
    for r in results:
        if not isinstance(r, Exception):
            resources.extend(r)
    resources.sort(key=lambda r: r["estimated_monthly_usd"], reverse=True)
    return resources


class CredentialsError(Exception):
    pass


class AWSError(Exception):
    pass
