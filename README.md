# dbt-mdl

Convert dbt project artifacts to model definition formats.

## Quickstart

```sh
uvx git+https://github.com/thealtoclef/dbt-mdl.git all --catalog catalog.json --manifest manifest.json --output ./output
```

## CLI Reference

```
usage: dbt-mdl [-h] --catalog PATH --manifest PATH [--output DIR] [--exclude PATTERN]
               {domain,wren,graphjin,all}

positional arguments:
  {domain,wren,graphjin,all}
                        Comma-separated output formats: domain, wren, graphjin, or all.

required arguments:
  --catalog PATH        Path to catalog.json.
  --manifest PATH       Path to manifest.json.

optional arguments:
  --output DIR          Output directory (default: current directory).
  --exclude PATTERN     Regex pattern to exclude models (may be repeated).
```

## Output Formats

| Format    | Output Files | Description |
|-----------|-------------|-------------|
| `wren`    | `mdl.json` | Wren AI MDL manifest |
| `graphjin`| `db.graphql` | GraphJin SDL schema |
| `domain`  | `lineage.json` | Domain lineage schema |
| `all`     | All of the above | All formats |

## Development

Generate dbt artifacts first:

```sh
dbt compile
dbt docs generate
```

### Datamodel Codegen
```sh
uvx --from 'datamodel-code-generator[http]' datamodel-codegen --profile 'PROFILE'
```
