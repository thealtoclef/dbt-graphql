"""Query guard validation rules: depth, field count, list-pagination cap.

Implemented as ``graphql-core`` validation rules so the same checks apply to
both transports without duplication: Ariadne's ``GraphQL`` ASGI app accepts
``validation_rules=`` directly; the MCP ``run_graphql`` path runs the same
``validate(...)`` step before executing. Errors carry ``extensions.code``
so HTTP clients route on a stable token instead of message text.
"""

from __future__ import annotations

from graphql import GraphQLError
from graphql.language import (
    FieldNode,
    FragmentDefinitionNode,
    IntValueNode,
    OperationDefinitionNode,
    SelectionSetNode,
)
from graphql.validation import ValidationContext, ValidationRule

__all__ = [
    "MAX_DEPTH_CODE",
    "MAX_FIELDS_CODE",
    "MAX_LIST_LIMIT_CODE",
    "make_query_guard_rules",
]

MAX_DEPTH_CODE = "MAX_DEPTH_EXCEEDED"
MAX_FIELDS_CODE = "MAX_FIELDS_EXCEEDED"
MAX_LIST_LIMIT_CODE = "MAX_LIST_LIMIT_EXCEEDED"

# Introspection field names — excluded from depth + field counting because
# they don't traverse the data-model relationship graph.
_INTROSPECTION_FIELDS = frozenset(
    {
        "__schema",
        "__typename",
        "__type",
    }
)

# Argument names commonly used to bound list resolvers.
_LIST_LIMIT_ARGS = ("limit", "first")


def _is_introspection(name: str) -> bool:
    return name in _INTROSPECTION_FIELDS


def _walk(selection_set: SelectionSetNode | None, depth: int) -> tuple[int, int]:
    """Walk a selection set; return (max_depth, leaf_field_count).

    Depth represents data-model nesting: a field with sub-selections is at
    ``depth``, its children at ``depth+1``. Introspection fields are
    transparent (no descent, not counted as leaves).
    """
    if selection_set is None:
        return depth, 0

    max_seen = depth
    leaf_total = 0

    for node in selection_set.selections:
        if not isinstance(node, FieldNode):
            # Inline fragments / fragment spreads — descend without counting
            # the spread itself as a level.
            if hasattr(node, "selection_set") and node.selection_set:
                d, f = _walk(node.selection_set, depth=depth)
                max_seen = max(max_seen, d)
                leaf_total += f
            continue

        if _is_introspection(node.name.value):
            continue

        if node.selection_set is None:
            leaf_total += 1
            max_seen = max(max_seen, depth)
            continue

        # Strip introspection-only children before deciding leaf vs descent
        non_intro = [
            c
            for c in node.selection_set.selections
            if not (isinstance(c, FieldNode) and _is_introspection(c.name.value))
        ]
        if not non_intro:
            leaf_total += 1
            max_seen = max(max_seen, depth)
            continue

        d, f = _walk(node.selection_set, depth=depth + 1)
        max_seen = max(max_seen, d)
        leaf_total += f

    return max_seen, leaf_total


def make_query_guard_rules(
    *,
    max_depth: int,
    max_fields: int,
    max_list_limit: int | None = None,
) -> list[type[ValidationRule]]:
    """Return validation-rule classes parameterized for the given limits.

    The rules raise ``GraphQLError`` with ``extensions.code`` set to one of
    ``MAX_DEPTH_CODE`` / ``MAX_FIELDS_CODE`` / ``MAX_LIST_LIMIT_CODE``. They
    are pure AST checks — no schema lookup needed — so they run before
    type-validation rules and short-circuit cheaply.

    ``max_list_limit`` (when set) caps integer literals on ``limit:`` /
    ``first:`` arguments. This addresses the analytics-cost axis that depth
    and field count don't: a single-field query selecting a million rows is
    far more expensive than a 50-field projection.
    """

    class QueryShapeRule(ValidationRule):
        """Enforces depth + leaf-field-count limits per operation/fragment."""

        def __init__(self, context: ValidationContext) -> None:
            super().__init__(context)

        def enter_operation_definition(
            self, node: OperationDefinitionNode, *_: object
        ) -> None:
            self._check(node, node.selection_set)

        def enter_fragment_definition(
            self, node: FragmentDefinitionNode, *_: object
        ) -> None:
            self._check(node, node.selection_set)

        def _check(self, node: object, sel: SelectionSetNode | None) -> None:
            if sel is None:
                return
            # Strip top-level introspection (``{ __schema { ... } }``) so an
            # introspection-only request isn't rejected on its inner depth.
            data_selections = [
                s
                for s in sel.selections
                if not (isinstance(s, FieldNode) and _is_introspection(s.name.value))
            ]
            if not data_selections:
                return
            wrapped = SelectionSetNode(selections=tuple(data_selections))
            depth, fields = _walk(wrapped, depth=1)
            if depth > max_depth:
                self.report_error(
                    GraphQLError(
                        f"Query depth {depth} exceeds the limit of {max_depth}",
                        nodes=[node],  # type: ignore[arg-type]
                        extensions={"code": MAX_DEPTH_CODE},
                    )
                )
            if fields > max_fields:
                self.report_error(
                    GraphQLError(
                        f"Query contains {fields} fields which exceeds the "
                        f"limit of {max_fields}",
                        nodes=[node],  # type: ignore[arg-type]
                        extensions={"code": MAX_FIELDS_CODE},
                    )
                )

    rules: list[type[ValidationRule]] = [QueryShapeRule]

    if max_list_limit is not None:
        cap = max_list_limit

        class ListLimitRule(ValidationRule):
            """Caps integer literals on ``limit:`` / ``first:`` arguments.

            Variables bypass this check by design — they are bound at
            execution and the rule runs at validation; resolvers are
            expected to apply runtime caps when accepting variables for
            pagination args.
            """

            def enter_field(self, node: FieldNode, *_: object) -> None:
                for arg in node.arguments or ():
                    if arg.name.value not in _LIST_LIMIT_ARGS:
                        continue
                    if not isinstance(arg.value, IntValueNode):
                        continue
                    val = int(arg.value.value)
                    if val > cap:
                        self.report_error(
                            GraphQLError(
                                f"Argument '{arg.name.value}: {val}' exceeds "
                                f"the maximum of {cap}",
                                nodes=[arg],
                                extensions={"code": MAX_LIST_LIMIT_CODE},
                            )
                        )

        rules.append(ListLimitRule)

    return rules
