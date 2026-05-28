"""CLI entry point: vantage serve | vantage list"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from dotenv import load_dotenv

load_dotenv()


def cmd_serve(args: argparse.Namespace) -> None:
    try:
        import uvicorn
    except ImportError:
        print("uvicorn not installed. Run: uv sync --extra server", file=sys.stderr)
        sys.exit(1)

    host = args.host or os.getenv("VANTAGE_HOST", "0.0.0.0")  # nosec B104 — intentional, user-configurable
    port = args.port or int(os.getenv("VANTAGE_PORT", "8000"))
    print(f"Vantage → http://{host}:{port}")
    uvicorn.run(
        "vantage.server.app:app",
        host=host,
        port=port,
        reload=args.reload,
    )


def cmd_list(args: argparse.Namespace) -> None:
    from vantage.aws import AWSError, CredentialsError, list_instances

    async def _run():
        try:
            return await list_instances(args.region)
        except CredentialsError as e:
            print(f"Error: AWS credentials not configured — {e}", file=sys.stderr)
            sys.exit(1)
        except AWSError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    records = asyncio.run(_run())

    if args.json:
        print(json.dumps([r.__dict__ for r in records], indent=2))
        return

    if not records:
        print(f"No instances found in {args.region}")
        return

    try:
        from rich.console import Console
        from rich.table import Table

        console = Console()
        table = Table(title=f"EC2 Instances — {args.region}", show_lines=True)
        table.add_column("Name", style="bold")
        table.add_column("Instance ID", style="dim")
        table.add_column("State")
        table.add_column("Type")
        table.add_column("AZ")
        table.add_column("Launch Time")
        table.add_column("First Started")
        table.add_column("Username")

        state_styles = {"running": "green", "stopped": "yellow", "terminated": "red"}

        for r in records:
            state_style = state_styles.get(r.state, "white")
            table.add_row(
                r.name,
                r.instance_id,
                f"[{state_style}]{r.state}[/{state_style}]",
                r.instance_type,
                r.availability_zone,
                r.launch_time,
                r.first_started or "[dim]N/A[/dim]",
                r.username or "[dim]N/A[/dim]",
            )
        console.print(table)
    except ImportError:
        # Fallback plain text
        header = f"{'Name':<30} {'ID':<22} {'State':<12} {'Type':<14} {'AZ':<16} {'Launch Time':<28} {'First Started':<28} Username"
        print(header)
        print("-" * len(header))
        for r in records:
            print(
                f"{r.name:<30} {r.instance_id:<22} {r.state:<12} {r.instance_type:<14} "
                f"{r.availability_zone:<16} {r.launch_time:<28} "
                f"{(r.first_started or 'N/A'):<28} {r.username or 'N/A'}"
            )


def main() -> None:
    parser = argparse.ArgumentParser(prog="vantage", description="AWS EC2 instance viewer")
    sub = parser.add_subparsers(dest="command", required=True)

    serve_p = sub.add_parser("serve", help="Start the web server")
    serve_p.add_argument("--host", default=None)
    serve_p.add_argument("--port", type=int, default=None)
    serve_p.add_argument("--reload", action="store_true")
    serve_p.set_defaults(func=cmd_serve)

    list_p = sub.add_parser("list", help="Print instances table to terminal")
    list_p.add_argument("--region", required=True, help="AWS region (e.g. us-east-1)")
    list_p.add_argument("--json", action="store_true", help="Output raw JSON")
    list_p.set_defaults(func=cmd_list)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
