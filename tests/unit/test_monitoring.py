"""Unit tests for dbt_graphql.monitoring.configure_monitoring."""

from __future__ import annotations

from unittest.mock import MagicMock, patch


from dbt_graphql.config import LogsConfig, MetricsConfig, MonitoringConfig, TracesConfig


def _make_otel_mocks():
    sdk_resources = MagicMock()
    sdk_resources.SERVICE_NAME = "service.name"
    return {
        "opentelemetry": MagicMock(),
        "opentelemetry.trace": MagicMock(),
        "opentelemetry.sdk": MagicMock(),
        "opentelemetry.sdk.resources": sdk_resources,
        "opentelemetry.sdk.trace": MagicMock(),
        "opentelemetry.sdk.trace.export": MagicMock(),
    }


def _make_otlp_grpc_trace_mocks(mocks=None):
    m = mocks or _make_otel_mocks()
    otlp_mod = MagicMock()
    m.update(
        {
            "opentelemetry.exporter": MagicMock(),
            "opentelemetry.exporter.otlp": MagicMock(),
            "opentelemetry.exporter.otlp.proto": MagicMock(),
            "opentelemetry.exporter.otlp.proto.grpc": MagicMock(),
            "opentelemetry.exporter.otlp.proto.grpc.trace_exporter": otlp_mod,
        }
    )
    return m, otlp_mod


def _make_otlp_grpc_metric_mocks(mocks=None):
    m = mocks or _make_otel_mocks()
    otlp_mod = MagicMock()
    m.update(
        {
            "opentelemetry.sdk.metrics": MagicMock(),
            "opentelemetry.sdk.metrics.export": MagicMock(),
            "opentelemetry.exporter": MagicMock(),
            "opentelemetry.exporter.otlp": MagicMock(),
            "opentelemetry.exporter.otlp.proto": MagicMock(),
            "opentelemetry.exporter.otlp.proto.grpc": MagicMock(),
            "opentelemetry.exporter.otlp.proto.grpc.metric_exporter": otlp_mod,
        }
    )
    return m, otlp_mod


class TestConfigureMonitoring:
    def _run(self, config, extra_mocks=None, *, patch_instrument=True):
        """Run configure_monitoring with OTel mocked and loguru sinks suppressed.

        Patches are applied AFTER reload so they target the freshly-defined functions,
        not the pre-reload versions that reload() replaces.
        """
        mocks = _make_otel_mocks()
        if extra_mocks:
            mocks.update(extra_mocks)

        with patch.dict("sys.modules", mocks):
            from importlib import reload
            import dbt_graphql.monitoring as tel

            reload(tel)

            fn_patches = [patch.object(tel, "_setup_loguru")]
            if patch_instrument:
                fn_patches.append(patch.object(tel, "_instrument_loguru"))

            for p in fn_patches:
                p.start()
            try:
                tel.configure_monitoring(config)
            finally:
                for p in reversed(fn_patches):
                    p.stop()

        return mocks

    def test_console_span_exporter_added_when_debug_level(self):
        mocks = self._run(MonitoringConfig(logs=LogsConfig(level="DEBUG")))
        mocks["opentelemetry.sdk.trace.export"].ConsoleSpanExporter.assert_called_once()

    def test_no_console_span_exporter_at_info_level(self):
        mocks = self._run(MonitoringConfig(logs=LogsConfig(level="INFO")))
        mocks["opentelemetry.sdk.trace.export"].ConsoleSpanExporter.assert_not_called()

    def test_otlp_grpc_span_exporter_when_traces_endpoint_set(self):
        extra, otlp_mod = _make_otlp_grpc_trace_mocks()
        config = MonitoringConfig(
            traces=TracesConfig(endpoint="http://collector:4317", protocol="grpc")
        )
        self._run(config, extra_mocks=extra)
        otlp_mod.OTLPSpanExporter.assert_called_once_with(
            endpoint="http://collector:4317"
        )

    def test_otlp_http_span_exporter_when_protocol_http(self):
        mocks = _make_otel_mocks()
        otlp_mod = MagicMock()
        mocks.update(
            {
                "opentelemetry.exporter": MagicMock(),
                "opentelemetry.exporter.otlp": MagicMock(),
                "opentelemetry.exporter.otlp.proto": MagicMock(),
                "opentelemetry.exporter.otlp.proto.http": MagicMock(),
                "opentelemetry.exporter.otlp.proto.http.trace_exporter": otlp_mod,
            }
        )
        config = MonitoringConfig(
            traces=TracesConfig(endpoint="http://collector:4318", protocol="http")
        )
        self._run(config, extra_mocks=mocks)
        otlp_mod.OTLPSpanExporter.assert_called_once_with(
            endpoint="http://collector:4318"
        )

    def test_no_otlp_span_exporter_when_no_traces_endpoint(self):
        mocks = self._run(MonitoringConfig())
        assert "opentelemetry.exporter.otlp.proto.grpc.trace_exporter" not in mocks

    def test_service_name_passed_to_resource(self):
        mocks = self._run(MonitoringConfig(service_name="my-svc"))
        mocks["opentelemetry.sdk.resources"].Resource.assert_called_once_with(
            {"service.name": "my-svc"}
        )

    def test_metrics_otlp_exporter_when_endpoint_configured(self):
        extra, otlp_mod = _make_otlp_grpc_metric_mocks()
        config = MonitoringConfig(
            metrics=MetricsConfig(endpoint="http://collector:4317", protocol="grpc")
        )
        self._run(config, extra_mocks=extra)
        otlp_mod.OTLPMetricExporter.assert_called_once_with(
            endpoint="http://collector:4317"
        )

    def test_no_metrics_exporter_without_endpoint(self):
        mocks = self._run(MonitoringConfig())
        assert "opentelemetry.exporter.otlp.proto.grpc.metric_exporter" not in mocks

    def test_loguru_patcher_configured(self):
        mocks = _make_otel_mocks()
        with (
            patch.dict("sys.modules", mocks),
            patch("dbt_graphql.monitoring._setup_loguru"),
            patch("loguru.logger.configure") as mock_configure,
        ):
            from importlib import reload
            import dbt_graphql.monitoring as tel

            reload(tel)
            tel.configure_monitoring(MonitoringConfig())

        mock_configure.assert_called_once()
        assert "patcher" in mock_configure.call_args.kwargs

    def test_otlp_log_sink_added_when_logs_endpoint_set(self):
        mocks = _make_otel_mocks()
        with patch.dict("sys.modules", mocks):
            from importlib import reload
            import dbt_graphql.monitoring as tel

            reload(tel)
            with (
                patch.object(tel, "_setup_loguru"),
                patch.object(tel, "_instrument_loguru"),
                patch.object(tel, "_add_otlp_log_sink") as mock_sink,
            ):
                config = MonitoringConfig(
                    logs=LogsConfig(endpoint="http://collector:4317", protocol="grpc")
                )
                tel.configure_monitoring(config)

        mock_sink.assert_called_once()

    def test_no_otlp_log_sink_without_endpoint(self):
        mocks = _make_otel_mocks()
        with patch.dict("sys.modules", mocks):
            from importlib import reload
            import dbt_graphql.monitoring as tel

            reload(tel)
            with (
                patch.object(tel, "_setup_loguru"),
                patch.object(tel, "_instrument_loguru"),
                patch.object(tel, "_add_otlp_log_sink") as mock_sink,
            ):
                tel.configure_monitoring(MonitoringConfig())

        mock_sink.assert_not_called()
