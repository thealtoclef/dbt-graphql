from __future__ import annotations


from wren import DataSource as WrenDataSource

from ..models.data_source import map_column_type
from ..models.wren_mdl import WrenColumn


def convert_columns(
    catalog_node,
    manifest_node,
    data_source: WrenDataSource,
    column_to_enum_name: dict[str, str],
    column_to_not_null: dict[str, bool],
) -> list[WrenColumn]:
    """
    Convert catalog + manifest column information into WrenColumn list.

    Columns are sorted by catalog index, then by name.
    """
    catalog_columns: dict = catalog_node.columns or {}
    manifest_columns: dict = getattr(manifest_node, "columns", None) or {}
    node_unique_id: str = getattr(manifest_node, "unique_id", "")

    result: list[WrenColumn] = []

    for col_name, col_meta in catalog_columns.items():
        col_key = f"{node_unique_id}.{col_name}"

        raw_type = col_meta.type or ""
        wren_type = map_column_type(data_source, raw_type) if raw_type else raw_type

        not_null = column_to_not_null.get(col_key, False)
        enum_name = column_to_enum_name.get(col_key)

        # Collect properties
        props: dict[str, str] = {}
        manifest_col = manifest_columns.get(col_name)
        if manifest_col:
            desc = getattr(manifest_col, "description", None)
            if desc:
                props["description"] = desc
        comment = col_meta.comment
        if comment:
            props["comment"] = comment
        if enum_name:
            props["enumDefinition"] = enum_name

        result.append(
            WrenColumn(
                name=col_name,
                type=wren_type,
                notNull=not_null,
                properties=props if props else None,
            )
        )

    # Sort: primary by index (catalog), secondary by name
    def sort_key(col: WrenColumn) -> tuple:
        cat_col = catalog_columns.get(col.name)
        idx = cat_col.index if cat_col and cat_col.index is not None else 9999
        return (idx, col.name)

    result.sort(key=sort_key)
    return result
