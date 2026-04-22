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
        "serve",
        help="Serve one or both interfaces: api (GraphQL HTTP) and/or mcp (stdio).",
    )
    _add_serve_args(srv)

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        sys.exit(0)
    elif args.command == "generate":
        _run_generate(args)
    elif args.command == "serve":
        _run_serve(args)


# ---------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------


def _add_generate_args(parser: argparse.ArgumentParser) -> None:
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

_VALID_TARGETS = {"api", "mcp"}


def _add_serve_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--target",
        default="api",
        metavar="TARGET",
        help=(
            "Comma-separated list of interfaces to serve: api, mcp, or api,mcp. "
            "Default: api."
        ),
    )
    # api args
    parser.add_argument(
        "--db-graphql",
        type=Path,
        metavar="PATH",
        help="Path to db.graphql SDL file (required for target=api).",
    )
    # shared / mcp args
    parser.add_argument(
        "--config",
        type=Path,
        metavar="PATH",
        help="Path to config.yml (required for target=api; optional for mcp).",
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        metavar="PATH",
        help="Path to catalog.json (required for target=mcp).",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        metavar="PATH",
        help="Path to manifest.json (required for target=mcp).",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=None,
        metavar="PATTERN",
        help="Regex pattern to exclude models (mcp only). May be repeated.",
    )


def _parse_targets(raw: str) -> set[str]:
    targets = {t.strip().lower() for t in raw.split(",")}
    unknown = targets - _VALID_TARGETS
    if unknown:
        print(
            f"Error: unknown target(s): {', '.join(sorted(unknown))}. "
            f"Valid: {', '.join(sorted(_VALID_TARGETS))}.",
            file=sys.stderr,
        )
        sys.exit(1)
    return targets


def _run_serve(args) -> None:
    from .config import load_config
    from .monitoring import configure_monitoring

    config = None
    if args.config:
        try:
            config = load_config(args.config)
        except (ValueError, Exception) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)

    if config is not None:
        mon = config.monitoring
        configure_monitoring(
            service_name=mon.service_name,
            exporter=mon.exporter,
            endpoint=mon.endpoint,
            log_level=mon.log_level,
            protocol=mon.protocol,
        )
    else:
        configure_monitoring()

    targets = _parse_targets(args.target)

    # Validate required args per target
    if "api" in targets:
        if not args.db_graphql:
            print("Error: --db-graphql is required for target=api.", file=sys.stderr)
            sys.exit(1)
        if not args.config:
            print("Error: --config is required for target=api.", file=sys.stderr)
            sys.exit(1)
    if "mcp" in targets:
        if not args.catalog or not args.manifest:
            print(
                "Error: --catalog and --manifest are required for target=mcp.",
                file=sys.stderr,
            )
            sys.exit(1)

    # Build shared db connection for mcp (if config provided)
    db = None
    if "mcp" in targets and config is not None:
        from .compiler.connection import DatabaseManager

        db = DatabaseManager(config=config.db)

    # Start MCP in a daemon thread when serving both, so the API can block main
    if "mcp" in targets:
        try:
            project = extract_project(
                catalog_path=args.catalog,
                manifest_path=args.manifest,
                exclude_patterns=args.exclude,
            )
        except (FileNotFoundError, ValueError, KeyError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)

        if targets == {"mcp"}:
            # MCP only — run directly in main thread
            from .mcp.server import serve_mcp

            serve_mcp(project, db=db)
            return

        # api + mcp — start MCP in a daemon thread
        import threading
        from .mcp.server import serve_mcp

        t = threading.Thread(
            target=serve_mcp, args=(project,), kwargs={"db": db}, daemon=True
        )
        t.start()

    if "api" in targets:
        from .api import serve

        assert config is not None  # guaranteed by validation above

        if config.serve is None:
            print("Error: config.yml must have a 'serve:' section.", file=sys.stderr)
            sys.exit(1)

        serve(
            db_graphql_path=args.db_graphql,
            config=config.db,
            host=config.serve.host,
            port=config.serve.port,
        )
