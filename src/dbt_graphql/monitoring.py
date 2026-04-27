from __future__ import annotations

import contextlib
import logging
import sys
import time

from loguru import logger

from .config import MonitoringConfig

_LOG_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | {name}:{line} | "
    "trace_id={extra[otelTraceID]} span_id={extra[otelSpanID]} | {message}"
)


class _InterceptHandler(logging.Handler):
    """Route stdlib logging (Ariadne, SQLAlchemy, etc.) through loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = logging.currentframe(), 2
        while frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back  # type: ignore[assignment]
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


def _setup_loguru(level: str) -> None:
    logger.remove()
    logger.add(sys.stderr, level=level, format=_LOG_FORMAT)
    logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)


def _instrument_loguru() -> None:
    """Inject OTel trace context into every loguru record (loguru docs recipe)."""
    from opentelemetry.trace import (
        INVALID_SPAN,
        INVALID_SPAN_CONTEXT,
        get_current_span,
        get_tracer_provider,
    )

    provider = get_tracer_provider()
    service_name: str | None = None

    def _patch(record: dict) -> None:
        nonlocal service_name
        record["extra"].update(otelTraceID="0", otelSpanID="0", otelTraceSampled=False)

        if service_name is None:
            resource = getattr(provider, "resource", None)
            service_name = (
                (resource.attributes.get("service.name") or "") if resource else ""
            )
        record["extra"]["otelServiceName"] = service_name

        span = get_current_span()
        if span != INVALID_SPAN:
            ctx = span.get_span_context()
            if ctx != INVALID_SPAN_CONTEXT:
                record["extra"]["otelTraceID"] = format(ctx.trace_id, "032x")
                record["extra"]["otelSpanID"] = format(ctx.span_id, "016x")
                record["extra"]["otelTraceSampled"] = ctx.trace_flags.sampled

    logger.configure(patcher=_patch)


def _add_otlp_log_sink(config: MonitoringConfig, resource) -> None:
    from opentelemetry._logs import SeverityNumber, set_logger_provider
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

    if config.logs.protocol == "http":
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    else:
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter

    log_provider = LoggerProvider(resource=resource)
    log_provider.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter(endpoint=config.logs.endpoint))
    )
    set_logger_provider(log_provider)
    otel_logger = log_provider.get_logger("dbt_graphql")

    severity_map = {
        "TRACE": SeverityNumber.TRACE,
        "DEBUG": SeverityNumber.DEBUG,
        "INFO": SeverityNumber.INFO,
        "SUCCESS": SeverityNumber.INFO2,
        "WARNING": SeverityNumber.WARN,
        "ERROR": SeverityNumber.ERROR,
        "CRITICAL": SeverityNumber.FATAL,
    }

    def _otlp_sink(message) -> None:
        from opentelemetry.trace import (
            NonRecordingSpan,
            SpanContext,
            TraceFlags,
            set_span_in_context,
        )

        r = message.record
        trace_id_str = r["extra"].get("otelTraceID", "0") or "0"
        span_id_str = r["extra"].get("otelSpanID", "0") or "0"
        sampled = r["extra"].get("otelTraceSampled", False)

        ctx = None
        if trace_id_str != "0" and span_id_str != "0":
            span_context = SpanContext(
                trace_id=int(trace_id_str, 16),
                span_id=int(span_id_str, 16),
                is_remote=False,
                trace_flags=TraceFlags(TraceFlags.SAMPLED if sampled else 0),
            )
            ctx = set_span_in_context(NonRecordingSpan(span_context))

        attributes = {
            "code.filepath": str(r["file"].path),
            "code.lineno": r["line"],
            "code.function": r["function"],
            "logger.name": r["name"],
        }

        exc = r.get("exception")
        if exc and exc[0] is not None:
            import traceback

            attributes["exception.type"] = exc[0].__name__
            attributes["exception.message"] = str(exc[1]) if exc[1] else ""
            if exc[2]:
                attributes["exception.stacktrace"] = "".join(
                    traceback.format_exception(exc[0], exc[1], exc[2])
                )

        try:
            otel_logger.emit(
                timestamp=int(r["time"].timestamp() * 1e9),
                observed_timestamp=int(r["time"].timestamp() * 1e9),
                context=ctx,
                severity_number=severity_map.get(r["level"].name, SeverityNumber.INFO),
                severity_text=r["level"].name,
                body=r["message"],
                attributes=attributes,
            )
        except Exception:
            pass

    logger.add(_otlp_sink, level=config.logs.level.upper())


def configure_monitoring(config: MonitoringConfig | None = None) -> None:
    """Bootstrap OTel SDK and configure loguru from a MonitoringConfig."""
    if config is None:
        config = MonitoringConfig()

    level = config.logs.level.upper()

    # 1. Loguru: stderr sink + stdlib intercept
    _setup_loguru(level)

    # 2. OTel shared resource
    from opentelemetry.sdk.resources import SERVICE_NAME, Resource

    resource = Resource({SERVICE_NAME: config.service_name})

    # 3. Traces
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    tracer_provider = TracerProvider(resource=resource)

    if level == "DEBUG":
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter

        tracer_provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

    if config.traces.endpoint:
        if config.traces.protocol == "http":
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
        else:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,
            )
        tracer_provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=config.traces.endpoint))
        )

    trace.set_tracer_provider(tracer_provider)

    # 4. Metrics (only when endpoint configured)
    if config.metrics.endpoint:
        from opentelemetry import metrics
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

        if config.metrics.protocol == "http":
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
                OTLPMetricExporter,
            )
        else:
            from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
                OTLPMetricExporter,
            )

        reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=config.metrics.endpoint)
        )
        metrics.set_meter_provider(
            MeterProvider(resource=resource, metric_readers=[reader])
        )

    # 5. Loguru patcher: inject trace context into every log record
    _instrument_loguru()

    # 6. OTLP log sink (only when endpoint configured; stderr sink always runs)
    if config.logs.endpoint:
        _add_otlp_log_sink(config, resource)

    logger.info(
        "monitoring configured | service={} traces={} metrics={} logs={} level={}",
        config.service_name,
        config.traces.endpoint or "off",
        config.metrics.endpoint or "off",
        config.logs.endpoint or "off",
        level,
    )


@contextlib.asynccontextmanager
async def timed(histogram, counter, base_attributes: dict):
    """Async context manager that records duration and call count on exit.

    Sets status=success on normal exit, status=error if an exception escapes.
    """
    start = time.perf_counter()
    attrs = {**base_attributes, "status": "success"}
    try:
        yield
    except Exception:
        attrs["status"] = "error"
        raise
    finally:
        histogram.record((time.perf_counter() - start) * 1000, attrs)
        counter.add(1, attrs)
