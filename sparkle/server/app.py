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

from sparkle.aws import AWSError, Credentials, CredentialsError, InstanceRecord, list_instances, list_regions

load_dotenv()

FRONTEND_BUILD = Path(__file__).parent.parent.parent / "frontend" / "build"

app = FastAPI(title="Sparkle", version="0.1.0", docs_url="/api/docs")

app.add_middleware(GZipMiddleware, minimum_size=1024)

if os.getenv("SPARKLE_DEV"):
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
    x_aws_cred_source: str | None = Header(default=None),
    x_aws_access_key_id: str | None = Header(default=None),
    x_aws_secret_access_key: str | None = Header(default=None),
    x_aws_session_token: str | None = Header(default=None),
):
    try:
        creds = _resolve_creds(x_aws_cred_source, x_aws_access_key_id, x_aws_secret_access_key, x_aws_session_token)
        return list_regions(creds)
    except CredentialsError as e:
        raise HTTPException(status_code=503, detail=f"AWS credentials not configured: {e}")
    except AWSError as e:
        raise HTTPException(status_code=502, detail=f"AWS error: {e}")


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
        return {"message": "Sparkle API running. Frontend not built yet — run: cd frontend && pnpm build"}
