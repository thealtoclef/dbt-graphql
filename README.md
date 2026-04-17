# dbt-mdl

Convert dbt project artifacts to model definition formats.

## Quickstart

```sh
uvx git+https://github.com/thealtoclef/dbt-mdl.git all --profiles profiles.yml --catalog target/catalog.json --manifest target/manifest.json
```

## CLI Reference

```
usage: dbt-mdl [-h] --profiles PATH --catalog PATH --manifest PATH
               [--output DIR] [--profile-name NAME] [--target TARGET]
               [--exclude PATTERN]
               {domain,wren,graphjin,all}

positional arguments:
  {domain,wren,graphjin,all}
                        Comma-separated output formats: domain, wren, graphjin, or all.

required arguments:
  --profiles PATH       Path to profiles.yml.
  --catalog PATH        Path to catalog.json.
  --manifest PATH       Path to manifest.json.

optional arguments:
  --output DIR          Output directory (default: current directory).
  --profile-name NAME   dbt profile name (default: first profile in profiles.yml).
  --target TARGET        dbt target within the profile.
  --exclude PATTERN     Regex pattern to exclude models (may be repeated).
```

## Output Formats

| Format    | Output Files | Description |
|-----------|-------------|-------------|
| `wren`    | `mdl.json`, `connection.json` | Wren AI MDL manifest |
| `graphjin`| `db.graphql`, `dev.yml` | GraphJin SDL schema + config |
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
