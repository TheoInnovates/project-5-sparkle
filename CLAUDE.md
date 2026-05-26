# project-5-sparkle

AWS EC2 instance viewer: launch times, first-start history via CloudTrail, and IAM ownership.

## Stack

- **Backend**: Python 3.11, FastAPI, uvicorn, boto3
- **Frontend**: SvelteKit 2.x + Svelte 5, TailwindCSS v4, TypeScript, pnpm
- **Deps**: `uv` — never use plain `pip install`

## Dev setup

```bash
uv sync --extra server
cp .env.example .env   # add AWS creds
uv run sparkle serve --reload

# Frontend (separate terminal)
cd frontend && pnpm install && pnpm dev
```

## Build

```bash
cd frontend && pnpm build
uv run sparkle serve
```

## Commands

- `uv run sparkle serve [--host H] [--port P] [--reload]`
- `uv run sparkle list --region REGION [--json]`
- `uv run pytest`
- `cd frontend && pnpm check`
