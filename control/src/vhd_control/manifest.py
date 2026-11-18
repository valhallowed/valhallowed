"""Ingress (I0): the manifest and the secrets file, read and checked against schema/ (G6, G18).

Nothing past this module reads either file: it hands on Settings, with every default
taken from the manifest schema.
"""

import posixpath
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from vhd_control.schemas import Schemas

MANIFEST_TOML = "manifest.toml"
SECRETS_TOML = "secrets.toml"
MANIFEST_SCHEMA = "schema/manifest.schema.json"
SECRETS_SCHEMA = "schema/secrets.schema.json"
# A duration's unit, in seconds: the manifest schema's $defs/duration.
_UNIT_S = {"m": 60, "h": 3600, "d": 86400}


class InvalidSettings(Exception):
    """The manifest or secrets file cannot be read or breaks its schema."""


@dataclass(frozen=True)
class World:
    name: str
    port: int
    # Never in a repr, so no log line or test diff can carry it.
    password: str | None = field(repr=False)


@dataclass(frozen=True)
class Backups:
    # The host folder, as the manifest names it: relative to the install folder unless absolute.
    path: str
    every_s: int
    keep: int
    keep_daily: int


@dataclass(frozen=True)
class Updates:
    policy: str
    every_s: int


@dataclass(frozen=True)
class Settings:
    """Everything the manifest and secrets say, defaults filled in. Worlds sort by name."""

    worlds: tuple[World, ...]
    web_map_port: int
    backups: Backups
    updates: Updates


def load(install: Path, schemas: Schemas) -> Settings:
    """The install's settings. The secrets file is optional; the manifest is not."""
    manifest = _read(install / MANIFEST_TOML, MANIFEST_SCHEMA, schemas)
    secrets_path = install / SECRETS_TOML
    secrets = _read(secrets_path, SECRETS_SCHEMA, schemas, secret=True) if secrets_path.exists() else {}
    schema = schemas.document(MANIFEST_SCHEMA)
    default_port = cast(int, schema["$defs"]["world"]["properties"]["port"]["default"])
    default_web_map_port = cast(int, schema["properties"]["web_map"]["properties"]["port"]["default"])

    world_tables = cast(dict[str, dict[str, Any]], manifest["world"])
    world_secrets = cast(dict[str, dict[str, Any]], secrets.get("world", {}))
    unknown = sorted(set(world_secrets) - set(world_tables))
    if unknown:
        raise InvalidSettings(f"{SECRETS_TOML} names world {unknown[0]!r}, which {MANIFEST_TOML} does not")
    worlds = tuple(
        World(
            name=name,
            port=cast(int, world_tables[name].get("port", default_port)),
            password=cast(str | None, world_secrets.get(name, {}).get("password")),
        )
        for name in sorted(world_tables)
    )
    web_map = cast(dict[str, Any], manifest.get("web_map", {}))
    backups = _table(manifest, schema, "backups")
    updates = _table(manifest, schema, "updates")
    return Settings(
        worlds=worlds,
        web_map_port=cast(int, web_map.get("port", default_web_map_port)),
        backups=Backups(
            # Normalized, so ./backups and backups/ name the folder backups does.
            path=posixpath.normpath(cast(str, backups["path"])),
            every_s=_seconds(schema, "backups", "every", cast(str, backups["every"])),
            keep=cast(int, backups["keep"]),
            keep_daily=cast(int, backups["keep_daily"]),
        ),
        updates=Updates(
            policy=cast(str, updates["policy"]),
            every_s=_seconds(schema, "updates", "every", cast(str, updates["every"])),
        ),
    )


def _table(manifest: Mapping[str, Any], schema: Mapping[str, Any], name: str) -> dict[str, object]:
    """A table of the manifest, each key it leaves out taken from the schema's default."""
    keys = cast(dict[str, dict[str, Any]], schema["properties"][name]["properties"])
    given = cast(dict[str, object], manifest.get(name, {}))
    return {key: given.get(key, spec["default"]) for key, spec in keys.items()}


def _seconds(schema: Mapping[str, Any], table: str, key: str, text: str) -> int:
    """A duration the schema has matched, in seconds, held to the range its x-seconds states."""
    match = re.fullmatch(r"([0-9]+)([mhd])", text)
    if match is None:
        raise InvalidSettings(f"{MANIFEST_TOML} at {table}/{key}: {text!r} is no duration")
    seconds = int(match[1]) * _UNIT_S[match[2]]
    bounds = cast(dict[str, int], schema["properties"][table]["properties"][key]["x-seconds"])
    if not bounds["minimum"] <= seconds <= bounds["maximum"]:
        raise InvalidSettings(
            f"{MANIFEST_TOML} at {table}/{key}: {text!r} is outside "
            f"{duration(bounds['minimum'])} to {duration(bounds['maximum'])}"
        )
    return seconds


def duration(seconds: int) -> str:
    """Seconds as the manifest writes them, in the largest unit that divides them."""
    for unit, size in sorted(_UNIT_S.items(), key=lambda item: -item[1]):
        if seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def _read(path: Path, schema: str, schemas: Schemas, *, secret: bool = False) -> dict[str, Any]:
    try:
        document = tomllib.loads(path.read_text())
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise InvalidSettings(f"{path.name}: {error}") from error
    problem = schemas.problem(schema, document, secret=secret)
    if problem is not None:
        raise InvalidSettings(f"{path.name} {problem}")
    return document
