from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .graphql.formatter import format_graphql
from .wren.formatter import format_mdl
from .pipeline import extract_project


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="dbt-mdl",
        description="Convert dbt artifacts to model definition formats.",
    )
    subparsers = parser.add_subparsers(dest="command")

    # ── generate (default when no subcommand) ──────────────────────────
    gen = subparsers.add_parser("generate", help="Generate output files from dbt artifacts.")
    _add_generate_args(gen)

    # ── serve ──────────────────────────────────────────────────────────
    srv = subparsers.add_parser("serve", help="Serve a GraphQL API from a db.graphql file.")
    _add_serve_args(srv)

    args = parser.parse_args(argv)

    # Backwards-compat: if no subcommand given, treat as bare `dbt-mdl <format> ...`
    if args.command is None:
        _run_generate(args, parser)
    elif args.command == "generate":
        _run_generate(args, parser)
    elif args.command == "serve":
        _run_serve(args)


# ---------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------


def _add_generate_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "format",
        nargs="?",
        help="Comma-separated output formats: wren, graphql, or all.",
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


def _run_generate(args, parser: argparse.ArgumentParser) -> None:
    if not args.format:
        parser.print_help()
        sys.exit(1)

    if not args.catalog or not args.manifest:
        print("Error: --catalog and --manifest are required for generation.", file=sys.stderr)
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
        requested = {f.strip() for f in args.format.split(",")}
        valid = {"wren", "graphql", "all"}
        unknown = requested - valid
        if unknown:
            print(
                f"Error: unknown format(s): {', '.join(sorted(unknown))}",
                file=sys.stderr,
            )
            sys.exit(1)

        formats = valid - {"all"} if "all" in requested else requested

        _write_lineage(project, output_dir)

        if "wren" in formats:
            _write_wren(project, output_dir)
        if "graphql" in formats:
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


def _write_wren(project, output_dir: Path) -> None:
    result = format_mdl(project)

    mdl_path = output_dir / "mdl.json"
    mdl_path.write_text(
        result.manifest.model_dump_json(by_alias=True, exclude_none=True, indent=2)
    )
    print(f"mdl.json              -> {mdl_path}")


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
    from .graphql import serve
    from .graphql.connection import load_db_config

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
