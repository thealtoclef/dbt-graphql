"""Ariadne GraphQL ASGI sub-app.

Builds only the GraphQL endpoint — no Starlette, no lifespan, no auth
middleware, no MCP mounting. Composition (Starlette, lifespan, auth, MCP
co-mount, OTel) lives in ``dbt_graphql.serve.app``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ariadne import make_executable_schema
from ariadne.asgi import GraphQL
from graphql import DocumentNode, GraphQLSchema

from ..cache import CacheConfig
from ..compiler.connection import DatabaseManager
from ..config import GraphQLConfig
from ..formatter.graphql import _description_block, build_source_doc
from ..formatter.schema import TableRegistry
from .auth import JWTPayload
from .guards import make_query_guard_rules
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

    Primary-key columns are emitted with the built-in ``ID`` scalar so the
    PK signal reaches standard introspection without a custom directive.
    dbt descriptions are emitted as triple-quoted blocks above types and
    fields so GraphiQL / Apollo Studio / codegen all see them via standard
    introspection.
    """
    custom_scalars: set[str] = set()
    type_blocks: list[str] = []
    where_input_defs: list[str] = []

    for table_def in registry:
        type_block = _description_block(table_def.description)
        type_block += f"type {table_def.name} {{\n"
        input_lines = [f"input {table_def.name}WhereInput {{"]
        for col in table_def.columns:
            type_name = "ID" if col.is_pk else col.gql_type
            if type_name and type_name not in _STANDARD_GQL_SCALARS:
                custom_scalars.add(type_name)
            wrapped = f"[{type_name}]" if col.is_array else type_name
            if col.not_null:
                wrapped += "!"
            type_block += _description_block(col.description, indent="  ")
            type_block += f"  {col.name}: {wrapped}\n"
            if not col.is_array:
                input_lines.append(f"  {col.name}: {type_name}")
        type_block += "}"
        input_lines.append("}")
        type_blocks.append(type_block)
        where_input_defs.append("\n".join(input_lines))

    query_fields = [
        _description_block(t.description, indent="  ")
        + f"  {t.name}(limit: Int, offset: Int, where: {t.name}WhereInput): [{t.name}]"
        for t in registry
    ]
    query_fields.append(
        '  "The effective db.graphql SDL for this caller, with full custom directives."\n'
        "  _sdl: String!"
    )
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
    source_doc: DocumentNode
    build_context: Any  # callable: (jwt_payload, request|None) -> dict
    db: DatabaseManager
    policy_engine: PolicyEngine | None
    # Validation rules applied to every operation — the same instance is
    # reused by ``MCP run_graphql`` so the two transports cannot drift.
    validation_rules: list = field(default_factory=list)


def create_graphql_subapp(
    *,
    registry: TableRegistry,
    db: DatabaseManager,
    access_policy: AccessPolicy | None = None,
    cache_config: CacheConfig = CacheConfig(),
    graphql_config: GraphQLConfig = GraphQLConfig(),
    introspection: bool = False,
) -> GraphQLBundle:
    """Build the Ariadne GraphQL ASGI sub-app.

    The returned bundle is mountable under any Starlette path. The caller
    owns the Starlette app, lifespan (which must call
    ``dbt_graphql.cache.setup_cache(cache_config)``), auth middleware, and
    any co-mounted sub-apps (e.g. MCP). See
    ``dbt_graphql.serve.app.create_app``.
    """
    if any(t.name == "_sdl" for t in registry):
        raise ValueError(
            "model name '_sdl' collides with the reserved Query._sdl field; "
            "rename the model or exclude it via dbt.exclude."
        )
    policy_engine = PolicyEngine(access_policy) if access_policy is not None else None
    source_doc = build_source_doc(registry)
    query_type = create_query_type(registry)
    gql_schema = make_executable_schema(_build_ariadne_sdl(registry), query_type)

    validation_rules = make_query_guard_rules(
        max_depth=graphql_config.query_max_depth,
        max_fields=graphql_config.query_max_fields,
        max_limit=graphql_config.query_max_limit,
    )

    def build_context(jwt_payload: JWTPayload, request: Any = None) -> dict[str, Any]:
        return {
            "request": request,
            "registry": registry,
            "source_doc": source_doc,
            "db": db,
            "jwt_payload": jwt_payload,
            "policy_engine": policy_engine,
            "cache_config": cache_config,
        }

    asgi = GraphQL(
        gql_schema,
        context_value=lambda req, _data=None: build_context(req.user.payload, req),
        validation_rules=validation_rules,
        http_handler=build_graphql_http_handler(),
        introspection=introspection,
    )
    return GraphQLBundle(
        asgi=asgi,
        schema=gql_schema,
        registry=registry,
        source_doc=source_doc,
        build_context=build_context,
        db=db,
        policy_engine=policy_engine,
        validation_rules=validation_rules,
    )
