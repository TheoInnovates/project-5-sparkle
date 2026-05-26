# Sparkle

AWS EC2 instance viewer — see every instance in an account and region along with who launched it and when, sourced from CloudTrail.

[![CI](https://github.com/TheoInnovates/project-5-sparkle/actions/workflows/ci.yml/badge.svg)](https://github.com/TheoInnovates/project-5-sparkle/actions/workflows/ci.yml)

## What it shows

| Column | Source |
|---|---|
| Name | EC2 `Name` tag (falls back to Instance ID) |
| Instance ID | EC2 |
| State | EC2 (running / stopped / terminated / …) |
| Type | EC2 |
| Availability Zone | EC2 |
| Launch Time | EC2 — most recent start |
| First Started | CloudTrail — oldest `RunInstances` event |
| Username | CloudTrail — IAM user or role ARN from that event |

`First Started` and `Username` show **N/A** for instances older than CloudTrail's 90-day retention window or when the account lacks CloudTrail read permissions.

## Stack

- **Backend** — Python 3.11, FastAPI, uvicorn, boto3
- **Frontend** — SvelteKit 2 + Svelte 5, TailwindCSS v4, TypeScript
- **Deps** — `uv` (backend), `pnpm` (frontend)

## Quick start

### 1. Clone and configure

```bash
git clone https://github.com/TheoInnovates/project-5-sparkle.git
cd project-5-sparkle
cp .env.example .env
```

Edit `.env` — at minimum set a region:

```env
AWS_DEFAULT_REGION=us-east-1
```

AWS credentials are optional in `.env`; the server will fall back to the boto3 default chain (`~/.aws/credentials`, IAM instance role, etc.).

### 2. Install and run

```bash
uv sync --extra server
uv run sparkle serve
```

Open [http://localhost:8000](http://localhost:8000).

The frontend is pre-built. For a live-reload dev setup see [Development](#development).

## AWS credentials and region

Click the lock button in the header to open the credentials panel. It controls both the credential source and the default region.

**Credential source**

| Source | How it works |
|---|---|
| **Local / Profile** | boto3 default chain — `~/.aws/credentials`, env vars, IAM role |
| **.env file** | Uses `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` set in the server's `.env` |
| **Manual entry** | Enter keys directly; stored in `sessionStorage` (cleared on tab close) |

**Default Region**

Type any region code (e.g. `eu-west-1`) into the Default Region field and click Apply. The toolbar region selector updates immediately and the saved value is restored on page refresh, taking priority over `AWS_DEFAULT_REGION` in `.env`. Reset clears it back to the server default.

The region can also be changed at any time from the toolbar dropdown without opening the panel — the panel field is only needed when you want the choice to persist across refreshes.

## CLI

```bash
# Formatted table
uv run sparkle list --region us-east-1

# Raw JSON
uv run sparkle list --region eu-west-1 --json

# Custom host/port
uv run sparkle serve --host 127.0.0.1 --port 9000 --reload
```

## Required IAM permissions

```json
{
  "Effect": "Allow",
  "Action": [
    "ec2:DescribeInstances",
    "ec2:DescribeRegions",
    "cloudtrail:LookupEvents"
  ],
  "Resource": "*"
}
```

## Development

```bash
# Backend (auto-reload on save)
uv sync --extra server
SPARKLE_DEV=1 uv run sparkle serve --reload

# Frontend (separate terminal — Vite proxies /api to :8000)
cd frontend
pnpm install
pnpm dev
```

Frontend dev server runs on [http://localhost:5173](http://localhost:5173).

To rebuild the static assets served by the backend:

```bash
cd frontend && pnpm build
```

## Tests

```bash
# Python (pytest + moto mocks)
uv run pytest

# Frontend type-check
cd frontend && pnpm check
```

## CI

GitHub Actions runs six jobs on every push and pull request:

| Job | Checks |
|---|---|
| Lint | `ruff check` + `ruff format` |
| Test | `pytest` |
| Security (Python) | `bandit` SAST + `pip-audit` CVE scan |
| Frontend | Svelte/TypeScript type-check + build |
| Security (JS) | `pnpm audit` |
| CodeQL | GitHub SAST for Python + JavaScript/TypeScript |

## Configuration reference

| Variable | Default | Description |
|---|---|---|
| `AWS_DEFAULT_REGION` | `us-east-1` | Region pre-selected on page load |
| `AWS_ACCESS_KEY_ID` | — | Optional — enables the `.env file` credential source in the UI |
| `AWS_SECRET_ACCESS_KEY` | — | Required alongside `AWS_ACCESS_KEY_ID` |
| `AWS_SESSION_TOKEN` | — | Optional session token for temporary credentials |
| `SPARKLE_HOST` | `0.0.0.0` | Bind address |
| `SPARKLE_PORT` | `8000` | Bind port |
| `SPARKLE_DEV` | — | Set to `1` to enable CORS (needed for Vite dev server) |
