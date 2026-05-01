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
from .sdl.generator import _description_block, build_source_doc
from ..schema.models import TableRegistry
from ..schema.constants import AGGREGATE_FIELD, STANDARD_GQL_SCALARS
from ..schema.helpers import numeric_columns, scalar_columns
from ..schema.constants import (
    LOGICAL_OPS,
    SCALAR_FILTER_OPS,
    _OPS_TAKING_BOOL,
    LIST_OPS,
)
from .auth import JWTPayload
from .guards import make_query_guard_rules
from .monitoring import build_graphql_http_handler
from .policy import AccessPolicy, PolicyEngine
from .resolvers import create_query_type


def _generate_shared_filter_types() -> str:
    """Generate {Scalar}Filter SDL from operator definitions."""
    blocks = []
    for scalar, ops in SCALAR_FILTER_OPS.items():
        lines = [f"input {scalar}Filter {{"]
        for op in sorted(ops):
            if op in _OPS_TAKING_BOOL:
                rhs = "Boolean"
            elif op in LIST_OPS:
                rhs = f"[{scalar}!]"
            else:
                rhs = scalar
            lines.append(f"  {op}: {rhs}")
        lines.append("}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


_SHARED_FILTER_TYPES = _generate_shared_filter_types()

_ORDER_DIRECTION_ENUM = """\
enum OrderDirection {
  asc
  desc
}"""


def _build_ariadne_sdl(registry: TableRegistry) -> str:
    """Build GraphJin-style GraphQL SDL for Ariadne.

    Emits per-table:
    - ``type {T}`` — row type with real columns AND aggregate fields
    - ``input {T}Where`` — recursive and/or/not + per-column unprefixed filter inputs
    - ``input {T}OrderBy`` — every real column and aggregate field → OrderDirection
    - ``enum {T}Column`` — one value per real scalar column

    Query root: ``Query.{T}(where, order_by, limit, offset, distinct) -> [T!]``

    Shared types (``OrderDirection``, ``StringFilter``, ``IntFilter``, ``FloatFilter``,
    ``BooleanFilter``) are emitted once before all tables.
    """
    custom_scalars: set[str] = set()
    type_blocks: list[str] = []
    where_defs: list[str] = []
    order_by_defs: list[str] = []
    column_enum_defs: list[str] = []
    agg_type_defs: list[str] = []

    for table_def in registry:
        name = table_def.name

        # --- Generate aggregate object types ---
        numeric_cols = numeric_columns(table_def.columns)
        all_cols = scalar_columns(table_def.columns)

        # Generate separate named types for each aggregate operation
        if numeric_cols:
            # sum type
            fields = "\n".join(f"  {c.name}: {c.gql_type}" for c in numeric_cols)
            agg_type_defs.append(f"type {name}_aggregate_sum {{\n{fields}\n}}")
            # avg type (always Float)
            fields = "\n".join(f"  {c.name}: Float" for c in numeric_cols)
            agg_type_defs.append(f"type {name}_aggregate_avg {{\n{fields}\n}}")
            # stddev type
            fields = "\n".join(f"  {c.name}: Float" for c in numeric_cols)
            agg_type_defs.append(f"type {name}_aggregate_stddev {{\n{fields}\n}}")
            # var type
            fields = "\n".join(f"  {c.name}: Float" for c in numeric_cols)
            agg_type_defs.append(f"type {name}_aggregate_var {{\n{fields}\n}}")
        if all_cols:
            # count_distinct type
            fields = "\n".join(f"  {c.name}: Int" for c in all_cols)
            agg_type_defs.append(
                f"type {name}_aggregate_count_distinct {{\n{fields}\n}}"
            )
            # min type
            fields = "\n".join(f"  {c.name}: {c.gql_type}" for c in all_cols)
            agg_type_defs.append(f"type {name}_aggregate_min {{\n{fields}\n}}")
            # max type
            fields = "\n".join(f"  {c.name}: {c.gql_type}" for c in all_cols)
            agg_type_defs.append(f"type {name}_aggregate_max {{\n{fields}\n}}")

        # Single aggregate type referencing the named operation types
        agg_fields_lines = ["  count: Int!"]
        if numeric_cols:
            agg_fields_lines.append(f"  sum: {name}_aggregate_sum")
            agg_fields_lines.append(f"  avg: {name}_aggregate_avg")
            agg_fields_lines.append(f"  stddev: {name}_aggregate_stddev")
            agg_fields_lines.append(f"  var: {name}_aggregate_var")
        if all_cols:
            agg_fields_lines.append(
                f"  count_distinct: {name}_aggregate_count_distinct"
            )
            agg_fields_lines.append(f"  min: {name}_aggregate_min")
            agg_fields_lines.append(f"  max: {name}_aggregate_max")

        agg_type_defs.append(
            f"type {name}_aggregate {{\n" + "\n".join(agg_fields_lines) + "\n}"
        )

        # --- type {T} with real columns AND _aggregate field ---
        type_block = _description_block(table_def.description)
        type_block += f"type {name} {{\n"
        for col in table_def.columns:
            type_name = col.gql_type
            if type_name and type_name not in STANDARD_GQL_SCALARS:
                custom_scalars.add(type_name)
            wrapped = f"[{type_name}]" if col.is_array else type_name
            if col.not_null:
                wrapped += "!"
            type_block += _description_block(col.description, indent="  ")
            type_block += f"  {col.name}: {wrapped}\n"

        # _aggregate wrapper field
        type_block += f"  {AGGREGATE_FIELD}: {name}_aggregate!\n"

        type_block += "}"
        type_blocks.append(type_block)

        # --- input {T}Where ---
        lines = [
            f"input {name}Where {{",
            f"  _and: [{name}Where!]",
            f"  _or: [{name}Where!]",
            f"  _not: {name}Where",
        ]
        for col in table_def.columns:
            if col.is_array:
                continue
            gql_type = col.gql_type
            filter_type = f"{gql_type}Filter"
            lines.append(f"  {col.name}: {filter_type}")
        lines.append("}")
        where_defs.append("\n".join(lines))

        # --- input {T}OrderBy ---
        lines = [f"input {name}OrderBy {{"]
        for col in table_def.columns:
            if not col.is_array:
                lines.append(f"  {col.name}: OrderDirection")
        # _aggregate fields for order_by
        lines.append(f"  {AGGREGATE_FIELD}: OrderDirection")
        lines.append("}")
        order_by_defs.append("\n".join(lines))

        # --- enum {T}Column ---
        scalar_cols = [
            c
            for c in table_def.columns
            if c.gql_type in STANDARD_GQL_SCALARS and not c.is_array
        ]
        lines = [f"enum {name}Column {{"]
        for col in scalar_cols:
            lines.append(f"  {col.name}")
        lines.append("}")
        column_enum_defs.append("\n".join(lines))

    query_fields = [
        _description_block(t.description, indent="  ")
        + f"  {t.name}(where: {t.name}Where, order_by: {t.name}OrderBy, limit: Int, offset: Int, distinct: Boolean): [{t.name}!]!"
        for t in registry
    ]
    query_fields.append(
        '  """The effective db.graphql SDL for this caller, with full custom directives.\n'
        "If `tables` is given, only those tables are emitted; names the caller cannot "
        'see are silently skipped."""\n'
        "  _sdl(tables: [String!]): String!"
    )
    query_fields.append(
        '  """Names and descriptions of tables visible to this caller after policy '
        'pruning. Use as the cheap index before drilling in via `_sdl(tables: ...)`."""\n'
        "  _tables: [_TableInfo!]!"
    )
    query_block = "type Query {\n" + "\n".join(query_fields) + "\n}"

    table_info_block = (
        '"""Summary of a single table — the index-page projection. Description '
        "comes from the dbt manifest; structure (columns, relations) belongs to "
        '`_sdl`."""\n'
        "type _TableInfo {\n"
        "  name: String!\n"
        '  "dbt-authored description; empty string when none is set."\n'
        "  description: String!\n"
        "}"
    )

    scalar_defs = [f"scalar {s}" for s in sorted(custom_scalars)]
    parts = (
        scalar_defs
        + [_SHARED_FILTER_TYPES, _ORDER_DIRECTION_ENUM]
        + column_enum_defs
        + where_defs
        + order_by_defs
        + agg_type_defs
        + type_blocks
        + [table_info_block, query_block]
    )
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
) -> GraphQLBundle:
    """Build the Ariadne GraphQL ASGI sub-app.

    The returned bundle is mountable under any Starlette path. The caller
    owns the Starlette app, lifespan (which must call
    ``dbt_graphql.cache.setup_cache(cache_config)``), auth middleware, and
    any co-mounted sub-apps (e.g. MCP). See
    ``dbt_graphql.serve.app.create_app``.

    Standard ``__schema`` introspection is always enabled — the
    authoritative caller-effective view lives in the policy-pruned
    ``Query._sdl`` field, which is what auth-sensitive clients should use.
    """
    table_names = {t.name for t in registry}
    _RESERVED_TYPES = {
        "_sdl",
        "_tables",
        "_TableInfo",
        "OrderDirection",
        "StringFilter",
        "IntFilter",
        "FloatFilter",
        "BooleanFilter",
    }
    _DERIVED_SUFFIXES = (
        "Where",
        "OrderBy",
        "Column",
        AGGREGATE_FIELD,
    )
    for t in registry:
        if t.name in _RESERVED_TYPES:
            raise ValueError(
                f"model name '{t.name}' collides with a reserved schema name."
            )
        for suffix in _DERIVED_SUFFIXES:
            derived = f"{t.name}{suffix}"
            if derived in table_names:
                raise ValueError(
                    f"model name '{derived}' collides with the synthetic "
                    f"type/input/enum '{derived}' derived from another model."
                )
        col_names = {c.name for c in t.columns}

        # Top-level aggregate field name
        agg_field_names = {
            AGGREGATE_FIELD,
        }

        # Check exact collision: column name exactly matches an aggregate field name
        for col_name in col_names:
            if col_name in agg_field_names:
                raise ValueError(
                    f"table '{t.name}': column '{col_name}' collides with "
                    f"the aggregate field of the same name."
                )

        # Reject columns whose names collide with logical operators in Where inputs.
        # A column named _and, _or, or _not would shadow the _and/_or/_not
        # logical combinators in {T}Where and produce a GraphQL schema error.
        for col_name in col_names:
            if col_name in LOGICAL_OPS:
                raise ValueError(
                    f"table '{t.name}': column '{col_name}' collides with the "
                    f"logical operator of the same name in Where inputs."
                )

    policy_engine = PolicyEngine(access_policy) if access_policy is not None else None
    source_doc = build_source_doc(registry)
    query_type, object_types = create_query_type(registry)
    ariadne_sdl_str = _build_ariadne_sdl(registry)
    gql_schema = make_executable_schema(ariadne_sdl_str, query_type, *object_types)

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
        introspection=True,
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
