from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .formatter import format_graphql
from .pipeline import extract_project


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="dbt-graphql",
        description="Convert dbt artifacts to GraphQL schema and serve a SQL-backed GraphQL API.",
    )
    subparsers = parser.add_subparsers(dest="command")

    gen = subparsers.add_parser(
        "generate", help="Generate output files from dbt artifacts."
    )
    _add_generate_args(gen)

    srv = subparsers.add_parser(
        "serve", help="Serve a GraphQL API from a db.graphql file."
    )
    _add_serve_args(srv)

    mcp_parser = subparsers.add_parser(
        "mcp", help="Start an MCP server for schema discovery by LLM agents."
    )
    _add_mcp_args(mcp_parser)

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        sys.exit(0)
    elif args.command == "generate":
        _run_generate(args)
    elif args.command == "serve":
        _run_serve(args)
    elif args.command == "mcp":
        _run_mcp(args)


# ---------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------


def _add_generate_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--format",
        required=True,
        choices=["graphql"],
        metavar="FMT",
        help="Output format: graphql.",
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        metavar="PATH",
        help="Path to catalog.json.",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
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


def _run_generate(args) -> None:
    if not args.catalog or not args.manifest:
        print(
            "Error: --catalog and --manifest are required for generation.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        project = extract_project(
            catalog_path=args.catalog,
            manifest_path=args.manifest,
            exclude_patterns=args.exclude,
        )
    except (FileNotFoundError, ValueError, KeyError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    output_dir: Path = args.output
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        _write_lineage(project, output_dir)
        _write_graphql(project, output_dir)
    except (ValueError, KeyError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


def _write_lineage(project, output_dir: Path) -> None:
    lineage = project.build_lineage_schema()
    if lineage.table_lineage or lineage.column_lineage:
        lineage_path = output_dir / "lineage.json"
        lineage_path.write_text(lineage.model_dump_json(by_alias=True, indent=2))
        print(f"lineage.json          -> {lineage_path}")


def _write_graphql(project, output_dir: Path) -> None:
    gj = format_graphql(project)

    db_graphql_path = output_dir / "db.graphql"
    db_graphql_path.write_text(gj.db_graphql)
    print(f"db.graphql            -> {db_graphql_path}")


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------


def _add_serve_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--db-graphql",
        type=Path,
        required=True,
        metavar="PATH",
        help="Path to db.graphql SDL file.",
    )
    parser.add_argument(
        "--db-url",
        type=str,
        metavar="URL",
        help="SQLAlchemy async connection URL (e.g. mysql+aiomysql://user:pass@host/db).",
    )
    parser.add_argument(
        "--db-config",
        type=Path,
        metavar="PATH",
        help="Path to db.yml config file (alternative to --db-url).",
    )
    parser.add_argument(
        "--host",
        type=str,
        default="0.0.0.0",
        help="Bind host (default: 0.0.0.0).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Bind port (default: 8080).",
    )


def _run_serve(args) -> None:
    from .serve import serve
    from .compiler.connection import load_db_config

    if not args.db_url and not args.db_config:
        print("Error: provide either --db-url or --db-config.", file=sys.stderr)
        sys.exit(1)

    config = None
    if args.db_config:
        config = load_db_config(args.db_config)

    serve(
        db_graphql_path=args.db_graphql,
        db_url=args.db_url,
        config=config,
        host=args.host,
        port=args.port,
    )


# ---------------------------------------------------------------------------
# mcp
# ---------------------------------------------------------------------------


def _add_mcp_args(parser: argparse.ArgumentParser) -> None:
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
        "--db-url",
        type=str,
        metavar="URL",
        help="SQLAlchemy async connection URL for live enrichment (optional).",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=None,
        metavar="PATTERN",
        help="Regex pattern to exclude models.",
    )


def _run_mcp(args) -> None:
    from .mcp.server import serve_mcp

    try:
        project = extract_project(
            catalog_path=args.catalog,
            manifest_path=args.manifest,
            exclude_patterns=args.exclude,
        )
    except (FileNotFoundError, ValueError, KeyError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    db = None
    if args.db_url:
        from .compiler.connection import DatabaseManager

        db = DatabaseManager(db_url=args.db_url)

    serve_mcp(project, db=db)
