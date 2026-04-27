from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from aslan_core.config import postgres_dsn_from_env


def _is_pgbouncer_dsn(dsn: str) -> bool:
    """Heuristic per spec §11: port 6432 means PgBouncer (transaction pooling)."""
    return urlparse(dsn).port == 6432


def _connect_args_for(dsn: str) -> dict[str, Any]:
    """asyncpg connect_args tailored to the DSN.

    PgBouncer in transaction-pooling mode is incompatible with asyncpg's
    server-side prepared-statement cache; disable it for bouncer endpoints.
    """
    if _is_pgbouncer_dsn(dsn):
        return {"statement_cache_size": 0}
    return {}


def create_engine(dsn: str | None = None, **kw: Any) -> AsyncEngine:
    """Build the async SQLAlchemy engine.

    Auto-applies ``statement_cache_size=0`` to asyncpg connect args when the
    DSN points at a PgBouncer endpoint (port 6432). Direct-Postgres
    connections keep the default cache. Caller-supplied ``connect_args``
    override the auto-detected defaults.
    """
    actual = dsn or postgres_dsn_from_env()
    caller_connect_args: dict[str, Any] = kw.pop("connect_args", {})
    connect_args: dict[str, Any] = {**_connect_args_for(actual), **caller_connect_args}
    return create_async_engine(actual, connect_args=connect_args, **kw)
