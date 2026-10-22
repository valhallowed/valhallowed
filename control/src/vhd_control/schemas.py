"""Validation against the schemas a release ships, in schema/ and contracts/."""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

# jsonschema's own type for a schema document.
_Schema = bool | Mapping[str, Any]


class Schemas:
    """The schemas under root, which holds schema/ and contracts/. A schema refers to
    another by relative path, so each gets its own file URI and the registry holds every
    schema it may name: validation never reads a file it was not given."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._registry: Registry[_Schema] = Registry()
        for path in [*(root / "schema").glob("*.schema.json"), *(root / "contracts").glob("*.schema.json")]:
            resource: Resource[_Schema] = Resource.from_contents(
                json.loads(path.read_text()), default_specification=DRAFT202012
            )
            self._registry = self._registry.with_resource(path.resolve().as_uri(), resource)

    def document(self, name: str) -> dict[str, Any]:
        """The schema at name, relative to root, such as schema/manifest.schema.json."""
        document: dict[str, Any] = json.loads((self._root / name).read_text())
        return document

    def problem(self, name: str, instance: object) -> str | None:
        """The first way instance breaks the schema at name, or None if it meets it."""
        schema = self.document(name)
        schema["$id"] = (self._root / name).resolve().as_uri()
        validator = Draft202012Validator(schema, registry=self._registry)
        errors = sorted(validator.iter_errors(instance), key=lambda e: list(e.absolute_path))
        if not errors:
            return None
        where = "/".join(str(p) for p in errors[0].absolute_path) or "(root)"
        return f"at {where}: {errors[0].message}"
