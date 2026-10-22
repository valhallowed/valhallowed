"""B1: this runtime's generation and scoped secrets, validated on every read."""

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, NotRequired, TypedDict, cast

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
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


class _GenerationDocument(TypedDict):
    contract: int
    generation: int
    world: _World
    plugin: _Plugin
    status_url: NotRequired[str]


class _SecretsDocument(TypedDict):
    contract: int
    generation: int
    password: NotRequired[str]


@dataclass(frozen=True)
class Generation:
    number: int
    world: str
    port: int
    status_interval_s: int
    status_url: str | None
    password: str | None

    def same_settings(self, other: "Generation") -> bool:
        """Whether other differs from this generation in nothing but its number."""
        return replace(other, number=self.number) == self


class Reader:
    """Reads the generation the control plane wrote into this runtime's volume."""

    def __init__(self, directory: Path, contracts: Path) -> None:
        self._directory = directory
        self._generation = _validator(contracts / "game-runtime.generation.schema.json")
        self._secrets = _validator(contracts / "game-runtime.secrets.schema.json")

    def read(self) -> Generation | None:
        """The current generation with its secrets, or None while the control plane has written none.
        Raises IncompleteGeneration until secrets.json carries the same generation."""
        generation_path = self._directory / GENERATION_FILE
        if not generation_path.exists():
            return None
        # Each world's directory holds two files. A directory here means the whole generations
        # volume is mounted, every world's secrets with it.
        if any(entry.is_dir() for entry in self._directory.iterdir()):
            raise InvalidGeneration(f"{self._directory} shows other worlds' directories, not this world's own files")
        generation = cast(_GenerationDocument, _load(generation_path, self._generation))
        number = generation["generation"]
        secrets_path = self._directory / SECRETS_FILE
        if not secrets_path.exists():
            raise IncompleteGeneration(f"generation {number} has no {SECRETS_FILE} yet")
        secrets = cast(_SecretsDocument, _load(secrets_path, self._secrets))
        if secrets["generation"] != number:
            raise IncompleteGeneration(f"{SECRETS_FILE} belongs to generation {secrets['generation']}, not {number}")
        return Generation(
            number=number,
            world=generation["world"]["name"],
            port=generation["world"]["port"],
            status_interval_s=generation["plugin"]["status_interval_s"],
            status_url=generation.get("status_url"),
            password=secrets.get("password"),
        )


def _validator(schema_path: Path) -> Draft202012Validator:
    schema = json.loads(schema_path.read_text())
    # A contract refers to the manifest schema by relative path: the schema gets
    # its own file URI as base, and the registry holds every schema it may name,
    # so validation never reads a file it was not given.
    schema["$id"] = schema_path.resolve().as_uri()
    return Draft202012Validator(schema, registry=_registry(schema_path.parent))


def _registry(contracts: Path) -> Registry[_Schema]:
    registry: Registry[_Schema] = Registry()
    for path in [*contracts.glob("*.schema.json"), *(contracts.parent / "schema").glob("*.schema.json")]:
        resource: Resource[_Schema] = Resource.from_contents(
            json.loads(path.read_text()), default_specification=DRAFT202012
        )
        registry = registry.with_resource(path.resolve().as_uri(), resource)
    return registry


def _load(path: Path, validator: Draft202012Validator) -> object:
    """The document at path, once it meets its contract. Callers cast to its shape."""
    try:
        document: object = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise InvalidGeneration(f"{path.name}: {error}") from error
    errors = sorted(validator.iter_errors(document), key=lambda e: list(e.absolute_path))
    if errors:
        where = "/".join(str(p) for p in errors[0].absolute_path) or "(root)"
        raise InvalidGeneration(f"{path.name} at {where}: {errors[0].message}")
    return document
