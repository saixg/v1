"""The JSON Schema -> Gemini responseSchema transform.

Pure function, no network, no key. The last test runs the real
`BlueprintDraft` schema through it so the transform is proven against a
schema that actually ships rather than a hand-written stand-in.
"""

from __future__ import annotations

from typing import Any, Literal

import pytest
from pydantic import BaseModel, Field

from app.llm.gemini_schema import RecursiveSchemaError, transform_schema

KEPT = {"type", "properties", "required", "items", "enum", "description", "nullable"}


def walk(node: Any):
    """Every schema object in the transformed tree."""
    if isinstance(node, dict):
        yield node
        for key in ("properties",):
            for child in (node.get(key) or {}).values():
                yield from walk(child)
        if "items" in node:
            yield from walk(node["items"])


def test_ref_is_inlined():
    class Leaf(BaseModel):
        value: str

    class Root(BaseModel):
        leaf: Leaf

    out = transform_schema(Root.model_json_schema())
    assert "$defs" not in out
    assert out["properties"]["leaf"]["type"] == "OBJECT"
    assert out["properties"]["leaf"]["properties"]["value"]["type"] == "STRING"


def test_repeated_ref_is_inlined_at_every_use_site():
    class Leaf(BaseModel):
        value: str

    class Root(BaseModel):
        a: Leaf
        b: Leaf
        many: list[Leaf]

    out = transform_schema(Root.model_json_schema())
    props = out["properties"]
    expected = {"value": {"type": "STRING"}}
    assert props["a"]["properties"] == expected
    assert props["b"]["properties"] == expected
    assert props["many"]["items"]["properties"] == expected
    # Two independent dicts, not one shared object: mutating one must not
    # reach the other if a caller ever post-processes the result.
    assert props["a"] is not props["b"]


def test_recursive_schema_fails_loudly():
    schema = {
        "type": "object",
        "properties": {"child": {"$ref": "#/$defs/Node"}},
        "$defs": {
            "Node": {"type": "object", "properties": {"child": {"$ref": "#/$defs/Node"}}}
        },
    }
    with pytest.raises(RecursiveSchemaError, match="Node"):
        transform_schema(schema)


def test_mutually_recursive_schema_fails_loudly():
    schema = {
        "$ref": "#/$defs/A",
        "$defs": {
            "A": {"type": "object", "properties": {"b": {"$ref": "#/$defs/B"}}},
            "B": {"type": "object", "properties": {"a": {"$ref": "#/$defs/A"}}},
        },
    }
    with pytest.raises(RecursiveSchemaError):
        transform_schema(schema)


def test_missing_definition_is_an_error_not_an_empty_schema():
    with pytest.raises(ValueError, match="no definition"):
        transform_schema({"type": "object", "properties": {"x": {"$ref": "#/$defs/Gone"}}})


def test_optional_field_idiom_becomes_nullable():
    class Root(BaseModel):
        maybe: str | None = None
        required: str

    out = transform_schema(Root.model_json_schema())
    maybe = out["properties"]["maybe"]
    assert maybe["type"] == "STRING"
    assert maybe["nullable"] is True
    assert "anyOf" not in maybe
    # A non-optional field is not marked nullable.
    assert "nullable" not in out["properties"]["required"]


def test_optional_nested_model_is_inlined_and_nullable():
    class Leaf(BaseModel):
        value: str

    class Root(BaseModel):
        leaf: Leaf | None = None

    leaf = transform_schema(Root.model_json_schema())["properties"]["leaf"]
    assert leaf["type"] == "OBJECT"
    assert leaf["nullable"] is True
    assert leaf["properties"]["value"]["type"] == "STRING"


def test_dropped_constraints_are_preserved_in_the_description():
    class Root(BaseModel):
        key: str = Field(pattern=r"^R[0-9]{1,4}$", description="Unique key")
        quote: str = Field(max_length=600)
        score: float = Field(ge=0, le=1)
        items_: list[str] = Field(default_factory=list, max_length=20)

    props = transform_schema(Root.model_json_schema())["properties"]

    # The original description survives; the constraint is appended to it.
    assert props["key"]["description"].startswith("Unique key")
    assert "pattern: ^R[0-9]{1,4}$" in props["key"]["description"]
    assert "pattern" not in props["key"]

    assert "maxLength: 600" in props["quote"]["description"]
    desc = props["score"]["description"]
    assert "minimum: 0" in desc and "maximum: 1" in desc
    assert "maxItems: 20" in props["items_"]["description"]


def test_rejected_keywords_are_gone_everywhere():
    class Leaf(BaseModel):
        model_config = {"extra": "forbid"}
        tag: Literal["only"] = "only"

    class Root(BaseModel):
        model_config = {"extra": "forbid"}
        leaf: Leaf
        leaves: list[Leaf]

    out = transform_schema(Root.model_json_schema())
    for node in walk(out):
        assert set(node) <= KEPT, node


def test_const_becomes_a_single_value_enum():
    out = transform_schema({"type": "string", "const": "only"})
    assert out == {"type": "STRING", "enum": ["only"]}


def test_literal_enum_is_kept():
    class Root(BaseModel):
        kind: Literal["api", "event", "function"]

    kind = transform_schema(Root.model_json_schema())["properties"]["kind"]
    assert kind["enum"] == ["api", "event", "function"]
    assert kind["type"] == "STRING"


def test_multi_branch_union_keeps_first_branch_and_describes_the_rest():
    # Not the optional idiom: a real sum type, which the subset cannot express.
    out = transform_schema({"anyOf": [{"type": "string"}, {"type": "integer"}]})
    assert out["type"] == "STRING"
    assert "may instead be one of" in out["description"]
    assert "integer" in out["description"]


def test_type_null_alone_is_nullable():
    assert transform_schema({"type": "null"}) == {"nullable": True}


def test_input_is_not_mutated():
    schema = {
        "type": "object",
        "properties": {"leaf": {"$ref": "#/$defs/Leaf"}},
        "$defs": {"Leaf": {"type": "object", "properties": {"v": {"type": "string"}}}},
    }
    before = repr(schema)
    transform_schema(schema)
    assert repr(schema) == before


def test_real_shipping_schemas_transform_cleanly():
    """The two schemas that actually flow through `generate_structured`."""
    from app.blueprint.generate import BlueprintDraft
    from app.context.extract import RequirementExtraction

    for model in (BlueprintDraft, RequirementExtraction):
        out = transform_schema(model.model_json_schema())
        nodes = list(walk(out))
        assert nodes, model
        for node in nodes:
            assert set(node) <= KEPT, (model, node)
        # Nested models really were inlined rather than left as references.
        assert "$defs" not in out

    draft = transform_schema(BlueprintDraft.model_json_schema())
    component = draft["properties"]["components"]["items"]
    assert component["type"] == "OBJECT"
    assert set(component["required"]) == {"key", "name", "mapping", "responsibility"}
    # `candidate: str | None` -- the optional idiom, two models deep.
    assert component["properties"]["candidate"]["nullable"] is True
    # `interfaces: list[DraftInterface]` -- a $ref inside a list inside a $ref.
    assert component["properties"]["interfaces"]["items"]["properties"]["kind"]["enum"] == [
        "api",
        "event",
        "function",
    ]
