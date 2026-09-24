"""Minimal JSON-Schema validator (stdlib only) driven by ``config/config.schema.json``.

The approved schema in ``config/config.schema.json`` is the single source of
truth for configuration shape. This module implements only the subset of JSON
Schema that the file actually uses, so that:

* no second, competing config format is introduced (Issue #3 requirement), and
* validation can run on a VM without pip and without third-party packages.

Supported keywords: ``$ref``/``$defs``, ``type``, ``required``, ``properties``,
``additionalProperties`` (``false`` or a schema), ``propertyNames``, ``enum``,
``const``, ``minLength``, ``minimum``, ``pattern`` and ``default``.

Unsupported keywords fail loudly rather than being ignored: silently skipping a
constraint would be a security-relevant lie about what the schema enforces.
"""

from __future__ import annotations

import re
from typing import Any

SUPPORTED_KEYWORDS = {
    "$schema",
    "$id",
    "$ref",
    "$defs",
    "title",
    "description",
    "type",
    "required",
    "properties",
    "additionalProperties",
    "propertyNames",
    "enum",
    "const",
    "minLength",
    "minimum",
    "pattern",
    "default",
}

TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    # bool is a subclass of int in Python; JSON distinguishes them.
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


class SchemaError(Exception):
    """The schema file itself is unsupported or malformed."""


def _unsupported_keywords(schema: dict[str, Any], where: str, problems: list[str]) -> None:
    for key in schema:
        if key not in SUPPORTED_KEYWORDS:
            problems.append(f"{where}: unsupported schema keyword '{key}'")


def _check_types(instance: Any, schema: dict[str, Any], where: str, problems: list[str]) -> None:
    expected = schema["type"]
    names = [expected] if isinstance(expected, str) else list(expected)
    if not any(TYPE_CHECKS[name](instance) for name in names):
        joined = "/".join(names)
        problems.append(f"{where}: expected {joined}, got {type(instance).__name__}")


def _validate_string(instance: str, schema: dict[str, Any], where: str, problems: list[str]) -> None:
    if "minLength" in schema and len(instance) < schema["minLength"]:
        problems.append(f"{where}: shorter than minLength {schema['minLength']}")
    if "pattern" in schema and not re.search(schema["pattern"], instance):
        problems.append(f"{where}: does not match pattern {schema['pattern']!r}")


def _validate_object(instance: dict[str, Any], schema: dict[str, Any], where: str, problems: list[str]) -> None:
    for name in schema.get("required", []):
        if name not in instance:
            problems.append(f"{where}: missing required key '{name}'")

    name_rule = schema.get("propertyNames")
    if name_rule is not None and "$ref" in name_rule:
        raise SchemaError(f"{where}: propertyNames/$ref is not supported")

    properties = schema.get("properties", {})
    additional = schema.get("additionalProperties", True)

    for key, value in instance.items():
        child_where = f"{where}.{key}"
        if name_rule is not None:
            key_problems: list[str] = []
            if "pattern" in name_rule and not re.search(name_rule["pattern"], key):
                key_problems.append(f"{where}: property name {key!r} does not match {name_rule['pattern']!r}")
            problems.extend(key_problems)

        if key in properties:
            problems.extend(validate(value, properties[key], child_where))
        elif additional is False:
            problems.append(f"{where}: unknown key '{key}' (not allowed by schema)")
        elif isinstance(additional, dict):
            problems.extend(validate(value, additional, child_where))


def validate(instance: Any, schema: dict[str, Any], where: str = "config") -> list[str]:
    """Return a list of human-readable validation problems (empty == valid)."""
    problems: list[str] = []
    _unsupported_keywords(schema, where, problems)

    if "$ref" in schema:
        ref = schema["$ref"]
        if not ref.startswith("#/"):
            raise SchemaError(f"{where}: only local '$ref' targets are supported, got {ref!r}")
        target: Any = _ROOT_HOLDER["root"]
        for part in ref[2:].split("/"):
            if not isinstance(target, dict) or part not in target:
                raise SchemaError(f"{where}: unresolvable '$ref' {ref!r}")
            target = target[part]
        if not isinstance(target, dict):
            raise SchemaError(f"{where}: '$ref' {ref!r} does not point at a schema object")
        return problems + validate(instance, target, where)

    if "const" in schema and instance != schema["const"]:
        problems.append(f"{where}: must equal {schema['const']!r}, got {instance!r}")

    if "enum" in schema and instance not in schema["enum"]:
        allowed = ", ".join(repr(item) for item in schema["enum"])
        problems.append(f"{where}: must be one of [{allowed}], got {instance!r}")

    if "type" in schema:
        before = len(problems)
        _check_types(instance, schema, where, problems)
        # Only descend when the type matched, to avoid a cascade of noise.
        if len(problems) > before:
            return problems

    if isinstance(instance, str):
        _validate_string(instance, schema, where, problems)
    elif isinstance(instance, dict):
        _validate_object(instance, schema, where, problems)
    elif isinstance(instance, int) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            problems.append(f"{where}: must be >= {schema['minimum']}, got {instance}")

    return problems


_ROOT_HOLDER: dict[str, Any] = {"root": {}}


def set_root(schema: dict[str, Any]) -> None:
    """Register the root document so ``$ref``/``$defs`` can be resolved."""
    _ROOT_HOLDER["root"] = schema


def apply_defaults(instance: Any, schema: dict[str, Any]) -> Any:
    """Return ``instance`` with schema ``default`` values filled in.

    Defaults come from ``config.schema.json`` so the documented example and the
    runtime do not drift apart. Objects without defaults are returned as-is.
    """
    if "$ref" in schema:
        target: Any = _ROOT_HOLDER["root"]
        for part in schema["$ref"][2:].split("/"):
            target = target[part]
        return apply_defaults(instance, target)

    if not isinstance(instance, dict):
        return instance

    properties = schema.get("properties", {})
    result = dict(instance)
    for key, child in properties.items():
        if key not in result:
            if "default" in child:
                result[key] = child["default"]
            elif "$ref" in child:
                # Nested objects that exist only as $ref cannot carry a default;
                # skip so we never fabricate an empty required object.
                continue
            continue
        result[key] = apply_defaults(result[key], child)

    additional = schema.get("additionalProperties")
    if isinstance(additional, dict):
        for key in result:
            if key not in properties:
                result[key] = apply_defaults(result[key], additional)

    return result
