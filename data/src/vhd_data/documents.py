"""B1: a service's generation and scoped secrets, validated on every read.

The game runtime's generation module does the same for a world; the two ship in separate
images, so each carries its own.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

GENERATION_FILE = "generation.json"
SECRETS_FILE = "secrets.json"

# jsonschema's own type for a schema document.
_Schema = bool | Mapping[str, Any]


class InvalidDocuments(Exception):
    """The generation or its secrets break the B1 contract."""


class IncompleteDocuments(Exception):
    """The generation's secrets are not written yet: missing, or for another generation."""


@dataclass(frozen=True)
class Documents:
    number: int
    generation: Mapping[str, Any]
    # Never in a repr, so no log line or test diff can carry a password.
    secrets: Mapping[str, Any] = field(repr=False)


class Reader:
    """Reads the documents the control plane wrote into this service's directory."""

    def __init__(self, service: str, directory: Path, contracts: Path) -> None:
        self._directory = directory
        self._generation = validator(contracts / f"{service}.generation.schema.json")
        self._secrets = validator(contracts / f"{service}.secrets.schema.json")
        self._read: tuple[tuple[int, ...], Documents] | None = None

    def read(self) -> Documents | None:
        """The current documents, or None while the control plane has written none. Raises
        IncompleteDocuments until secrets.json carries the same generation."""
        generation_path = self._directory / GENERATION_FILE
        secrets_path = self._directory / SECRETS_FILE
        # Asked every second; the files change only when the control plane replaces them,
        # which gives each a new inode.
        stamp = _stamp(generation_path, secrets_path)
        if self._read is not None and self._read[0] == stamp:
            return self._read[1]
        # A service's directory holds two files. A directory here means the whole generations
        # volume is mounted, every child's secrets with it.
        if self._directory.is_dir() and any(entry.is_dir() for entry in self._directory.iterdir()):
            raise InvalidDocuments(f"{self._directory} shows other children's directories, not this service's files")
        if not generation_path.exists():
            return None
        generation = _load(generation_path, self._generation)
        number = generation["generation"]
        if not secrets_path.exists():
            raise IncompleteDocuments(f"generation {number} has no {SECRETS_FILE} yet")
        secrets = _load(secrets_path, self._secrets, secret=True)
        if secrets["generation"] != number:
            raise IncompleteDocuments(f"{SECRETS_FILE} belongs to generation {secrets['generation']}, not {number}")
        read = Documents(number=number, generation=generation, secrets=secrets)
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


def validator(schema_path: Path, definition: str | None = None) -> Draft202012Validator:
    """A validator for one contract, or for one of its $defs, which may refer to the other
    contracts and to schema/ by relative path."""
    uri = schema_path.resolve().as_uri()
    if definition is not None:
        return Draft202012Validator({"$ref": f"{uri}#/$defs/{definition}"}, registry=_registry(schema_path.parent))
    schema = json.loads(schema_path.read_text())
    schema["$id"] = uri
    return Draft202012Validator(schema, registry=_registry(schema_path.parent))


def _registry(contracts: Path) -> Registry[_Schema]:
    registry: Registry[_Schema] = Registry()
    for path in [*contracts.glob("*.schema.json"), *(contracts.parent / "schema").glob("*.schema.json")]:
        resource: Resource[_Schema] = Resource.from_contents(
            json.loads(path.read_text()), default_specification=DRAFT202012
        )
        registry = registry.with_resource(path.resolve().as_uri(), resource)
    return registry


def problem(validator: Draft202012Validator, document: object, *, secret: bool = False) -> str | None:
    """The first way document breaks the validator's contract, or None if it meets it. For a
    secret document it names where and which rule, never the value."""
    errors = sorted(validator.iter_errors(document), key=lambda e: list(e.absolute_path))
    if not errors:
        return None
    first = errors[0]
    where = "/".join(str(p) for p in first.absolute_path) or "(root)"
    return f"at {where}: {_rule(first) if secret else first.message}"


def _load(path: Path, validator: Draft202012Validator, *, secret: bool = False) -> dict[str, Any]:
    try:
        document: object = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise InvalidDocuments(f"{path.name}: {error}") from error
    found = problem(validator, document, secret=secret)
    if found is not None:
        raise InvalidDocuments(f"{path.name} {found}")
    # Every contract here is an object.
    return cast(dict[str, Any], document)


def _rule(error: ValidationError) -> str:
    """What a value breaks, without the value. The messages kept name keys, never values."""
    # jsonschema's stubs type the keyword as a validator; at run time it is the keyword's name.
    keyword = str(error.validator)
    if keyword in ("additionalProperties", "required"):
        return str(error.message)
    return f"breaks {keyword} {json.dumps(error.validator_value)}"
