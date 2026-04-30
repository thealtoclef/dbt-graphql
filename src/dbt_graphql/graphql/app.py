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
from ..compiler.query import agg_fields_for_table
from ..config import GraphQLConfig
from ..formatter.graphql import _description_block, build_source_doc
from ..formatter.schema import TableRegistry
from .auth import JWTPayload
from .guards import make_query_guard_rules
from .monitoring import build_graphql_http_handler
from .policy import AccessPolicy, PolicyEngine
from .resolvers import create_query_type

_STANDARD_GQL_SCALARS = {"String", "Int", "Float", "Boolean"}

# Global synthetic types are framework-private — leading ``_`` matches the
# convention set by ``_sdl`` / ``_tables`` / ``_TableInfo``. PK columns
# keep their underlying scalar (``Int`` / ``String`` / etc.) so they
# dispatch to the same ``_<Scalar>_comparison_exp`` as any other column
# of that type — the PK signal travels via the ``@id`` directive in the
# printed db.graphql artefact and ``Query._sdl``, not through the scalar.
_GQL_SCALAR_TO_CMP_EXP: dict[str, str] = {
    "String": "_String_comparison_exp",
    "Int": "_Int_comparison_exp",
    "Float": "_Float_comparison_exp",
    "Boolean": "_Boolean_comparison_exp",
}

_COMPARISON_EXP_TYPES = """\
input _String_comparison_exp {
  _eq: String  _neq: String
  _gt: String  _gte: String
  _lt: String  _lte: String
  _in: [String!]  _nin: [String!]
  _is_null: Boolean
  _like: String  _nlike: String
  _ilike: String  _nilike: String
}

input _Int_comparison_exp {
  _eq: Int  _neq: Int
  _gt: Int  _gte: Int
  _lt: Int  _lte: Int
  _in: [Int!]  _nin: [Int!]
  _is_null: Boolean
}

input _Float_comparison_exp {
  _eq: Float  _neq: Float
  _gt: Float  _gte: Float
  _lt: Float  _lte: Float
  _in: [Float!]  _nin: [Float!]
  _is_null: Boolean
}

input _Boolean_comparison_exp {
  _eq: Boolean
  _is_null: Boolean
}"""

_ORDER_BY_ENUM = """\
enum _order_by {
  asc
  desc
}"""


def _build_ariadne_sdl(registry: TableRegistry) -> str:
    """Build a standard GraphQL SDL (without db.graphql custom directives) for Ariadne.

    Emits per-table:
    - ``type {T}`` — row type (unchanged from db.graphql)
    - ``type {T}Result`` — result envelope with ``nodes``, aggregate fields, ``group``
    - ``type {T}_group`` — GROUP BY row: dimensions + flat aggregate fields
    - ``input {T}_bool_exp`` — recursive bool_exp WHERE filter
    - ``input {T}_order_by`` — ORDER BY for ``nodes``
    - ``input {T}_group_order_by`` — flat ORDER BY for ``group``

    Shared framework-private types (``_<Scalar>_comparison_exp``, ``_order_by``)
    are emitted once. The leading underscore matches the ``_sdl`` / ``_tables``
    / ``_TableInfo`` convention for synthesized non-dbt surface.
    """
    custom_scalars: set[str] = set()
    type_blocks: list[str] = []
    result_type_blocks: list[str] = []
    group_type_blocks: list[str] = []
    bool_exp_defs: list[str] = []
    order_by_defs: list[str] = []
    group_order_by_defs: list[str] = []

    for table_def in registry:
        name = table_def.name
        agg_fields = agg_fields_for_table(table_def)

        # --- type {T} ---
        type_block = _description_block(table_def.description)
        type_block += f"type {name} {{\n"
        for col in table_def.columns:
            type_name = col.gql_type
            if type_name and type_name not in _STANDARD_GQL_SCALARS:
                custom_scalars.add(type_name)
            wrapped = f"[{type_name}]" if col.is_array else type_name
            if col.not_null:
                wrapped += "!"
            type_block += _description_block(col.description, indent="  ")
            type_block += f"  {col.name}: {wrapped}\n"
        type_block += "}"
        type_blocks.append(type_block)

        # --- input {T}_bool_exp ---
        lines = [
            f"input {name}_bool_exp {{",
            f"  _and: [{name}_bool_exp!]",
            f"  _or:  [{name}_bool_exp!]",
            f"  _not: {name}_bool_exp",
        ]
        for col in table_def.columns:
            if col.is_array:
                continue
            cmp_exp = _GQL_SCALAR_TO_CMP_EXP.get(col.gql_type, "_String_comparison_exp")
            lines.append(f"  {col.name}: {cmp_exp}")
        lines.append("}")
        bool_exp_defs.append("\n".join(lines))

        # --- input {T}_order_by ---
        lines = [f"input {name}_order_by {{"]
        for col in table_def.columns:
            if not col.is_array:
                lines.append(f"  {col.name}: _order_by")
        lines.append("}")
        order_by_defs.append("\n".join(lines))

        # --- input {T}_group_order_by (flat: dimensions + aggregate fields) ---
        lines = [f"input {name}_group_order_by {{"]
        for col in table_def.columns:
            if not col.is_array:
                lines.append(f"  {col.name}: _order_by")
        for fname, _ in agg_fields:
            lines.append(f"  {fname}: _order_by")
        lines.append("}")
        group_order_by_defs.append("\n".join(lines))

        # --- type {T}Result ---
        lines = [f"type {name}Result {{"]
        lines.append(
            f"  nodes(order_by: [{name}_order_by!], limit: Int, offset: Int): [{name}!]!"
        )
        for fname, ftype in agg_fields:
            lines.append(f"  {fname}: {ftype}")
        lines.append(
            f"  group(order_by: [{name}_group_order_by!], limit: Int, offset: Int): [{name}_group!]!"
        )
        lines.append("}")
        result_type_blocks.append("\n".join(lines))

        # --- type {T}_group ---
        lines = [f"type {name}_group {{"]
        for col in table_def.columns:
            if col.is_array:
                continue
            lines.append(f"  {col.name}: {col.gql_type}")
        for fname, ftype in agg_fields:
            lines.append(f"  {fname}: {ftype}")
        lines.append("}")
        group_type_blocks.append("\n".join(lines))

    query_fields = [
        _description_block(t.description, indent="  ")
        + f"  {t.name}(where: {t.name}_bool_exp): {t.name}Result"
        for t in registry
    ]
    query_fields.append(
        '  "The effective db.graphql SDL for this caller, with full custom directives.\\n'
        "If `tables` is given, only those tables are emitted; names the caller cannot "
        'see are silently skipped."\n'
        "  _sdl(tables: [String!]): String!"
    )
    query_fields.append(
        '  "Names and descriptions of tables visible to this caller after policy '
        'pruning. Use as the cheap index before drilling in via `_sdl(tables: ...)`."\n'
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
        + [_COMPARISON_EXP_TYPES, _ORDER_BY_ENUM]
        + bool_exp_defs
        + order_by_defs
        + group_order_by_defs
        + type_blocks
        + result_type_blocks
        + group_type_blocks
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
        "_order_by",
        "_String_comparison_exp",
        "_Int_comparison_exp",
        "_Float_comparison_exp",
        "_Boolean_comparison_exp",
    }
    _DERIVED_SUFFIXES = (
        "Result",
        "_group",
        "_bool_exp",
        "_order_by",
        "_group_order_by",
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
                    f"type/input name '{derived}' derived from another model."
                )
        # Aggregate field names share a namespace with real columns on
        # ``{T}_group`` — collisions are rejected here, not silently shadowed.
        col_names = {c.name for c in t.columns}
        for fname, _ in agg_fields_for_table(t):
            if fname in col_names:
                raise ValueError(
                    f"table '{t.name}': column '{fname}' clashes with the "
                    f"synthetic aggregate field of the same name."
                )

    policy_engine = PolicyEngine(access_policy) if access_policy is not None else None
    source_doc = build_source_doc(registry)
    query_type, object_types = create_query_type(registry)
    gql_schema = make_executable_schema(
        _build_ariadne_sdl(registry), query_type, *object_types
    )

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
