"""FastAPI application — REST API + SvelteKit static file serving."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from vantage.aws import (
    AWSError, Credentials, CredentialsError,
    InstanceEvent, InstanceRecord,
    fetch_ec2_pricing, fetch_s3_events, list_cost_resources, list_events, list_instances,
    list_regions, list_volumes, search_resources_by_tag,
    reboot_instance, set_instance_tags, start_instance, stop_instance, terminate_instance,
)

load_dotenv()

FRONTEND_BUILD = Path(__file__).parent.parent.parent / "frontend" / "build"

app = FastAPI(title="Vantage", version="0.1.0", docs_url="/api/docs")

app.add_middleware(GZipMiddleware, minimum_size=1024)

if os.getenv("VANTAGE_DEV"):
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173"],
        allow_methods=["GET"],
        allow_headers=["*"],
        expose_headers=["*"],
    )


def _resolve_creds(
    source: str | None,
    access_key_id: str | None,
    secret_access_key: str | None,
    session_token: str | None,
) -> Credentials | None:
    """Resolve the credential source sent by the frontend into a Credentials object (or None)."""
    src = (source or "local").lower()

    if src == "local":
        return None  # boto3 default chain: env vars → ~/.aws/credentials → IAM role

    if src == "env":
        key = os.getenv("AWS_ACCESS_KEY_ID")
        secret = os.getenv("AWS_SECRET_ACCESS_KEY")
        if not key or not secret:
            raise CredentialsError(
                "AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY not set in the server .env file"
            )
        return Credentials(key, secret, os.getenv("AWS_SESSION_TOKEN") or None)

    if src == "manual":
        if not access_key_id or not secret_access_key:
            raise CredentialsError("Manual source selected but Access Key ID / Secret not provided")
        return Credentials(access_key_id, secret_access_key, session_token or None)

    return None  # unknown source → fall back to default chain


class InstanceResponse(BaseModel):
    instance_id: str
    name: str
    state: str
    instance_type: str
    availability_zone: str
    launch_time: str
    first_started: str | None
    username: str | None
    private_ip: str | None = None
    public_ip: str | None = None
    vpc_id: str | None = None
    subnet_id: str | None = None
    security_groups: list | None = None
    image_id: str | None = None
    key_name: str | None = None
    iam_profile: str | None = None
    architecture: str | None = None
    tags: list | None = None

    @classmethod
    def from_record(cls, r: InstanceRecord) -> "InstanceResponse":
        return cls(
            instance_id=r.instance_id,
            name=r.name,
            state=r.state,
            instance_type=r.instance_type,
            availability_zone=r.availability_zone,
            launch_time=r.launch_time,
            first_started=r.first_started,
            username=r.username,
            private_ip=r.private_ip,
            public_ip=r.public_ip,
            vpc_id=r.vpc_id,
            subnet_id=r.subnet_id,
            security_groups=r.security_groups,
            image_id=r.image_id,
            key_name=r.key_name,
            iam_profile=r.iam_profile,
            architecture=r.architecture,
            tags=r.tags,
        )


@app.get("/api/instances", response_model=list[InstanceResponse])
async def get_instances(
    region: str = Query(..., description="AWS region, e.g. us-east-1"),
    x_aws_cred_source: str | None = Header(default=None),
    x_aws_access_key_id: str | None = Header(default=None),
    x_aws_secret_access_key: str | None = Header(default=None),
    x_aws_session_token: str | None = Header(default=None),
):
    try:
        creds = _resolve_creds(x_aws_cred_source, x_aws_access_key_id, x_aws_secret_access_key, x_aws_session_token)
        records = await list_instances(region, creds)
    except CredentialsError as e:
        raise HTTPException(status_code=503, detail=f"AWS credentials not configured: {e}")
    except AWSError as e:
        raise HTTPException(status_code=502, detail=f"AWS error: {e}")
    return [InstanceResponse.from_record(r) for r in records]


@app.get("/api/regions", response_model=list[str])
def get_regions(
    region: str | None = Query(default=None, description="Hint region for partition detection (e.g. us-gov-east-1)"),
    x_aws_cred_source: str | None = Header(default=None),
    x_aws_access_key_id: str | None = Header(default=None),
    x_aws_secret_access_key: str | None = Header(default=None),
    x_aws_session_token: str | None = Header(default=None),
):
    try:
        creds = _resolve_creds(x_aws_cred_source, x_aws_access_key_id, x_aws_secret_access_key, x_aws_session_token)
        hint = region or "us-east-1"
        return list_regions(creds, hint_region=hint)
    except CredentialsError as e:
        raise HTTPException(status_code=503, detail=f"AWS credentials not configured: {e}")
    except AWSError as e:
        raise HTTPException(status_code=502, detail=f"AWS error: {e}")


class EventResponse(BaseModel):
    event_time: str
    event_name: str
    instance_id: str
    username: str | None
    source_ip: str | None

    @classmethod
    def from_record(cls, e: InstanceEvent) -> "EventResponse":
        return cls(
            event_time=e.event_time,
            event_name=e.event_name,
            instance_id=e.instance_id,
            username=e.username,
            source_ip=e.source_ip,
        )


@app.get("/api/events", response_model=list[EventResponse])
async def get_events(
    region: str = Query(..., description="AWS region, e.g. us-east-1"),
    x_aws_cred_source: str | None = Header(default=None),
    x_aws_access_key_id: str | None = Header(default=None),
    x_aws_secret_access_key: str | None = Header(default=None),
    x_aws_session_token: str | None = Header(default=None),
):
    try:
        creds = _resolve_creds(x_aws_cred_source, x_aws_access_key_id, x_aws_secret_access_key, x_aws_session_token)
        evts = await list_events(region, creds)
    except CredentialsError as e:
        raise HTTPException(status_code=503, detail=f"AWS credentials not configured: {e}")
    except AWSError as e:
        raise HTTPException(status_code=502, detail=f"AWS error: {e}")
    return [EventResponse.from_record(e) for e in evts]


def _action_headers(
    x_aws_cred_source: str | None,
    x_aws_access_key_id: str | None,
    x_aws_secret_access_key: str | None,
    x_aws_session_token: str | None,
) -> Credentials | None:
    return _resolve_creds(x_aws_cred_source, x_aws_access_key_id, x_aws_secret_access_key, x_aws_session_token)


@app.post("/api/instances/{instance_id}/start")
def do_start(
    instance_id: str,
    region: str = Query(...),
    x_aws_cred_source: str | None = Header(default=None),
    x_aws_access_key_id: str | None = Header(default=None),
    x_aws_secret_access_key: str | None = Header(default=None),
    x_aws_session_token: str | None = Header(default=None),
):
    try:
        creds = _action_headers(x_aws_cred_source, x_aws_access_key_id, x_aws_secret_access_key, x_aws_session_token)
        return start_instance(region, instance_id, creds)
    except CredentialsError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except AWSError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/instances/{instance_id}/stop")
def do_stop(
    instance_id: str,
    region: str = Query(...),
    x_aws_cred_source: str | None = Header(default=None),
    x_aws_access_key_id: str | None = Header(default=None),
    x_aws_secret_access_key: str | None = Header(default=None),
    x_aws_session_token: str | None = Header(default=None),
):
    try:
        creds = _action_headers(x_aws_cred_source, x_aws_access_key_id, x_aws_secret_access_key, x_aws_session_token)
        return stop_instance(region, instance_id, creds)
    except CredentialsError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except AWSError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/instances/{instance_id}/reboot")
def do_reboot(
    instance_id: str,
    region: str = Query(...),
    x_aws_cred_source: str | None = Header(default=None),
    x_aws_access_key_id: str | None = Header(default=None),
    x_aws_secret_access_key: str | None = Header(default=None),
    x_aws_session_token: str | None = Header(default=None),
):
    try:
        creds = _action_headers(x_aws_cred_source, x_aws_access_key_id, x_aws_secret_access_key, x_aws_session_token)
        return reboot_instance(region, instance_id, creds)
    except CredentialsError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except AWSError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/instances/{instance_id}/terminate")
def do_terminate(
    instance_id: str,
    region: str = Query(...),
    x_aws_cred_source: str | None = Header(default=None),
    x_aws_access_key_id: str | None = Header(default=None),
    x_aws_secret_access_key: str | None = Header(default=None),
    x_aws_session_token: str | None = Header(default=None),
):
    try:
        creds = _action_headers(x_aws_cred_source, x_aws_access_key_id, x_aws_secret_access_key, x_aws_session_token)
        return terminate_instance(region, instance_id, creds)
    except CredentialsError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except AWSError as e:
        raise HTTPException(status_code=502, detail=str(e))


class TagsPayload(BaseModel):
    upsert: list[dict] = []
    delete_keys: list[str] = []


@app.put("/api/instances/{instance_id}/tags", status_code=204)
def do_update_tags(
    instance_id: str,
    payload: TagsPayload,
    region: str = Query(...),
    x_aws_cred_source: str | None = Header(default=None),
    x_aws_access_key_id: str | None = Header(default=None),
    x_aws_secret_access_key: str | None = Header(default=None),
    x_aws_session_token: str | None = Header(default=None),
):
    try:
        creds = _action_headers(x_aws_cred_source, x_aws_access_key_id, x_aws_secret_access_key, x_aws_session_token)
        set_instance_tags(region, instance_id, payload.upsert, payload.delete_keys, creds)
    except CredentialsError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except AWSError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/s3-events", response_model=list[EventResponse])
async def get_s3_events(
    bucket: str = Query(..., description="S3 bucket containing CloudTrail logs"),
    region: str = Query(..., description="CloudTrail region (used in the S3 path)"),
    bucket_region: str = Query(default=None, description="S3 bucket region (defaults to region)"),
    prefix: str = Query(default="", description="Path prefix before AWSLogs/ (for org trails or custom layouts)"),
    start_date: str = Query(default=None, description="Start date YYYY-MM-DD (default: 1 year ago)"),
    end_date: str = Query(default=None, description="End date YYYY-MM-DD (default: today)"),
    x_aws_cred_source: str | None = Header(default=None),
    x_aws_access_key_id: str | None = Header(default=None),
    x_aws_secret_access_key: str | None = Header(default=None),
    x_aws_session_token: str | None = Header(default=None),
):
    try:
        creds = _resolve_creds(x_aws_cred_source, x_aws_access_key_id, x_aws_secret_access_key, x_aws_session_token)
        evts = await fetch_s3_events(
            bucket=bucket,
            trail_region=region,
            bucket_region=bucket_region or region,
            prefix=prefix,
            start_date_str=start_date,
            end_date_str=end_date,
            creds=creds,
        )
    except CredentialsError as e:
        raise HTTPException(status_code=503, detail=f"AWS credentials not configured: {e}")
    except AWSError as e:
        raise HTTPException(status_code=502, detail=f"AWS error: {e}")
    return [EventResponse.from_record(e) for e in evts]


@app.get("/api/tag-search")
async def tag_search_endpoint(
    key: str = Query(..., description="Tag key to search for"),
    value: str | None = Query(default=None, description="Tag value (omit to match any value)"),
    region: str = Query(..., description="AWS region"),
    x_aws_cred_source: str | None = Header(default=None),
    x_aws_access_key_id: str | None = Header(default=None),
    x_aws_secret_access_key: str | None = Header(default=None),
    x_aws_session_token: str | None = Header(default=None),
):
    try:
        creds = _resolve_creds(x_aws_cred_source, x_aws_access_key_id, x_aws_secret_access_key, x_aws_session_token)
        return search_resources_by_tag(key, value, region, creds)
    except CredentialsError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except AWSError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/volumes")
async def get_volumes_endpoint(
    region: str = Query(..., description="AWS region"),
    x_aws_cred_source: str | None = Header(default=None),
    x_aws_access_key_id: str | None = Header(default=None),
    x_aws_secret_access_key: str | None = Header(default=None),
    x_aws_session_token: str | None = Header(default=None),
):
    try:
        creds = _resolve_creds(x_aws_cred_source, x_aws_access_key_id, x_aws_secret_access_key, x_aws_session_token)
        return list_volumes(region, creds)
    except CredentialsError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except AWSError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/cost-resources")
async def get_cost_resources(
    region: str = Query(..., description="AWS region"),
    x_aws_cred_source: str | None = Header(default=None),
    x_aws_access_key_id: str | None = Header(default=None),
    x_aws_secret_access_key: str | None = Header(default=None),
    x_aws_session_token: str | None = Header(default=None),
):
    try:
        creds = _resolve_creds(x_aws_cred_source, x_aws_access_key_id, x_aws_secret_access_key, x_aws_session_token)
        return await list_cost_resources(region, creds)
    except CredentialsError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except AWSError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/pricing")
async def get_pricing(
    region: str = Query(..., description="AWS region"),
    x_aws_cred_source: str | None = Header(default=None),
    x_aws_access_key_id: str | None = Header(default=None),
    x_aws_secret_access_key: str | None = Header(default=None),
    x_aws_session_token: str | None = Header(default=None),
):
    creds = _resolve_creds(x_aws_cred_source, x_aws_access_key_id, x_aws_secret_access_key, x_aws_session_token)
    prices = await fetch_ec2_pricing(region, creds)
    return {"region": region, "prices": prices, "count": len(prices)}


@app.get("/api/config")
def get_config():
    return {
        "default_region": os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
        "env_creds_configured": bool(
            os.getenv("AWS_ACCESS_KEY_ID") and os.getenv("AWS_SECRET_ACCESS_KEY")
        ),
    }


# SvelteKit static files
if FRONTEND_BUILD.exists():
    app.mount("/_app", StaticFiles(directory=str(FRONTEND_BUILD / "_app")), name="sveltekit_app")

    @app.get("/favicon.svg")
    def favicon():
        f = FRONTEND_BUILD / "favicon.svg"
        if f.exists():
            return FileResponse(str(f))
        raise HTTPException(status_code=404)

    @app.get("/{full_path:path}")
    def spa_fallback(full_path: str):
        index = FRONTEND_BUILD / "index.html"
        if index.exists():
            return FileResponse(str(index))
        raise HTTPException(status_code=404, detail="Frontend build not found. Run: cd frontend && pnpm build")
else:
    @app.get("/")
    def root():
        return {"message": "Vantage API running. Frontend not built yet — run: cd frontend && pnpm build"}
