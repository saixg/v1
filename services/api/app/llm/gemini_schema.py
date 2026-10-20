"""JSON Schema -> Gemini `responseSchema` transform.

Gemini's `generationConfig.responseSchema` accepts a subset of OpenAPI 3.0,
not JSON Schema. `BaseModel.model_json_schema()` routinely emits keywords it
rejects outright: `$ref`/`$defs` for every nested model, `additionalProperties`
for a strict model, `const` for a single-value `Literal`, and the
`anyOf: [X, {"type": "null"}]` idiom for every `X | None` field. Sending one of
those is a 400, so the schema has to be rewritten rather than forwarded.

The contract
------------
`transform_schema` returns a schema containing only `type`, `properties`,
`required`, `items`, `enum`, `description` and `nullable`. `type` values are
upper-cased (`STRING`, `OBJECT`, ...), which is how the `Schema.type` proto
enum is named; lower-case happens to be accepted today but is not the
documented spelling.

Every other keyword is dropped. A dropped keyword that *constrains a value* --
a bound, a length, a pattern, a format, a default -- is appended to the
field's `description` instead of vanishing, because the model is the only
thing left that can honour it: `RoutedModelService` still validates the
response with the caller's Pydantic model, so a constraint lost here does not
become an invalid object, it becomes a retry and then a `StructuredOutputError`.
Describing it is what keeps the first attempt likely to pass. (Anthropic's own
`transform_schema` folds unsupported constraints into descriptions for the
same reason; this mirrors it deliberately.)

Keywords dropped *silently* are the ones that carry nothing a generator could
act on: `title` (it only repeats the field name), `additionalProperties`,
`$schema`, `$id`, and the `$defs`/`definitions` blocks once they are inlined.

The keep-list is deliberately narrower than what the API may actually tolerate
today. Over-sending is a hard 400 that kills the call; under-sending costs a
description string and is caught by Pydantic. When a vendor-documented wider
subset is worth the risk, widen `_KEEP` -- not the call sites.

Named gaps
----------
- **Recursive schemas are refused, not truncated.** A self-referencing model
  cannot be inlined into a finite tree, and silently cutting the recursion off
  at some depth would send the model a schema that is not the one Pydantic
  validates against. `RecursiveSchemaError` is raised instead.
- **A multi-branch `anyOf` (a real union, not the optional idiom) keeps only
  its first branch's structure**, with the remaining branches written into the
  description as JSON. There is no sum type in the supported subset, so this
  is the strictest available encoding; the alternatives are stated rather than
  dropped. No caller in this repository currently has one.
"""

from __future__ import annotations

import json
from typing import Any

# The only keywords forwarded to the API. See the module docstring.
_KEEP = frozenset({"type", "properties", "required", "items", "enum", "description", "nullable"})

# Dropped without a trace: nothing a generator can act on.
_SILENT = frozenset(
    {"title", "additionalProperties", "$schema", "$id", "$defs", "definitions", "$comment"}
)

# Dropped, but restated in the description because they constrain the value.
# Order fixed so the appended note is deterministic (and testable).
_DESCRIBED = (
    "pattern",
    "format",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minItems",
    "maxItems",
    "uniqueItems",
    "minProperties",
    "maxProperties",
    "default",
)


class RecursiveSchemaError(ValueError):
    """The schema references itself, directly or through a cycle.

    Raised rather than unrolled to a fixed depth: a truncated schema is not
    the schema Pydantic will validate the response against, and sending one
    would make the mismatch look like a model failure."""


def transform_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Rewrite a Pydantic JSON schema into Gemini's OpenAPI 3.0 subset.

    Pure: the input is never mutated. Raises `RecursiveSchemaError` on a
    cyclic schema.
    """
    defs: dict[str, Any] = {}
    for key in ("$defs", "definitions"):
        found = schema.get(key)
        if isinstance(found, dict):
            defs.update(found)
    return _convert(schema, defs, ())


def _convert(node: Any, defs: dict[str, Any], path: tuple[str, ...]) -> dict[str, Any]:
    """One schema node. `path` is the chain of `$ref` names currently being
    inlined, which is what makes a cycle detectable."""
    if not isinstance(node, dict):
        # `true`/`false` schemas and malformed nodes: the honest rendering of
        # "anything" in a subset with no such keyword is an absent type.
        return {}

    if "$ref" in node:
        name = _ref_name(str(node["$ref"]))
        if name in path:
            raise RecursiveSchemaError(
                f"schema definition {name!r} references itself "
                f"(via {' -> '.join((*path, name))}); Gemini's responseSchema "
                "has no $ref, so it cannot be expressed"
            )
        target = defs.get(name)
        if target is None:
            raise ValueError(f"schema has no definition for $ref {node['$ref']!r}")
        inlined = _convert(target, defs, (*path, name))
        # A sibling `description` on the $ref node is the field's own docstring
        # and is more specific than the definition's, so it wins.
        if isinstance(node.get("description"), str):
            inlined["description"] = node["description"]
        return inlined

    out: dict[str, Any] = {}
    notes: list[str] = []

    optional, members = _split_union(node)
    if members is not None:
        # The optional idiom collapses into the branch plus `nullable`; a real
        # union keeps its first branch and describes the rest (see docstring).
        out = _convert(members[0], defs, path)
        if len(members) > 1:
            notes.append(
                "may instead be one of: "
                + "; ".join(json.dumps(m, sort_keys=True) for m in members[1:])
            )
    if optional:
        out["nullable"] = True

    for key, value in node.items():
        if key in _SILENT or key in ("$ref", "anyOf", "oneOf", "allOf"):
            continue
        if key == "const":
            # A single-value Literal. `enum` of one is the supported spelling
            # and keeps the constraint enforceable by the API.
            out["enum"] = [value]
            continue
        if key == "type":
            if value == "null":
                out["nullable"] = True
            elif isinstance(value, str):
                out["type"] = value.upper()
            elif isinstance(value, list):
                # JSON Schema's type-array form; same shape as a union.
                concrete = [t for t in value if t != "null"]
                if len(value) != len(concrete):
                    out["nullable"] = True
                if concrete:
                    out["type"] = str(concrete[0]).upper()
                if len(concrete) > 1:
                    notes.append(f"may also be of type: {', '.join(map(str, concrete[1:]))}")
            continue
        if key == "properties" and isinstance(value, dict):
            out["properties"] = {k: _convert(v, defs, path) for k, v in value.items()}
            continue
        if key == "items":
            out["items"] = _convert(value, defs, path)
            continue
        if key == "prefixItems" and isinstance(value, list):
            # Tuples: the subset has no positional items, so the first
            # position's schema is sent and the real shape is described.
            out["items"] = _convert(value[0], defs, path) if value else {}
            notes.append(f"a fixed-length tuple of {len(value)} positional values")
            continue
        if key in _DESCRIBED:
            notes.append(f"{key}: {_render(value)}")
            continue
        if key in _KEEP:
            out[key] = value
            continue
        # Anything unrecognised: named rather than dropped, so an unhandled
        # keyword shows up in the prompt instead of disappearing.
        notes.append(f"{key}: {_render(value)}")

    if notes:
        out["description"] = _append_notes(out.get("description"), notes)
    return out


def _split_union(node: dict[str, Any]) -> tuple[bool, list[dict[str, Any]] | None]:
    """(is nullable, non-null branches) for `anyOf`/`oneOf`/`allOf`.

    `(False, None)` when the node is not a union, so the caller can tell
    "no union" from "a union of nothing".
    """
    for key in ("anyOf", "oneOf", "allOf"):
        branches = node.get(key)
        if isinstance(branches, list) and branches:
            members = [b for b in branches if not _is_null_schema(b)]
            nullable = len(members) != len(branches)
            return nullable, members or [{}]
    return False, None


def _is_null_schema(branch: Any) -> bool:
    return isinstance(branch, dict) and branch.get("type") == "null" and len(branch) == 1


def _ref_name(ref: str) -> str:
    return ref.rsplit("/", 1)[-1]


def _render(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True)


def _append_notes(description: str | None, notes: list[str]) -> str:
    joined = "; ".join(notes)
    base = (description or "").strip()
    constraint = f"Constraints (enforced after generation): {joined}."
    return f"{base} {constraint}".strip() if base else constraint
