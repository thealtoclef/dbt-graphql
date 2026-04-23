from __future__ import annotations

import time
from typing import Any

from ariadne.types import ContextValue, Extension

# Module-level lazy singletons — instruments must not be created per-request.
_graphql_meter = None
_op_counter = None
_err_counter = None
_duration_histogram = None


def _get_graphql_instruments():
    global _graphql_meter, _op_counter, _err_counter, _duration_histogram
    if _graphql_meter is None:
        from opentelemetry import metrics

        _graphql_meter = metrics.get_meter("dbt_graphql.graphql")
        _op_counter = _graphql_meter.create_counter(
            name="graphql.operation.count",
            description="Total number of GraphQL operations",
            unit="1",
        )
        _err_counter = _graphql_meter.create_counter(
            name="graphql.operation.errors",
            description="Total number of GraphQL operation errors",
            unit="1",
        )
        _duration_histogram = _graphql_meter.create_histogram(
            name="graphql.operation.duration",
            description="GraphQL operation duration in milliseconds",
            unit="ms",
        )
    return _op_counter, _err_counter, _duration_histogram


class GraphQLMetricsExtension(Extension):
    """Ariadne extension that records GraphQL operation metrics."""

    def __init__(self) -> None:
        self._start_time: float | None = None
        self._operation_name: str = "unknown"
        self._operation_type: str = "query"
        self._op_counter, self._err_counter, self._duration_histogram = (
            _get_graphql_instruments()
        )

    def request_started(self, context: ContextValue) -> None:
        self._start_time = time.perf_counter()
        try:
            query = context["query"]  # type: ignore[index]
            if query and hasattr(query, "definitions"):
                for definition in query.definitions:
                    if hasattr(definition, "name") and definition.name:
                        self._operation_name = definition.name.value
                    if hasattr(definition, "operation"):
                        self._operation_type = str(definition.operation).lower()
        except (KeyError, TypeError):
            pass

    def request_finished(self, context: ContextValue) -> None:
        if self._start_time is None:
            return

        duration_ms = (time.perf_counter() - self._start_time) * 1000
        attributes = {
            "operation.name": self._operation_name,
            "operation.type": self._operation_type,
        }
        self._duration_histogram.record(duration_ms, attributes)
        self._op_counter.add(1, attributes)

        try:
            errors = context["errors"]  # type: ignore[index]
            if errors:
                self._err_counter.add(
                    len(errors), {"operation.name": self._operation_name}
                )
        except (KeyError, TypeError):
            pass

    def format(self, context: ContextValue) -> dict[str, Any] | None:
        return None


def instrument_sqlalchemy(engine) -> None:
    """Attach SQLAlchemy OTel instrumentation to an engine."""
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine)


def instrument_starlette(app) -> None:
    """Attach Starlette OTel instrumentation to the app."""
    from opentelemetry.instrumentation.starlette import StarletteInstrumentor

    StarletteInstrumentor().instrument_app(app)


def build_graphql_http_handler():
    """Return GraphQLHTTPHandler with OpenTelemetryExtension and GraphQLMetricsExtension."""
    from ariadne.asgi.handlers import GraphQLHTTPHandler
    from ariadne.contrib.tracing.opentelemetry import OpenTelemetryExtension

    return GraphQLHTTPHandler(
        extensions=[OpenTelemetryExtension, GraphQLMetricsExtension]
    )
