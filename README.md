# dbt-graphql

Turn a dbt project into a typed GraphQL schema, a SQL-backed GraphQL API, and an MCP surface for LLM agents — without authoring a second modeling layer. dbt-graphql reads `catalog.json` and `manifest.json` and derives everything from what your analytics team already maintains.

## Installation

```bash
pip install dbt-graphql             # generate only
pip install dbt-graphql[api]        # + GraphQL API server
pip install dbt-graphql[mcp]        # + MCP server
pip install dbt-graphql[postgres]   # warehouse drivers
pip install dbt-graphql[mysql]
```

## Quick start

**1. Generate the schema**

```bash
dbt-graphql generate \
  --catalog target/catalog.json \
  --manifest target/manifest.json \
  --output output/
```

Produces `output/db.graphql` and `output/lineage.json`.

**2. Serve the GraphQL API**

```bash
dbt-graphql serve \
  --target api \
  --db-graphql output/db.graphql \
  --config config.yml
```

Playground at `http://localhost:8080/graphql`. See [`config.example.yml`](config.example.yml) for the config format.

**3. Start the MCP server**

```bash
dbt-graphql serve \
  --target mcp \
  --catalog target/catalog.json \
  --manifest target/manifest.json \
  --config config.yml
```

Starts an MCP stdio server for Claude Desktop, Cline, and other MCP clients.

**4. Serve both at once**

```bash
dbt-graphql serve \
  --target api,mcp \
  --db-graphql output/db.graphql \
  --catalog target/catalog.json \
  --manifest target/manifest.json \
  --config config.yml
```

## Documentation

- [**Architecture & Design**](docs/architecture.md) — pipeline, design principles, and landscape comparison.
- [**Schema Synthesis**](docs/schema-synthesis.md) — dbt extraction, IR, formatter, and lineage in depth.
- [**API & Compiler**](docs/api.md) — GraphQL→SQL compiler and HTTP API layer.
- [**MCP Server**](docs/mcp.md) — tools, discovery engine, and observability.
- [**Roadmap**](ROADMAP.md)

## License

MIT.
