"""Pre-execution query guards: depth and field-count limits."""

from graphql import DocumentNode, parse
from graphql.error import GraphQLSyntaxError

__all__ = ["check_query_limits"]

# Introspection field names — excluded from depth counting and field counting.
_INTROSPECTION_FIELDS = frozenset(
    {
        "__schema",
        "__typename",
        "__type",
        "__typeName",
        "__field",
        "__inputValue",
        "__enumValue",
        "__directive",
    }
)


def _is_introspection(name: str) -> bool:
    return name in _INTROSPECTION_FIELDS


def _walk_selections(selections, depth):
    """Walk a list of selection nodes at a fixed ``depth``.

    Returns (max_depth, leaf_field_count).

    Depth represents the nesting of *data-model* relationships.
    A field with a selection set is at ``depth``; its children are at ``depth+1``.
    Introspection fields (``__*``) are transparent: they do not cause a
    descent, and their leaf fields are not counted.
    """
    max_seen = depth
    leaf_total = 0

    for node in selections:
        if not hasattr(node, "name") or not node.name:
            continue

        field_name = node.name.value

        # Introspection fields: skip entirely (no descent, no leaf count)
        if _is_introspection(field_name):
            continue

        has_selection_set = hasattr(node, "selection_set") and node.selection_set

        if not has_selection_set:
            # Data-model leaf field — counts at current depth
            leaf_total += 1
            max_seen = max(max_seen, depth)
        else:
            # Check if there are any non-introspection children.
            child_nodes = node.selection_set.selections
            non_intro_children = [
                c
                for c in child_nodes
                if hasattr(c, "name") and c.name and not _is_introspection(c.name.value)
            ]

            if not non_intro_children:
                # All children are introspection — treat this field as a leaf
                leaf_total += 1
                max_seen = max(max_seen, depth)
            else:
                # Descend to children at depth+1
                d, f = _walk_selections(non_intro_children, depth=depth + 1)
                max_seen = max(max_seen, d)
                leaf_total += f

    return max_seen, leaf_total


def check_query_limits(query: str, max_depth: int, max_fields: int) -> list[str]:
    """Return list of error messages; empty if the query is within limits.

    Depth is the maximum nesting of selection sets. Introspection fields
    (``__schema``, ``__type``, ``__typename``, etc.) are excluded from
    depth counting as they do not follow the data model's relationship chains.
    """
    try:
        doc: DocumentNode = parse(query)
    except GraphQLSyntaxError:
        # Defer syntax errors to the GraphQL engine so it can return a proper
        # parse-error response. Guards only reject well-formed queries that
        # exceed limits.
        return []
    errors: list[str] = []

    max_seen_depth = 0
    total_leaf_fields = 0

    for definition in doc.definitions:
        kind = definition.kind if hasattr(definition, "kind") else None

        if kind in ("operation_definition", "fragment_definition"):
            if not hasattr(definition, "selection_set") or not definition.selection_set:
                continue
            for sel in definition.selection_set.selections:
                # Top-level introspection fields are entirely excluded
                if hasattr(sel, "name") and sel.name and _is_introspection(sel.name.value):
                    continue
                d, f = _walk_selections([sel], depth=1)
                max_seen_depth = max(max_seen_depth, d)
                total_leaf_fields += f

    if max_seen_depth > max_depth:
        errors.append(
            f"Query depth {max_seen_depth} exceeds the limit of {max_depth}"
        )
    if total_leaf_fields > max_fields:
        errors.append(
            f"Query contains {total_leaf_fields} fields which exceeds the limit of {max_fields}"
        )
    return errors
