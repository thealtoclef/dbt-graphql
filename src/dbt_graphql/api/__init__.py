"""GraphQL API server (Starlette + Ariadne + Granian)."""


def __getattr__(name):
    if name in ("create_app", "serve", "serve_mcp_http"):
        from . import app

        return getattr(app, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
