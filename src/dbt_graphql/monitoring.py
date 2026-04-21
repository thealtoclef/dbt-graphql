from __future__ import annotations


def configure_monitoring(
    service_name: str = "dbt-graphql",
    exporter: str = "otlp",
    endpoint: str | None = None,
    log_level: str = "INFO",
) -> None:
    """Bootstrap the OTel SDK from config.yml values (monitoring section).

    No-op if opentelemetry-sdk is not installed or the [api] extra is absent.
    """
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import SERVICE_NAME, Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        return

    import logging

    level = getattr(logging, log_level.upper(), logging.INFO)
    logging.getLogger("dbt_graphql").setLevel(level)

    resource = Resource({SERVICE_NAME: service_name})
    provider = TracerProvider(resource=resource)

    if exporter == "console":
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter

        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    else:
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,
            )

            exporter_instance = (
                OTLPSpanExporter(endpoint=endpoint) if endpoint else OTLPSpanExporter()
            )
            provider.add_span_processor(BatchSpanProcessor(exporter_instance))
        except ImportError:
            return

    trace.set_tracer_provider(provider)

    try:
        from opentelemetry.instrumentation.logging import LoggingInstrumentor

        LoggingInstrumentor().instrument()
    except ImportError:
        pass
