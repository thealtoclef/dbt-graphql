"""Unit tests for config loading and pydantic-settings env var overrides."""

import pytest
from pathlib import Path

from dbt_graphql.config import LogsConfig, MetricsConfig, TracesConfig, load_config


def _write_config(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "config.yml"
    p.write_text(content)
    return p


_MINIMAL_YAML = """\
db:
  type: postgres
  host: localhost
  dbname: mydb
"""


class TestLoadConfig:
    def test_reads_db_fields(self, tmp_path):
        cfg = load_config(_write_config(tmp_path, _MINIMAL_YAML))
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
