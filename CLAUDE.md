# project-5-sparkle (Vantage)

AWS resource viewer: EC2 instances, EBS volumes, cost inventory, CloudTrail events, and multi-region support.

## Stack

- **Backend**: Python 3.11, FastAPI, uvicorn, boto3
- **Frontend**: SvelteKit 2.x + Svelte 5, TailwindCSS v4, TypeScript, pnpm
- **Deps**: `uv` — never use plain `pip install`

## Dev setup

```bash
uv sync --extra server
cp .env.example .env   # add AWS creds
VANTAGE_DEV=1 uv run vantage serve --reload

# Frontend (separate terminal)
cd frontend && pnpm install && pnpm dev
```

## Build

```bash
cd frontend && pnpm build
uv run vantage serve
```

## Commands

- `uv run vantage serve [--host H] [--port P] [--reload]`
- `uv run vantage list --region REGION [--json]`
- `uv run pytest`
- `cd frontend && pnpm check`
