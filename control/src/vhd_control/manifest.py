"""Ingress (I0): the manifest and the secrets file, read and checked against schema/ (G6, G18).

Nothing past this module reads either file: it hands on Settings, with every default
taken from the manifest schema.
"""

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from vhd_control.schemas import Schemas

MANIFEST_FILE = "manifest.toml"
SECRETS_FILE = "secrets.toml"
MANIFEST_SCHEMA = "schema/manifest.schema.json"
SECRETS_SCHEMA = "schema/secrets.schema.json"


class InvalidSettings(Exception):
    """The manifest or secrets file cannot be read or breaks its schema."""


@dataclass(frozen=True)
class World:
    name: str
    port: int
    password: str | None


@dataclass(frozen=True)
class Settings:
    """Everything the manifest and secrets say, defaults filled in. Worlds sort by name."""

    worlds: tuple[World, ...]
    web_map_port: int


def load(install: Path, schemas: Schemas) -> Settings:
    """The install's settings. The secrets file is optional; the manifest is not."""
    manifest = _read(install / MANIFEST_FILE, MANIFEST_SCHEMA, schemas)
    secrets_path = install / SECRETS_FILE
    secrets = _read(secrets_path, SECRETS_SCHEMA, schemas) if secrets_path.exists() else {}
    schema = schemas.document(MANIFEST_SCHEMA)
    default_port = cast(int, schema["$defs"]["world"]["properties"]["port"]["default"])
    default_web_map_port = cast(int, schema["properties"]["web_map"]["properties"]["port"]["default"])

    world_tables = cast(dict[str, dict[str, Any]], manifest["world"])
    world_secrets = cast(dict[str, dict[str, Any]], secrets.get("world", {}))
    unknown = sorted(set(world_secrets) - set(world_tables))
    if unknown:
        raise InvalidSettings(f"{SECRETS_FILE} names world {unknown[0]!r}, which {MANIFEST_FILE} does not")
    worlds = tuple(
        World(
            name=name,
            port=cast(int, world_tables[name].get("port", default_port)),
            password=cast(str | None, world_secrets.get(name, {}).get("password")),
        )
        for name in sorted(world_tables)
    )
    web_map = cast(dict[str, Any], manifest.get("web_map", {}))
    return Settings(worlds=worlds, web_map_port=cast(int, web_map.get("port", default_web_map_port)))


def _read(path: Path, schema: str, schemas: Schemas) -> dict[str, Any]:
    try:
        document = tomllib.loads(path.read_text())
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise InvalidSettings(f"{path.name}: {error}") from error
    problem = schemas.problem(schema, document)
    if problem is not None:
        raise InvalidSettings(f"{path.name} {problem}")
    return document
