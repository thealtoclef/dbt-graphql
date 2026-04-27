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

        for c in mock_instrumentor.instrument.call_args_list:
            assert c != call(engine=async_engine), (
                "instrument() must not receive the AsyncEngine directly"
            )


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


class TestBuildGraphqlHttpHandler:
    def test_handler_carries_metrics_and_otel_extensions(self):
        """Handler is built and carries both extensions."""
        import dbt_graphql.api.monitoring as tel
        from ariadne.contrib.tracing.opentelemetry import OpenTelemetryExtension

        handler = tel.build_graphql_http_handler()

        exts = handler.extensions
        if callable(exts) and not isinstance(exts, (list, tuple)):
            exts = exts(None, None)  # type: ignore[misc]
        assert OpenTelemetryExtension in exts
        assert tel.GraphQLMetricsExtension in exts

    def test_pool_timeout_response_elevated_to_503(self):
        """Result with a POOL_TIMEOUT error → HTTP 503 + Retry-After header."""
        import asyncio
        import dbt_graphql.api.monitoring as tel
        from dbt_graphql.api.resolvers import POOL_TIMEOUT_CODE

        handler = tel.build_graphql_http_handler()
        result = {
            "errors": [
                {
                    "message": "database connection pool exhausted",
                    "extensions": {"code": POOL_TIMEOUT_CODE, "retry_after": 7},
                }
            ]
        }
        resp = asyncio.run(handler.create_json_response(MagicMock(), result, False))
        assert resp.status_code == 503
        assert resp.headers["retry-after"] == "7"

    def test_normal_response_unaffected(self):
        """Result with no POOL_TIMEOUT error → unchanged 200/400 path."""
        import asyncio
        import dbt_graphql.api.monitoring as tel

        handler = tel.build_graphql_http_handler()
        result = {"data": {"customers": []}}
        resp = asyncio.run(handler.create_json_response(MagicMock(), result, True))
        assert resp.status_code == 200
        assert "retry-after" not in {k.lower() for k in resp.headers}
