from __future__ import annotations


def instrument_sqlalchemy(engine) -> None:
    """Attach SQLAlchemy OTel instrumentation to an engine. No-op if not installed."""
    try:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine)
    except ImportError:
        pass


def instrument_starlette(app) -> None:
    """Attach Starlette OTel instrumentation to the app. No-op if not installed."""
    try:
        from opentelemetry.instrumentation.starlette import StarletteInstrumentor

        StarletteInstrumentor().instrument_app(app)
    except ImportError:
        pass


def build_graphql_http_handler():
    """Return GraphQLHTTPHandler with OpenTelemetryExtension if available, else None."""
    try:
        from ariadne.asgi.handlers import GraphQLHTTPHandler
        from ariadne.contrib.tracing.opentelemetry import OpenTelemetryExtension

        return GraphQLHTTPHandler(extensions=[OpenTelemetryExtension])
    except ImportError:
        return None
