"""Schema-graph adjacency for MCP relationship tools.

``list_tables`` and ``describe_table`` route through GraphQL ``_tables`` /
``_sdl(tables)`` instead — this module covers only graph traversal
(``find_path``) which has no GraphQL equivalent. Adjacency is derived
from the ``TableRegistry`` ``relation`` fields, i.e. the same view the
API serves. No live warehouse access.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from dbt_graphql.schema.models import TableRegistry


@dataclass
class JoinStep:
    from_table: str
    from_column: str
    to_table: str
    to_column: str


@dataclass
class JoinPath:
    steps: list[JoinStep] = field(default_factory=list)

    @property
    def length(self) -> int:
        return len(self.steps)


class SchemaDiscovery:
    """Graph adjacency over the ``TableRegistry`` for path-finding tools."""

    def __init__(self, registry: TableRegistry) -> None:
        self._registry = registry

        # Build adjacency from registry — outgoing edges live on each
        # column's ``relation``; incoming edges are the reverse.
        self._adj: dict[str, list[tuple[str, str, str]]] = {}
        for tdef in self._registry:
            for col in tdef.columns:
                rel = col.relation
                if rel is None or not rel.target_model:
                    continue
                from_col = rel.from_columns[0] if rel.from_columns else col.name
                to_col = (
                    rel.to_columns[0] if rel.to_columns else rel.target_column or ""
                )
                self._adj.setdefault(tdef.name, []).append(
                    (from_col, rel.target_model, to_col)
                )
                self._adj.setdefault(rel.target_model, []).append(
                    (to_col, tdef.name, from_col)
                )

    def find_path(self, from_table: str, to_table: str) -> list[JoinPath]:
        """BFS to find all shortest join paths between two tables.

        Processes nodes level-by-level so multiple shortest paths
        through shared intermediate nodes are all returned.
        """
        if from_table == to_table:
            return [JoinPath()]

        current_level: dict[str, list[list[JoinStep]]] = {from_table: [[]]}
        visited: set[str] = {from_table}
        shortest: list[JoinPath] = []

        while current_level and not shortest:
            next_level: dict[str, list[list[JoinStep]]] = {}
            for current, paths in current_level.items():
                for via_col, neighbor, neighbor_col in self._adj.get(current, []):
                    step = JoinStep(
                        from_table=current,
                        from_column=via_col,
                        to_table=neighbor,
                        to_column=neighbor_col,
                    )
                    for path in paths:
                        new_path = path + [step]
                        if neighbor == to_table:
                            shortest.append(JoinPath(steps=new_path))
                        elif neighbor not in visited:
                            next_level.setdefault(neighbor, []).append(new_path)

            visited.update(next_level.keys())
            current_level = next_level

        return shortest
