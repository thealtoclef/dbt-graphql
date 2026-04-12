from wren_dbt_converter.parsers.profiles_parser import (
    analyze_dbt_profiles,
    find_profiles_file,
)
from wren_dbt_converter.models.profiles import DbtProfiles


def test_parse_fixture_profiles(profiles_path):
    profiles = analyze_dbt_profiles(profiles_path)
    assert isinstance(profiles, DbtProfiles)
    assert "jaffle_shop" in profiles.profiles
    profile = profiles.profiles["jaffle_shop"]
    assert profile.target == "dev"
    assert "dev" in profile.outputs
    conn = profile.outputs["dev"]
    assert conn.type == "duckdb"
    assert conn.path == "jaffle_shop.duckdb"


def test_parse_multi_profile(tmp_path):
    content = """
project_a:
  target: prod
  outputs:
    prod:
      type: postgres
      host: pg-host
      port: 5432
      database: prod_db
      user: admin
      password: secret

project_b:
  target: dev
  outputs:
    dev:
      type: bigquery
      method: service-account-json
      project: my-gcp-project
      dataset: my_dataset
      keyfile_json: '{"type": "service_account"}'
"""
    path = tmp_path / "profiles.yml"
    path.write_text(content)
    profiles = analyze_dbt_profiles(path)

    assert "project_a" in profiles.profiles
    assert "project_b" in profiles.profiles

    pg = profiles.profiles["project_a"].outputs["prod"]
    assert pg.type == "postgres"
    assert pg.host == "pg-host"
    assert pg.port == 5432
    assert pg.database == "prod_db"

    bq = profiles.profiles["project_b"].outputs["dev"]
    assert bq.type == "bigquery"
    assert bq.get_extra("keyfile_json") == '{"type": "service_account"}'


def test_config_key_ignored(tmp_path):
    content = """
config:
  send_anonymous_usage_stats: false

my_project:
  target: dev
  outputs:
    dev:
      type: postgres
      host: localhost
      database: mydb
      user: user
"""
    path = tmp_path / "profiles.yml"
    path.write_text(content)
    profiles = analyze_dbt_profiles(path)
    assert "config" not in profiles.profiles
    assert "my_project" in profiles.profiles


def test_find_profiles_file_in_project(tmp_path):
    (tmp_path / "profiles.yml").write_text("test: {target: dev, outputs: {}}")
    found = find_profiles_file(tmp_path)
    assert found == tmp_path / "profiles.yml"


def test_find_profiles_file_missing(tmp_path):
    found = find_profiles_file(tmp_path)
    assert found is None


def test_port_as_string_coerced(tmp_path):
    content = """
pg_project:
  target: dev
  outputs:
    dev:
      type: postgres
      host: localhost
      port: "5432"
      database: mydb
      user: admin
"""
    path = tmp_path / "profiles.yml"
    path.write_text(content)
    profiles = analyze_dbt_profiles(path)
    conn = profiles.profiles["pg_project"].outputs["dev"]
    assert conn.port == 5432
    assert isinstance(conn.port, int)


def test_extra_fields_captured(tmp_path):
    content = """
my_project:
  target: dev
  outputs:
    dev:
      type: bigquery
      method: service-account-json
      project: proj
      dataset: ds
      keyfile_json: '{"type":"service_account"}'
      threads: 4
"""
    path = tmp_path / "profiles.yml"
    path.write_text(content)
    profiles = analyze_dbt_profiles(path)
    conn = profiles.profiles["my_project"].outputs["dev"]
    assert conn.get_extra("keyfile_json") == '{"type":"service_account"}'
    assert conn.get_extra("threads") == 4
