"""Unit tests for config loading and pydantic-settings env var overrides."""

import shutil

import pytest
from pathlib import Path

from dbt_graphql.config import LogsConfig, MetricsConfig, TracesConfig, load_config


def _write_config(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "config.yml"
    p.write_text(content)
    return p


_FIXTURES = Path(__file__).parent.parent / "fixtures" / "dbt-artifacts"

_MINIMAL_YAML = f"""\
dbt:
  catalog: {_FIXTURES / "catalog.json"}
  manifest: {_FIXTURES / "manifest.json"}
db:
  type: postgres
  host: localhost
  dbname: mydb
"""


class TestLoadConfig:
    def test_reads_db_fields(self, tmp_path):
        cfg = load_config(_write_config(tmp_path, _MINIMAL_YAML))
        assert cfg.db is not None
        assert cfg.db.type == "postgres"
        assert cfg.db.host == "localhost"
        assert cfg.db.dbname == "mydb"

    def test_enrichment_defaults_when_omitted(self, tmp_path):
        cfg = load_config(_write_config(tmp_path, _MINIMAL_YAML))
        assert cfg.enrichment.budget == 20
        assert cfg.enrichment.distinct_values_limit == 50
        assert cfg.enrichment.distinct_values_max_cardinality == 500

    def test_enrichment_values_from_yaml(self, tmp_path):
        yaml = _MINIMAL_YAML + "enrichment:\n  budget: 5\n"
        cfg = load_config(_write_config(tmp_path, yaml))
        assert cfg.enrichment.budget == 5

    def test_non_dict_yaml_raises_value_error(self, tmp_path):
        p = _write_config(tmp_path, "- item1\n- item2\n")
        with pytest.raises(ValueError, match="must be a YAML mapping"):
            load_config(p)

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_config(tmp_path / "nonexistent.yml")


class TestEnvVarOverrides:
    def test_env_overrides_enrichment_budget(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DBT_GRAPHQL__ENRICHMENT__BUDGET", "7")
        yaml = _MINIMAL_YAML + "enrichment:\n  budget: 100\n"
        cfg = load_config(_write_config(tmp_path, yaml))
        assert cfg.enrichment.budget == 7

    def test_env_overrides_db_host(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DBT_GRAPHQL__DB__HOST", "envhost")
        cfg = load_config(_write_config(tmp_path, _MINIMAL_YAML))
        assert cfg.db is not None
        assert cfg.db.host == "envhost"

    def test_env_overrides_monitoring_log_level(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DBT_GRAPHQL__MONITORING__LOGS__LEVEL", "DEBUG")
        cfg = load_config(_write_config(tmp_path, _MINIMAL_YAML))
        assert cfg.monitoring.logs.level == "DEBUG"

    def test_env_does_not_bleed_between_tests(self, tmp_path):
        # Env vars from other tests must not carry over (monkeypatch is per-test).
        cfg = load_config(_write_config(tmp_path, _MINIMAL_YAML))
        assert cfg.enrichment.budget == 20

    def test_yaml_value_wins_when_no_env_var(self, tmp_path):
        yaml = _MINIMAL_YAML + "enrichment:\n  budget: 42\n"
        cfg = load_config(_write_config(tmp_path, yaml))
        assert cfg.enrichment.budget == 42

    def test_monitoring_traces_endpoint_from_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv(
            "DBT_GRAPHQL__MONITORING__TRACES__ENDPOINT", "http://col:4317"
        )
        monkeypatch.setenv("DBT_GRAPHQL__MONITORING__TRACES__PROTOCOL", "grpc")
        cfg = load_config(_write_config(tmp_path, _MINIMAL_YAML))
        assert cfg.monitoring.traces.endpoint == "http://col:4317"
        assert cfg.monitoring.traces.protocol == "grpc"

    def test_monitoring_defaults_all_endpoints_none(self, tmp_path):
        cfg = load_config(_write_config(tmp_path, _MINIMAL_YAML))
        assert cfg.monitoring.traces.endpoint is None
        assert cfg.monitoring.metrics.endpoint is None
        assert cfg.monitoring.logs.endpoint is None


class TestProtocolValidation:
    def test_traces_endpoint_without_protocol_raises(self):
        with pytest.raises(ValueError, match="protocol is required"):
            TracesConfig(endpoint="http://collector:4317")

    def test_traces_endpoint_with_protocol_valid(self):
        cfg = TracesConfig(endpoint="http://collector:4317", protocol="grpc")
        assert cfg.protocol == "grpc"

    def test_traces_no_endpoint_no_protocol_valid(self):
        cfg = TracesConfig()
        assert cfg.endpoint is None
        assert cfg.protocol is None

    def test_metrics_endpoint_without_protocol_raises(self):
        with pytest.raises(ValueError, match="protocol is required"):
            MetricsConfig(endpoint="http://collector:4317")

    def test_logs_endpoint_without_protocol_raises(self):
        with pytest.raises(ValueError, match="protocol is required"):
            LogsConfig(endpoint="http://collector:4317")


class TestDbtConfig:
    def test_catalog_and_manifest_fields(self, tmp_path):
        cfg = load_config(_write_config(tmp_path, _MINIMAL_YAML))
        assert cfg.dbt.catalog == _FIXTURES / "catalog.json"
        assert cfg.dbt.manifest == _FIXTURES / "manifest.json"

    def test_exclude_defaults_to_empty_list(self, tmp_path):
        cfg = load_config(_write_config(tmp_path, _MINIMAL_YAML))
        assert cfg.dbt.exclude == []

    def test_exclude_list_from_yaml(self, tmp_path):
        yaml = (
            f"dbt:\n"
            f"  catalog: {_FIXTURES / 'catalog.json'}\n"
            f"  manifest: {_FIXTURES / 'manifest.json'}\n"
            f"  exclude:\n    - '^stg_'\n    - '^int_'\n"
        )
        cfg = load_config(_write_config(tmp_path, yaml))
        assert "^stg_" in cfg.dbt.exclude
        assert "^int_" in cfg.dbt.exclude

    def test_relative_paths_resolved_from_config_dir(self, tmp_path):
        catalog_dst = tmp_path / "catalog.json"
        manifest_dst = tmp_path / "manifest.json"
        shutil.copy(_FIXTURES / "catalog.json", catalog_dst)
        shutil.copy(_FIXTURES / "manifest.json", manifest_dst)

        cfg_path = _write_config(
            tmp_path, "dbt:\n  catalog: catalog.json\n  manifest: manifest.json\n"
        )
        cfg = load_config(cfg_path)
        assert cfg.dbt.catalog == catalog_dst
        assert cfg.dbt.manifest == manifest_dst

    def test_absolute_paths_unchanged(self, tmp_path):
        cfg = load_config(_write_config(tmp_path, _MINIMAL_YAML))
        assert cfg.dbt.catalog.is_absolute()
        assert cfg.dbt.manifest.is_absolute()


class TestServeConfig:
    def test_serve_defaults(self, tmp_path):
        yaml = _MINIMAL_YAML + "serve:\n  host: 0.0.0.0\n  port: 8080\n"
        cfg = load_config(_write_config(tmp_path, yaml))
        assert cfg.serve is not None
        assert cfg.serve.host == "0.0.0.0"
        assert cfg.serve.port == 8080
        assert cfg.serve.mcp_enabled is False
        assert cfg.serve.graphql_introspection is False

    def test_graphql_introspection_opt_in(self, tmp_path):
        yaml = (
            _MINIMAL_YAML
            + "serve:\n  host: 0.0.0.0\n  port: 8080\n"
            + "  graphql_introspection: true\n"
        )
        cfg = load_config(_write_config(tmp_path, yaml))
        assert cfg.serve is not None
        assert cfg.serve.graphql_introspection is True

    def test_mcp_enabled_opt_in(self, tmp_path):
        yaml = (
            _MINIMAL_YAML
            + "serve:\n  host: 0.0.0.0\n  port: 8080\n"
            + "  mcp_enabled: true\n"
        )
        cfg = load_config(_write_config(tmp_path, yaml))
        assert cfg.serve is not None
        assert cfg.serve.mcp_enabled is True

    def test_db_optional(self, tmp_path):
        yaml = (
            f"dbt:\n  catalog: {_FIXTURES / 'catalog.json'}\n"
            f"  manifest: {_FIXTURES / 'manifest.json'}\n"
        )
        cfg = load_config(_write_config(tmp_path, yaml))
        assert cfg.db is None


class TestJWTConfigKeyURLValidation:
    """``key_url`` is for a single static PEM/JWK (fetched once, no refresh).
    A JWKS endpoint must use ``jwks_url`` to get rotation. Reject ``key_url``
    values that look like JWKS endpoints — the silent failure mode (key set
    rotation breaks) is too easy to ship by accident."""

    def test_key_url_jwks_path_rejected(self, tmp_path):
        yaml = _MINIMAL_YAML + (
            "security:\n"
            "  enabled: true\n"
            "  jwt:\n"
            "    algorithms: [RS256]\n"
            "    key_url: https://issuer.example/.well-known/jwks.json\n"
        )
        with pytest.raises(ValueError, match="JWKS"):
            load_config(_write_config(tmp_path, yaml))

    def test_key_url_with_jwks_substring_rejected(self, tmp_path):
        yaml = _MINIMAL_YAML + (
            "security:\n"
            "  enabled: true\n"
            "  jwt:\n"
            "    algorithms: [RS256]\n"
            "    key_url: https://internal/jwks/v1\n"
        )
        with pytest.raises(ValueError, match="JWKS"):
            load_config(_write_config(tmp_path, yaml))

    def test_key_url_pem_path_accepted(self, tmp_path):
        yaml = _MINIMAL_YAML + (
            "security:\n"
            "  enabled: true\n"
            "  jwt:\n"
            "    algorithms: [RS256]\n"
            "    key_url: https://internal/keys/dbt-graphql.pem\n"
        )
        cfg = load_config(_write_config(tmp_path, yaml))
        assert cfg.security.jwt.key_url is not None


class TestConfigExampleSchemaGuard:
    """The shipped ``config.example.yml`` is the operator's reference. If
    ``AppConfig`` evolves but the example doesn't, operators copy a stale
    file. Load the example against the live schema so drift fails CI."""

    def test_config_example_yml_loads(self):
        repo_root = Path(__file__).resolve().parents[2]
        example = repo_root / "config.example.yml"
        assert example.exists(), f"config.example.yml missing at {example}"
        cfg = load_config(example)
        # Sanity: the example demonstrates a serve config so these must parse.
        assert cfg.serve is not None
        assert cfg.db is not None
