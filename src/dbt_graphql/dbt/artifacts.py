from __future__ import annotations

import json
from typing import Union

import fsspec
from dbt_artifacts_parser.parser import (
    CatalogV1,
    ManifestV1,
    ManifestV2,
    ManifestV3,
    ManifestV4,
    ManifestV5,
    ManifestV6,
    ManifestV7,
    ManifestV8,
    ManifestV9,
    ManifestV10,
    ManifestV11,
    ManifestV12,
    parse_catalog,
    parse_manifest,
)

DbtCatalog = CatalogV1
DbtManifest = Union[
    ManifestV1,
    ManifestV2,
    ManifestV3,
    ManifestV4,
    ManifestV5,
    ManifestV6,
    ManifestV7,
    ManifestV8,
    ManifestV9,
    ManifestV10,
    ManifestV11,
    ManifestV12,
]


def _read_json(uri: str) -> dict:
    with fsspec.open(uri, "rb") as f:
        return json.load(f)


def load_catalog(uri: str) -> DbtCatalog:
    return parse_catalog(_read_json(uri))


def load_manifest(uri: str) -> DbtManifest:
    return parse_manifest(_read_json(uri))
