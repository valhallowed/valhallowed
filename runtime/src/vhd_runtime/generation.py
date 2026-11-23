"""B1: this runtime's generation and scoped secrets, validated on every read."""

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, NotRequired, TypedDict, cast

from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry, Resource
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012

GENERATION_FILE = "generation.json"
SECRETS_FILE = "secrets.json"

# jsonschema's own type for a schema document.
_Schema = bool | Mapping[str, Any]


class InvalidGeneration(Exception):
    """The generation or its secrets break the B1 contract."""


class IncompleteGeneration(Exception):
    """The generation's secrets are not written yet: missing, or for another generation."""


class _World(TypedDict):
    name: str
    port: int


class _Plugin(TypedDict):
    status_interval_s: int
    intake_url: str


class _Backups(TypedDict):
    every_s: int
    keep: int
    keep_daily: int


class _Updates(TypedDict):
    policy: str
    every_s: int


class _Restore(TypedDict):
    id: str
    backup: str


class _GenerationDocument(TypedDict):
    contract: int
    generation: int
    world: _World
    plugin: _Plugin
    backups: _Backups
    updates: _Updates
    status_url: NotRequired[str]
    disconnect_players: NotRequired[bool]
    restore: NotRequired[_Restore]


class _SecretsDocument(TypedDict):
    contract: int
    generation: int
    password: NotRequired[str]


@dataclass(frozen=True)
class Backups:
    """How the runtime backs up its world."""

    every_s: int
    keep: int
    keep_daily: int


@dataclass(frozen=True)
class Updates:
    """The update policy: when-empty or at-start, and how often to look for a newer game build."""

    policy: str
    every_s: int


@dataclass(frozen=True)
class Restore:
    """A restore of one of the world's backups, done once for its id."""

    id: str
    backup: str


@dataclass(frozen=True)
class Generation:
    number: int
    world: str
    port: int
    status_interval_s: int
    status_url: str | None
    # Where the plugin posts telemetry (B2).
    intake_url: str
    # Never in a repr, so no log line or test diff can carry it.
    password: str | None = field(repr=False)
    backups: Backups
    updates: Updates
    # The server manager confirmed restarting this world with players online, for this
    # generation alone. It is no setting: it never makes two generations differ.
    disconnect_players: bool = False
    # A restore the runtime does once for its id, at a start; whether it costs a restart depends on
    # whether that id is done, which only the runtime knows, so it never makes two generations differ.
    restore: Restore | None = None

    def same_settings(self, other: "Generation") -> bool:
        """Whether other differs from this generation in nothing but its number, its confirmation,
        its restore, and the backups and updates the runtime adopts in place."""
        adopted = replace(
            other,
            number=self.number,
            disconnect_players=self.disconnect_players,
            backups=self.backups,
            updates=self.updates,
            restore=self.restore,
        )
        return adopted == self


class Reader:
    """Reads the generation the control plane wrote into this runtime's volume."""

    def __init__(self, directory: Path, contracts: Path) -> None:
        self._directory = directory
        self._generation = validator(contracts / "game-runtime.generation.schema.json")
        self._secrets = validator(contracts / "game-runtime.secrets.schema.json")
        self._read: tuple[tuple[int, ...], Generation] | None = None

    def read(self) -> Generation | None:
        """The current generation with its secrets, or None while the control plane has written none.
        Raises IncompleteGeneration until secrets.json carries the same generation."""
        generation_path = self._directory / GENERATION_FILE
        secrets_path = self._directory / SECRETS_FILE
        # The supervisor asks every second; the files change only when the control plane
        # replaces them, which gives each a new inode.
        stamp = _stamp(generation_path, secrets_path)
        if self._read is not None and self._read[0] == stamp:
            return self._read[1]
        # Each world's directory holds two files. A directory here means the whole generations
        # volume is mounted, every world's secrets with it; its root holds no generation.json.
        if self._directory.is_dir() and any(entry.is_dir() for entry in self._directory.iterdir()):
            raise InvalidGeneration(f"{self._directory} shows other worlds' directories, not this world's own files")
        if not generation_path.exists():
            return None
        generation = cast(_GenerationDocument, _load(generation_path, self._generation))
        number = generation["generation"]
        if not secrets_path.exists():
            raise IncompleteGeneration(f"generation {number} has no {SECRETS_FILE} yet")
        secrets = cast(_SecretsDocument, _load(secrets_path, self._secrets, secret=True))
        if secrets["generation"] != number:
            raise IncompleteGeneration(f"{SECRETS_FILE} belongs to generation {secrets['generation']}, not {number}")
        read = Generation(
            number=number,
            world=generation["world"]["name"],
            port=generation["world"]["port"],
            status_interval_s=generation["plugin"]["status_interval_s"],
            status_url=generation.get("status_url"),
            intake_url=generation["plugin"]["intake_url"],
            password=secrets.get("password"),
            backups=Backups(**generation["backups"]),
            updates=Updates(**generation["updates"]),
            disconnect_players=generation.get("disconnect_players", False),
            restore=Restore(**generation["restore"]) if "restore" in generation else None,
        )
        self._read = stamp, read
        return read


def _stamp(*paths: Path) -> tuple[int, ...]:
    """Which version of each file is on disk, or a stamp that matches nothing when one is missing."""
    stamp: list[int] = []
    for path in paths:
        try:
            info = path.stat()
        except FileNotFoundError:
            return (-1,)
        stamp += [info.st_ino, info.st_mtime_ns, info.st_size]
    return tuple(stamp)


# Keywords whose values are instances, never schemas, so a "$ref" key inside one is data.
_DATA_KEYWORDS = frozenset({"const", "enum", "default", "examples"})
# Keywords whose values map names to schemas: a property named "default" is a schema.
_SCHEMA_MAPS = frozenset({"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"})


def validator(schema_path: Path) -> Draft202012Validator:
    """A validator for one contract. The control plane's schemas.Schemas does the same; the two
    ship in separate images, so each carries its own. Raises ValueError when a $ref in the
    contract itself names a schema it cannot find, before anything is validated against it; a $ref
    inside a schema it names is not followed."""
    schema = json.loads(schema_path.read_text())
    # A contract refers to the manifest schema by relative path: the schema gets
    # its own file URI as base, and the registry holds every schema it may name,
    # so validation never reads a file it was not given.
    schema["$id"] = schema_path.resolve().as_uri()
    registry = _registry(schema_path.parent).with_resource(
        schema["$id"], Resource.from_contents(schema, default_specification=DRAFT202012)
    )
    resolver = registry.resolver(base_uri=schema["$id"])
    for reference in _references(schema):
        try:
            resolver.lookup(reference)
        except Unresolvable as error:
            raise ValueError(f"{schema_path.name} refers to {reference}, which cannot be found") from error
    return Draft202012Validator(schema, registry=registry)


def _references(schema: object) -> Iterator[str]:
    """Each $ref in a schema, leaving out the values of keywords that hold data, not schemas."""
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key == "$ref" and isinstance(value, str):
                yield value
            elif key in _SCHEMA_MAPS and isinstance(value, dict):
                for each in value.values():
                    yield from _references(each)
            elif key not in _DATA_KEYWORDS:
                yield from _references(value)
    elif isinstance(schema, list):
        for value in schema:
            yield from _references(value)


def _registry(contracts: Path) -> Registry[_Schema]:
    registry: Registry[_Schema] = Registry()
    for path in [*contracts.glob("*.schema.json"), *(contracts.parent / "schema").glob("*.schema.json")]:
        resource: Resource[_Schema] = Resource.from_contents(
            json.loads(path.read_text()), default_specification=DRAFT202012
        )
        registry = registry.with_resource(path.resolve().as_uri(), resource)
    return registry


def _load(path: Path, validator: Draft202012Validator, *, secret: bool = False) -> object:
    """The document at path, once it meets its contract. Callers cast to its shape. For a
    secret document the error names where and which rule, never the value it logs."""
    try:
        document: object = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise InvalidGeneration(f"{path.name}: {error}") from error
    errors = sorted(validator.iter_errors(document), key=lambda e: list(e.absolute_path))
    if errors:
        first = errors[0]
        where = "/".join(str(p) for p in first.absolute_path) or "(root)"
        raise InvalidGeneration(f"{path.name} at {where}: {_rule(first) if secret else first.message}")
    return document


def breach(validator: Draft202012Validator, document: object) -> str | None:
    """Where a document first breaks its contract and which rule, without its values, or None when
    it keeps it."""
    errors = sorted(validator.iter_errors(document), key=lambda e: list(e.absolute_path))
    if not errors:
        return None
    where = "/".join(str(p) for p in errors[0].absolute_path) or "(root)"
    return f"at {where}: {_rule(errors[0])}"


def _rule(error: ValidationError) -> str:
    """What a value breaks, without the value; the control plane's schemas._rule, which ships in
    another image. The messages kept name keys, never values."""
    # jsonschema's stubs type the keyword as a validator; at run time it is the keyword's name.
    keyword = str(error.validator)
    if keyword in ("additionalProperties", "required"):
        return error.message
    return f"breaks {keyword} {json.dumps(error.validator_value)}"
