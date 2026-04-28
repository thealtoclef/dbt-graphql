from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .formatter import format_graphql
from .pipeline import extract_project


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="dbt-graphql",
        description=(
            "Convert dbt artifacts to a GraphQL/MCP server. "
            "With --output: write schema files to disk. "
            "Without --output: serve based on config."
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        metavar="PATH",
        help="Path to config.yml (required).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        metavar="DIR",
        help="Write db.graphql + lineage.json to DIR and exit (generate mode).",
    )

    args = parser.parse_args(argv)

    if not args.config:
        parser.print_help()
        sys.exit(0)

    from .config import load_config

    # Operator-input parsing — surface any error as a clean exit, no traceback.
    try:
        config = load_config(args.config)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        project = extract_project(
            catalog_path=config.dbt.catalog,
            manifest_path=config.dbt.manifest,
            exclude_patterns=config.dbt.exclude or None,
        )
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.output:
        _write_artifacts(project, args.output)
        return

    _run_serve(project, config)


# ---------------------------------------------------------------------------
# Generate mode
# ---------------------------------------------------------------------------


def _write_artifacts(project, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    lineage = project.build_lineage_schema()
    if lineage.table_lineage or lineage.column_lineage:
        lineage_path = output_dir / "lineage.json"
        lineage_path.write_text(lineage.model_dump_json(by_alias=True, indent=2))
        print(f"lineage.json -> {lineage_path}")

    gj = format_graphql(project)
    db_graphql_path = output_dir / "db.graphql"
    db_graphql_path.write_text(gj.db_graphql)
    print(f"db.graphql   -> {db_graphql_path}")


# ---------------------------------------------------------------------------
# Serve mode
# ---------------------------------------------------------------------------


def _run_serve(project, config) -> None:
    from .monitoring import configure_monitoring

    configure_monitoring(config.monitoring)

    if config.serve is None:
        print("Error: config.yml must have a 'serve:' section.", file=sys.stderr)
        sys.exit(1)

    if config.db is None:
        print(
            "Error: config.yml must have a 'db:' section for serve mode.",
            file=sys.stderr,
        )
        sys.exit(1)

    from loguru import logger

    if not config.security.enabled:
        logger.warning(
            "starting with security.enabled=false — every request is treated "
            "as anonymous and access policies are NOT enforced. Do not run "
            "this configuration in production unless an upstream proxy "
            "authenticates and authorizes requests."
        )

    from .formatter.graphql import build_registry
    from .graphql.policy import (
        AccessPolicy,
        validate_access_policy_against_registry,
    )
    from .serve import run as _run

    registry = build_registry(project)
    access_policy: AccessPolicy | None = None
    if config.security.enabled and config.security.policies:
        access_policy = AccessPolicy(policies=config.security.policies)
        try:
            validate_access_policy_against_registry(access_policy, registry)
        except Exception as exc:
            print(f"Error in security.policies: {exc}", file=sys.stderr)
            sys.exit(1)

    _run(
        registry=registry,
        config=config,
        project=project,
        access_policy=access_policy,
    )
