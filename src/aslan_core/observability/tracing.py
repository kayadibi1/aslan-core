"""OpenTelemetry tracing skeleton + ``@traced`` decorator.

Public API:

* :func:`setup_tracing` — configure the global ``TracerProvider`` and
  wire OTLP-gRPC export + SQLAlchemy / asyncpg auto-instrumentation.
  No-op when ``endpoint`` is ``None``.
* :func:`traced` — decorator factory wrapping an async method in an
  OTel span named ``{ClassName}.{method_name}``. Adds ``actor.id`` and
  ``actor.kind`` attributes from :func:`aslan_core.audit.current_actor`
  at call time.

Lazy-import contract:

The OpenTelemetry packages live under the ``aslan-core[obs]`` extra.
A consumer that does not install the extra must still be able to
``from aslan_core.observability import setup_tracing, traced`` and
either no-op or run the wrapped method without the SDK present.

  * ``setup_tracing(None)`` returns immediately — no imports.
  * ``setup_tracing("...")`` lazily imports the SDK + exporters +
    instrumentations and raises ``ImportError`` only on this call.
  * ``@traced(...)`` wraps a method and returns the wrapper *without*
    importing OpenTelemetry. The first call to the wrapper attempts
    a lazy import of ``opentelemetry.trace``; if absent, the wrapper
    falls through to the underlying coroutine unchanged so consumers
    without the extra still run.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from typing import Any, ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")


def setup_tracing(
    endpoint: str | None = None,
    *,
    service_name: str = "aslan-core",
    resource_attributes: dict[str, str] | None = None,
) -> None:
    """Configure the global OTel TracerProvider with OTLP-gRPC export
    and SQLAlchemy + asyncpg auto-instrumentation.

    No-op when ``endpoint`` is ``None``. Lazy-imports OpenTelemetry —
    consumers without the ``aslan-core[obs]`` extra can call this with
    ``None`` without an ``ImportError``.
    """
    if endpoint is None:
        return
    # Lazy imports — only required when an endpoint is configured.
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    attrs: dict[str, str] = {"service.name": service_name}
    if resource_attributes:
        attrs.update(resource_attributes)
    provider = TracerProvider(resource=Resource.create(attrs))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)

    # Auto-instrumentation — captures every SQL query as a span with
    # bind-param-redacted SQL text. Idempotent ``instrument()`` calls
    # are tolerated by the OTel runtime so calling setup_tracing twice
    # does not double-wrap the engine.
    SQLAlchemyInstrumentor().instrument()
    AsyncPGInstrumentor().instrument()


def traced(
    operation_name: str | None = None,
) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Awaitable[T]]]:
    """Decorator factory wrapping an async method in an OTel span.

    Span name defaults to ``{ClassName}.{method_name}``; pass
    ``operation_name`` to override. The wrapper sets ``actor.id`` and
    ``actor.kind`` attributes from :func:`aslan_core.audit.current_actor`
    at call time.

    Lazy-import contract: the decorator does NOT import
    ``opentelemetry`` at decoration time. The first invocation tries
    to obtain the OTel tracer; if that import fails (no ``[obs]``
    extra installed), the wrapper falls through to the underlying
    coroutine unchanged. Consumers without the extra still run.
    """

    def deco(fn: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
        @functools.wraps(fn)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            tracer = _get_tracer_or_none()
            if tracer is None:
                # No OTel installed — run the coroutine without
                # instrumentation so base-install consumers still work.
                return await fn(*args, **kwargs)
            cls_name = type(args[0]).__name__ if args else ""
            name = operation_name or (f"{cls_name}.{fn.__name__}" if cls_name else fn.__name__)
            with tracer.start_as_current_span(name) as span:
                # Lazy import inside the call to avoid a circular
                # ``tracing → audit → tracing`` chain at module load.
                from aslan_core.audit import current_actor

                a = current_actor()
                if a is not None:
                    span.set_attribute("actor.id", a.actor_id)
                    span.set_attribute("actor.kind", a.actor_kind)
                return await fn(*args, **kwargs)

        return wrapper

    return deco


def _get_tracer_or_none() -> Any:
    """Return ``opentelemetry.trace.get_tracer("aslan_core")`` if the
    SDK is importable; otherwise ``None``.

    Cached on the module via ``_tracer`` so the import lookup happens
    at most once per process.
    """
    global _tracer_cached
    if _tracer_cached is not _SENTINEL:
        return _tracer_cached
    try:
        from opentelemetry import trace as _trace
    except ImportError:
        _tracer_cached = None
        return None
    _tracer_cached = _trace.get_tracer("aslan_core")
    return _tracer_cached


_SENTINEL: object = object()
_tracer_cached: Any = _SENTINEL
