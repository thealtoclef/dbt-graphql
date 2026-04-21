"""Unit tests for api/monitoring.py.

All OTel packages are mocked so these tests run without the [api] extra installed.
The critical invariant: instrument_sqlalchemy must pass engine.sync_engine (the
underlying sync engine) to SQLAlchemyInstrumentor, not the AsyncEngine itself.
SQLAlchemy raises NotImplementedError if you register sync event listeners directly
on an AsyncEngine — this is exactly the production bug that prompted these tests.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch, call


class TestInstrumentSqlalchemy:
    def test_passes_sync_engine_to_instrumentor(self):
        """The core regression test: must use engine.sync_engine, not engine."""
        mock_instrumentor = MagicMock()
        mock_instrumentor_class = MagicMock(return_value=mock_instrumentor)

        async_engine = MagicMock()
        async_engine.sync_engine = MagicMock(name="sync_engine")

        with patch.dict(
            "sys.modules",
            {
                "opentelemetry.instrumentation.sqlalchemy": MagicMock(
                    SQLAlchemyInstrumentor=mock_instrumentor_class
                )
            },
        ):
            from importlib import reload
            import dbt_graphql.api.monitoring as tel

            reload(tel)
            tel.instrument_sqlalchemy(async_engine)

        mock_instrumentor.instrument.assert_called_once_with(
            engine=async_engine.sync_engine
        )

    def test_does_not_pass_async_engine_directly(self):
        """Passing the AsyncEngine directly would raise NotImplementedError at runtime."""
        mock_instrumentor = MagicMock()
        mock_instrumentor_class = MagicMock(return_value=mock_instrumentor)

        async_engine = MagicMock()
        async_engine.sync_engine = MagicMock(name="sync_engine")

        with patch.dict(
            "sys.modules",
            {
                "opentelemetry.instrumentation.sqlalchemy": MagicMock(
                    SQLAlchemyInstrumentor=mock_instrumentor_class
                )
            },
        ):
            from importlib import reload
            import dbt_graphql.api.monitoring as tel

            reload(tel)
            tel.instrument_sqlalchemy(async_engine)

        # Verify the async engine itself was NOT passed
        for c in mock_instrumentor.instrument.call_args_list:
            assert c != call(engine=async_engine), (
                "instrument() must not receive the AsyncEngine directly"
            )

    def test_noop_when_package_missing(self):
        """No error raised when opentelemetry-instrumentation-sqlalchemy is absent."""
        with patch.dict(
            "sys.modules",
            {"opentelemetry.instrumentation.sqlalchemy": None},
        ):
            from importlib import reload
            import dbt_graphql.api.monitoring as tel

            reload(tel)
            tel.instrument_sqlalchemy(MagicMock())  # must not raise


class TestInstrumentStarlette:
    def test_calls_instrument_app(self):
        mock_instrumentor = MagicMock()
        mock_instrumentor_class = MagicMock(return_value=mock_instrumentor)
        app = MagicMock()

        with patch.dict(
            "sys.modules",
            {
                "opentelemetry.instrumentation.starlette": MagicMock(
                    StarletteInstrumentor=mock_instrumentor_class
                )
            },
        ):
            from importlib import reload
            import dbt_graphql.api.monitoring as tel

            reload(tel)
            tel.instrument_starlette(app)

        mock_instrumentor.instrument_app.assert_called_once_with(app)

    def test_noop_when_package_missing(self):
        with patch.dict(
            "sys.modules",
            {"opentelemetry.instrumentation.starlette": None},
        ):
            from importlib import reload
            import dbt_graphql.api.monitoring as tel

            reload(tel)
            tel.instrument_starlette(MagicMock())  # must not raise


class TestBuildGraphqlHttpHandler:
    def test_returns_handler_when_available(self):
        mock_extension = MagicMock(name="OpenTelemetryExtension")
        mock_handler_class = MagicMock(name="GraphQLHTTPHandler")
        mock_handler_instance = MagicMock()
        mock_handler_class.return_value = mock_handler_instance

        with patch.dict(
            "sys.modules",
            {
                "ariadne.asgi.handlers": MagicMock(
                    GraphQLHTTPHandler=mock_handler_class
                ),
                "ariadne.contrib.tracing.opentelemetry": MagicMock(
                    OpenTelemetryExtension=mock_extension
                ),
            },
        ):
            from importlib import reload
            import dbt_graphql.api.monitoring as tel

            reload(tel)
            result = tel.build_graphql_http_handler()

        assert result is mock_handler_instance
        mock_handler_class.assert_called_once_with(extensions=[mock_extension])

    def test_returns_none_when_package_missing(self):
        import sys

        for key in list(sys.modules):
            if "ariadne.contrib.tracing" in key:
                sys.modules.pop(key)

        with patch.dict(
            "sys.modules",
            {"ariadne.contrib.tracing.opentelemetry": None},
        ):
            from importlib import reload
            import dbt_graphql.api.monitoring as tel

            reload(tel)
            result = tel.build_graphql_http_handler()

        assert result is None
