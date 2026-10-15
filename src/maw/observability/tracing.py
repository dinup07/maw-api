"""OpenTelemetry tracing. The tracer is a no-op until ``setup_tracing`` installs a provider."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode

tracer = trace.get_tracer("maw")


def setup_tracing(service_name: str) -> None:
    """Export spans over OTLP/HTTP, configured by the standard ``OTEL_EXPORTER_OTLP_*`` env vars."""
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)


def _attr(value: Any) -> Any:
    return value if isinstance(value, (str, bool, int, float)) else str(value)


def _is_control_flow(exc: BaseException) -> bool:
    from langgraph.errors import GraphBubbleUp

    return isinstance(exc, (GraphBubbleUp, asyncio.CancelledError))


@contextmanager
def span(name: str, **attrs: Any) -> Iterator[Span]:
    attributes = {k: _attr(v) for k, v in attrs.items() if v is not None}
    with tracer.start_as_current_span(
        name, attributes=attributes, record_exception=False, set_status_on_exception=False
    ) as current:
        try:
            yield current
        except BaseException as e:
            if not _is_control_flow(e):
                current.set_status(Status(StatusCode.ERROR, str(e)))
                current.record_exception(e)
            raise
