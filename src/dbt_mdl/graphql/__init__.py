"""dbt-mdl GraphQL-to-SQL engine.

Serves a ``db.graphql`` SDL as a live GraphQL API backed by SQLAlchemy async queries.
"""


def __getattr__(name):
    if name in ("create_app", "serve"):
        from . import server

        return getattr(server, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
