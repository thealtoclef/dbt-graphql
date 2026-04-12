from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml

from ..models.profiles import DbtConnection, DbtProfile, DbtProfiles


def find_profiles_file(project_path: Path | str) -> Optional[Path]:
    """Return profiles.yml from the project root, or None if not found."""
    candidate = Path(project_path) / "profiles.yml"
    return candidate if candidate.exists() else None


def analyze_dbt_profiles(path: Path | str) -> DbtProfiles:
    """Parse a profiles.yml file into a DbtProfiles model."""
    path = Path(path)
    raw: dict = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"profiles.yml at {path} is not a YAML mapping")

    profiles: dict[str, DbtProfile] = {}
    for key, value in raw.items():
        if key == "config":
            continue
        if not isinstance(value, dict):
            continue
        profile = _parse_profile(key, value)
        profiles[key] = profile

    return DbtProfiles(profiles=profiles)


def _parse_profile(name: str, data: dict) -> DbtProfile:
    target = data.get("target", "")
    outputs_raw = data.get("outputs", {})
    outputs: dict[str, DbtConnection] = {}
    for output_name, conn_data in outputs_raw.items():
        if not isinstance(conn_data, dict):
            continue
        outputs[output_name] = _parse_connection(conn_data)
    return DbtProfile(target=target, outputs=outputs)


_KNOWN_FIELDS = {
    "type",
    "host",
    "server",
    "port",
    "user",
    "password",
    "database",
    "dbname",
    "schema",
    "project",
    "dataset",
    "keyfile",
    "method",
    "account",
    "warehouse",
    "role",
    "keepalive",
    "search_path",
    "sslmode",
    "ssl_disable",
    "path",
}


def _parse_connection(data: dict) -> DbtConnection:
    # Separate known fields from extras
    known: dict = {}
    extras: dict = {}

    for k, v in data.items():
        if k in _KNOWN_FIELDS:
            # Map "schema" -> "schema_" to avoid Pydantic name conflict
            field_key = "schema_" if k == "schema" else k
            known[field_key] = v
        else:
            extras[k] = v

    # port may be string in some YAML files — coerce to int
    if "port" in known and isinstance(known["port"], str):
        try:
            known["port"] = int(known["port"])
        except ValueError:
            known.pop("port")

    return DbtConnection(**known, **extras)
