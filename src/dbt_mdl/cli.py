from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .graphjin.formatter import format_graphjin
from .wren.formatter import format_mdl
from .pipeline import extract_project


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="dbt-mdl",
        description="Convert dbt artifacts to model definition formats.",
    )
    parser.add_argument(
        "format",
        help="Comma-separated output formats: domain, wren, graphjin, or all.",
    )
    parser.add_argument(
        "--profiles",
        type=Path,
        required=True,
        metavar="PATH",
        help="Path to profiles.yml.",
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        required=True,
        metavar="PATH",
        help="Path to catalog.json.",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        required=True,
        metavar="PATH",
        help="Path to manifest.json.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("."),
        metavar="DIR",
        help="Output directory for generated files (default: current directory).",
    )
    parser.add_argument(
        "--profile-name",
        default=None,
        metavar="NAME",
        help="dbt profile name to use (default: first profile in profiles.yml).",
    )
    parser.add_argument(
        "--target",
        default=None,
        metavar="TARGET",
        help="dbt target within the profile (default: profile's default target).",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=None,
        metavar="PATTERN",
        help=(
            "Regex pattern matched against model names; matching models are excluded. "
            "May be repeated to add multiple patterns (OR logic). "
            "Example: --exclude '^stg_' --exclude '^int_'"
        ),
    )

    args = parser.parse_args(argv)

    try:
        project = extract_project(
            profiles_path=args.profiles,
            catalog_path=args.catalog,
            manifest_path=args.manifest,
            profile_name=args.profile_name,
            target=args.target,
            exclude_patterns=args.exclude,
        )
    except (FileNotFoundError, ValueError, KeyError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    output_dir: Path = args.output
    output_dir.mkdir(parents=True, exist_ok=True)

    profiles_path = args.profiles

    try:
        requested = {f.strip() for f in args.format.split(",")}
        valid = {"domain", "wren", "graphjin", "all"}
        unknown = requested - valid
        if unknown:
            print(
                f"Error: unknown format(s): {', '.join(sorted(unknown))}",
                file=sys.stderr,
            )
            sys.exit(1)

        formats = valid - {"all"} if "all" in requested else requested

        if "domain" in formats:
            _write_domain(project, output_dir)
        if "wren" in formats:
            _write_wren(project, profiles_path, output_dir)
        if "graphjin" in formats:
            _write_graphjin(project, output_dir)
    except (ValueError, KeyError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


def _write_domain(project, output_dir: Path) -> None:
    lineage = project.build_lineage_schema()
    if lineage.table_lineage or lineage.column_lineage:
        lineage_path = output_dir / "lineage.json"
        lineage_path.write_text(lineage.model_dump_json(by_alias=True, indent=2))
        print(f"lineage.json          -> {lineage_path}")


def _write_wren(project, profiles_path: Path, output_dir: Path) -> None:
    from .wren.connection import build_connection_info

    dbt_home = profiles_path.parent
    try:
        connection_info = build_connection_info(project.connection, dbt_home)
    except ValueError as exc:
        print(f"Warning: {exc}", file=sys.stderr)
        connection_info = {"incomplete": True}

    result = format_mdl(project)

    mdl_path = output_dir / "mdl.json"
    mdl_path.write_text(
        result.manifest.model_dump_json(by_alias=True, exclude_none=True, indent=2)
    )
    print(f"mdl.json              -> {mdl_path}")

    connection_path = output_dir / "connection.json"
    connection_path.write_text(
        json.dumps(
            {
                "dataSource": result.data_source if result.data_source else "",
                "connection": connection_info,
            },
            indent=2,
        )
    )
    print(f"connection.json       -> {connection_path}")


def _write_graphjin(project, output_dir: Path) -> None:
    gj = format_graphjin(project)

    db_graphql_path = output_dir / "db.graphql"
    db_graphql_path.write_text(gj.db_graphql)
    print(f"db.graphql            -> {db_graphql_path}")

    dev_yml_path = output_dir / "dev.yml"
    dev_yml_path.write_text(gj.dev_yml)
    print(f"dev.yml               -> {dev_yml_path}")
