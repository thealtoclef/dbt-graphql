"""GraphQL core — Starlette + Ariadne schema assembly and resolvers."""


def __getattr__(name):
    if name == "create_app":
        from . import app

        return getattr(app, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
