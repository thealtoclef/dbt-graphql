"""Ariadne GraphQL ASGI sub-app.

Builds only the GraphQL endpoint — no Starlette, no lifespan, no auth
middleware, no MCP mounting. Composition (Starlette, lifespan, auth, MCP
co-mount, OTel) lives in ``dbt_graphql.serve.app``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ariadne import make_executable_schema
from ariadne.asgi import GraphQL
from graphql import GraphQLSchema

from ..cache import CacheConfig
from ..compiler.connection import DatabaseManager
from ..formatter.schema import TableRegistry
from .auth import JWTPayload
from .monitoring import build_graphql_http_handler
from .policy import AccessPolicy, PolicyEngine
from .resolvers import create_query_type

_STANDARD_GQL_SCALARS = {"String", "Int", "Float", "Boolean", "ID"}


def _build_ariadne_sdl(registry: TableRegistry) -> str:
    """Build a standard GraphQL SDL (without db.graphql custom directives) for Ariadne.

    The db.graphql format uses custom directives (@table, @column, @relation, etc.)
    that Ariadne's schema builder doesn't understand. This function builds a clean
    SDL with custom types declared as scalars, per-table WhereInput types, and a
    Query type for all tables.
    """
    custom_scalars: set[str] = set()
    type_blocks: list[str] = []
    where_input_defs: list[str] = []

    for table_def in registry:
        lines = [f"type {table_def.name} {{"]
        input_lines = [f"input {table_def.name}WhereInput {{"]
        for col in table_def.columns:
            type_name = col.gql_type
            if type_name and type_name not in _STANDARD_GQL_SCALARS:
                custom_scalars.add(type_name)
            wrapped = f"[{type_name}]" if col.is_array else type_name
            if col.not_null:
                wrapped += "!"
            lines.append(f"  {col.name}: {wrapped}")
            if not col.is_array:
                input_lines.append(f"  {col.name}: {type_name}")
        lines.append("}")
        input_lines.append("}")
        type_blocks.append("\n".join(lines))
        where_input_defs.append("\n".join(input_lines))

    query_fields = [
        f"  {t.name}(limit: Int, offset: Int, where: {t.name}WhereInput): [{t.name}]"
        for t in registry
    ]
    query_block = "type Query {\n" + "\n".join(query_fields) + "\n}"

    scalar_defs = [f"scalar {s}" for s in sorted(custom_scalars)]
    parts = scalar_defs + where_input_defs + type_blocks + [query_block]
    return "\n\n".join(parts) + "\n"


@dataclass
class GraphQLBundle:
    """Everything the serve layer needs to mount GraphQL and let other
    sub-apps (MCP) re-execute queries through the same engine + context.

    ``asgi`` mounts under ``/graphql``. The remaining attributes are
    exposed so the MCP ``run_graphql`` tool runs queries through the
    same executable schema with the same per-request context the HTTP
    layer would have built, and so MCP discovery tools share the one
    ``PolicyEngine`` instance that gates GraphQL.
    """

    asgi: GraphQL
    schema: GraphQLSchema
    registry: TableRegistry
    build_context: Any  # callable: (jwt_payload, request|None) -> dict
    db: DatabaseManager
    policy_engine: PolicyEngine | None


def create_graphql_subapp(
    *,
    registry: TableRegistry,
    db: DatabaseManager,
    access_policy: AccessPolicy | None = None,
    cache_config: CacheConfig | None = None,
    introspection: bool = False,
) -> GraphQLBundle:
    """Build the Ariadne GraphQL ASGI sub-app.

    The returned bundle is mountable under any Starlette path. The caller
    owns the Starlette app, lifespan, auth middleware, and any co-mounted
    sub-apps (e.g. MCP). See ``dbt_graphql.serve.app.create_app``.
    """
    policy_engine = PolicyEngine(access_policy) if access_policy is not None else None
    query_type = create_query_type(registry)
    gql_schema = make_executable_schema(_build_ariadne_sdl(registry), query_type)

    def build_context(jwt_payload: JWTPayload, request: Any = None) -> dict[str, Any]:
        return {
            "request": request,
            "registry": registry,
            "db": db,
            "jwt_payload": jwt_payload,
            "policy_engine": policy_engine,
            "cache_config": cache_config,
        }

    asgi = GraphQL(
        gql_schema,
        context_value=lambda req, _data=None: build_context(req.user.payload, req),
        http_handler=build_graphql_http_handler(),
        introspection=introspection,
    )
    return GraphQLBundle(
        asgi=asgi,
        schema=gql_schema,
        registry=registry,
        build_context=build_context,
        db=db,
        policy_engine=policy_engine,
    )
