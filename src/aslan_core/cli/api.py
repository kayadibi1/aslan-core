from __future__ import annotations

import os

import click


@click.group()
def api() -> None:
    """API server operations."""


@api.command("serve")
@click.option("--host", default="127.0.0.1", help="Bind address.")
@click.option("--port", default=8600, type=int, help="Port.")
def serve(host: str, port: int) -> None:
    """Start the Financial API server."""
    secret = os.environ.get("ASLAN_JWT_SECRET", "")
    if len(secret) < 32:
        raise click.UsageError("ASLAN_JWT_SECRET environment variable must be set (>= 32 chars)")
    import uvicorn

    from aslan_core.api import create_api_app

    app = create_api_app()
    uvicorn.run(app, host=host, port=port)
