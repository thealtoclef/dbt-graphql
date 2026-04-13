from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import build_manifest


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="wren-dbt-converter",
        description="Convert a dbt project to Wren MDL and connection info JSON files.",
    )
    parser.add_argument(
        "project_path",
        type=Path,
        help="Path to the dbt project root (must contain dbt_project.yml and profiles.yml).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("."),
        metavar="DIR",
        help="Output directory for generated files (default: current directory).",
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=None,
        metavar="PATH",
        help="Path to catalog.json (default: <project_path>/target/catalog.json).",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        metavar="PATH",
        help="Path to manifest.json (default: <project_path>/target/manifest.json).",
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
        result = build_manifest(
            project_path=args.project_path,
            profile_name=args.profile_name,
            target=args.target,
            exclude_patterns=args.exclude,
            catalog_path=args.catalog,
            manifest_path=args.manifest,
        )
    except (FileNotFoundError, ValueError, KeyError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    output_dir: Path = args.output
    output_dir.mkdir(parents=True, exist_ok=True)

    mdl_path = output_dir / "mdl.json"
    mdl_path.write_text(json.dumps(result.manifest.to_camel_dict(), indent=2))

    connection_path = output_dir / "connection.json"
    connection_path.write_text(
        json.dumps(
            {"dataSource": str(result.data_source), "connection": result.connection_info},
            indent=2,
        )
    )

    schema_description_path = output_dir / "schema_description.md"
    schema_description_path.write_text(result.schema_description)

    print(f"mdl.json                → {mdl_path}")
    print(f"connection.json         → {connection_path}")
    print(f"schema_description.md   → {schema_description_path}")
