"""Slice 1 tests — OutputContract + OutputSchema + OutputFormat."""
from __future__ import annotations

from typing import Any, Dict

import pytest

from backend.core.ouroboros.governance.output_contract import (
    OUTPUT_CONTRACT_SCHEMA_VERSION,
    MarkdownSection,
    OutputContract,
    OutputContractError,
    OutputFormat,
    OutputSchema,
    known_field_types,
    known_formats,
)


# ===========================================================================
# Schema version + constants
# ===========================================================================


def test_schema_version_stable():
    assert OUTPUT_CONTRACT_SCHEMA_VERSION == "output_contract.v1"


def test_known_formats_covers_expected_set():
    formats = known_formats()
    assert "json" in formats
    assert "markdown_sections" in formats
    assert "csv" in formats
    assert "yaml" in formats
    assert "code_block" in formats
    assert "plain" in formats


def test_known_field_types_covers_json_primitives():
    types = known_field_types()
    assert types >= {"string", "integer", "number", "boolean", "array", "object"}


# ===========================================================================
# OutputSchema
# ===========================================================================


def test_schema_from_mapping_happy_path():
    s = OutputSchema.from_mapping({
        "fields": {
            "title": {"type": "string", "required": True},
            "count": {"type": "integer", "minimum": 1},
        },
    })
    assert s.field_names == ("count", "title")
    assert s.required_field_names == ("title",)
    assert s.strict is True


def test_schema_rejects_non_mapping():
    with pytest.raises(OutputContractError):
        OutputSchema.from_mapping("not a dict")  # type: ignore[arg-type]


def test_schema_rejects_bad_field_name():
    with pytest.raises(OutputContractError):
        OutputSchema.from_mapping({
            "fields": {"has space": {"type": "string"}},
        })


def test_schema_rejects_unknown_type():
    with pytest.raises(OutputContractError):
        OutputSchema.from_mapping({
            "fields": {"x": {"type": "color"}},
        })


def test_schema_rejects_unknown_spec_key():
    with pytest.raises(OutputContractError):
        OutputSchema.from_mapping({
            "fields": {"x": {"type": "string", "undocumented_key": 1}},
        })


def test_schema_rejects_non_bool_required():
    with pytest.raises(OutputContractError):
        OutputSchema.from_mapping({
            "fields": {"x": {"type": "string", "required": "yes"}},
        })


def test_schema_rejects_empty_enum():
    with pytest.raises(OutputContractError):
        OutputSchema.from_mapping({
            "fields": {"x": {"type": "string", "enum": []}},
        })


def test_schema_rejects_bad_regex():
    with pytest.raises(OutputContractError):
        OutputSchema.from_mapping({
            "fields": {"x": {"type": "string", "regex": "[invalid"}},
        })


def test_schema_rejects_non_bool_strict():
    with pytest.raises(OutputContractError):
        OutputSchema.from_mapping({
            "fields": {"x": {"type": "string"}},
            "strict": "yes",
        })


def test_schema_items_type_checked():
    OutputSchema.from_mapping({
        "fields": {
            "tags": {"type": "array", "items": {"type": "string"}},
        },
    })
    with pytest.raises(OutputContractError):
        OutputSchema.from_mapping({
            "fields": {
                "tags": {"type": "array", "items": {"type": "color"}},
            },
        })


def test_schema_non_strict_tolerated():
    s = OutputSchema.from_mapping({
        "fields": {"x": {"type": "string"}},
        "strict": False,
    })
    assert s.strict is False


def test_schema_is_frozen():
    s = OutputSchema()
    with pytest.raises(Exception):
        s.strict = False  # type: ignore[misc]


# ===========================================================================
# MarkdownSection
# ===========================================================================


def test_section_valid_construction():
    s = MarkdownSection(name="Summary", heading_level=2, required=True)
    assert s.name == "Summary"
    assert s.heading_level == 2


def test_section_must_be_passed_to_contract_for_validation():
    """MarkdownSection's own dataclass doesn't validate; the contract
    runs _validate_markdown_section on every entry."""
    # Bad entry won't raise on construction
    bad = MarkdownSection(name="bad name with ? marks", heading_level=2)
    # But the contract rejects it
    with pytest.raises(OutputContractError):
        OutputContract.from_mapping({
            "name": "x",
            "format": "markdown_sections",
            "sections": [bad],
        })


# ===========================================================================
# OutputContract — JSON format
# ===========================================================================


def test_json_contract_minimal():
    c = OutputContract.from_mapping({
        "name": "result",
        "format": "json",
    })
    assert c.format is OutputFormat.JSON
    assert c.schema is None


def test_json_contract_with_schema():
    c = OutputContract.from_mapping({
        "name": "result",
        "format": "json",
        "schema": {
            "fields": {
                "ok": {"type": "boolean", "required": True},
            },
        },
    })
    assert c.schema is not None
    assert c.schema.required_field_names == ("ok",)


def test_contract_accepts_outputformat_enum():
    c = OutputContract.from_mapping({
        "name": "x",
        "format": OutputFormat.JSON,
    })
    assert c.format is OutputFormat.JSON


# ===========================================================================
# OutputContract — MARKDOWN_SECTIONS format
# ===========================================================================


def test_markdown_sections_contract_happy_path():
    c = OutputContract.from_mapping({
        "name": "pr-body",
        "format": "markdown_sections",
        "description": "PR description template",
        "sections": [
            {"name": "Summary", "min_body_chars": 10},
            {"name": "Test plan"},
        ],
    })
    assert c.format is OutputFormat.MARKDOWN_SECTIONS
    assert len(c.sections) == 2
    names = [s.name for s in c.sections]
    assert names == ["Summary", "Test plan"]


def test_markdown_sections_requires_at_least_one_section():
    with pytest.raises(OutputContractError):
        OutputContract.from_mapping({
            "name": "x",
            "format": "markdown_sections",
            "sections": [],
        })


def test_markdown_section_heading_level_bounds():
    # Valid range 1-6
    OutputContract.from_mapping({
        "name": "x", "format": "markdown_sections",
        "sections": [{"name": "A", "heading_level": 1}],
    })
    OutputContract.from_mapping({
        "name": "x", "format": "markdown_sections",
        "sections": [{"name": "A", "heading_level": 6}],
    })
    with pytest.raises(OutputContractError):
        OutputContract.from_mapping({
            "name": "x", "format": "markdown_sections",
            "sections": [{"name": "A", "heading_level": 0}],
        })
    with pytest.raises(OutputContractError):
        OutputContract.from_mapping({
            "name": "x", "format": "markdown_sections",
            "sections": [{"name": "A", "heading_level": 7}],
        })


def test_markdown_section_negative_min_body_rejected():
    with pytest.raises(OutputContractError):
        OutputContract.from_mapping({
            "name": "x", "format": "markdown_sections",
            "sections": [{"name": "A", "min_body_chars": -1}],
        })


def test_markdown_section_from_mapping_defaults():
    c = OutputContract.from_mapping({
        "name": "x", "format": "markdown_sections",
        "sections": [{"name": "A"}],
    })
    sec = c.sections[0]
    assert sec.heading_level == 2
    assert sec.required is True
    assert sec.min_body_chars == 0


# ===========================================================================
# OutputContract — CODE_BLOCK format
# ===========================================================================


def test_code_block_requires_fence_language():
    with pytest.raises(OutputContractError):
        OutputContract.from_mapping({
            "name": "x",
            "format": "code_block",
        })


def test_code_block_happy_path():
    c = OutputContract.from_mapping({
        "name": "x",
        "format": "code_block",
        "fence_language": "python",
    })
    assert c.fence_language == "python"


def test_code_block_rejects_weird_fence_lang_chars():
    with pytest.raises(OutputContractError):
        OutputContract.from_mapping({
            "name": "x", "format": "code_block",
            "fence_language": "python; rm -rf /",
        })


# ===========================================================================
# OutputContract — CSV / YAML / PLAIN
# ===========================================================================


def test_csv_contract_minimal():
    c = OutputContract.from_mapping({
        "name": "report",
        "format": "csv",
    })
    assert c.format is OutputFormat.CSV


def test_yaml_contract_minimal():
    c = OutputContract.from_mapping({
        "name": "config",
        "format": "yaml",
    })
    assert c.format is OutputFormat.YAML


def test_plain_contract_minimal():
    c = OutputContract.from_mapping({
        "name": "notes",
        "format": "plain",
    })
    assert c.format is OutputFormat.PLAIN


# ===========================================================================
# Name validation
# ===========================================================================


@pytest.mark.parametrize("name", [
    "UPPERCASE",
    "has space",
    "has.dot",
    "has/slash",
])
def test_bad_contract_name_rejected(name: str):
    with pytest.raises(OutputContractError):
        OutputContract.from_mapping({
            "name": name, "format": "json",
        })


@pytest.mark.parametrize("name", [
    "result", "pr-body", "result_v1", "plugin:custom-format",
])
def test_valid_contract_name(name: str):
    c = OutputContract.from_mapping({
        "name": name, "format": "json",
    })
    assert c.name == name


# ===========================================================================
# Format validation
# ===========================================================================


def test_format_missing_rejected():
    with pytest.raises(OutputContractError):
        OutputContract.from_mapping({"name": "x"})


def test_format_unknown_rejected():
    with pytest.raises(OutputContractError):
        OutputContract.from_mapping({"name": "x", "format": "gibberish"})


def test_format_non_string_or_enum_rejected():
    with pytest.raises(OutputContractError):
        OutputContract.from_mapping({"name": "x", "format": 42})


# ===========================================================================
# Description bounds
# ===========================================================================


def test_description_too_long_rejected():
    with pytest.raises(OutputContractError):
        OutputContract.from_mapping({
            "name": "x", "format": "json",
            "description": "x" * 2500,
        })


def test_description_non_string_rejected():
    with pytest.raises(OutputContractError):
        OutputContract.from_mapping({
            "name": "x", "format": "json",
            "description": 123,
        })


# ===========================================================================
# Length caps
# ===========================================================================


def test_max_length_must_be_positive():
    with pytest.raises(OutputContractError):
        OutputContract.from_mapping({
            "name": "x", "format": "json", "max_length_chars": 0,
        })


def test_min_length_non_negative():
    with pytest.raises(OutputContractError):
        OutputContract.from_mapping({
            "name": "x", "format": "json", "min_length_chars": -1,
        })


def test_min_length_cannot_exceed_max():
    with pytest.raises(OutputContractError):
        OutputContract.from_mapping({
            "name": "x", "format": "json",
            "min_length_chars": 1000, "max_length_chars": 500,
        })


# ===========================================================================
# Extractor hints
# ===========================================================================


def test_extractor_hints_accepted():
    c = OutputContract.from_mapping({
        "name": "x", "format": "plain",
        "extractor_hints": [r"```json\s*\n(.*?)\n```", r"## Summary"],
    })
    assert len(c.extractor_hints) == 2


def test_extractor_hints_non_list_rejected():
    with pytest.raises(OutputContractError):
        OutputContract.from_mapping({
            "name": "x", "format": "plain",
            "extractor_hints": "not-a-list",
        })


def test_extractor_hints_empty_string_rejected():
    with pytest.raises(OutputContractError):
        OutputContract.from_mapping({
            "name": "x", "format": "plain",
            "extractor_hints": [""],
        })


def test_extractor_hints_invalid_regex_rejected():
    with pytest.raises(OutputContractError):
        OutputContract.from_mapping({
            "name": "x", "format": "plain",
            "extractor_hints": ["[invalid"],
        })


# ===========================================================================
# Immutability + equality
# ===========================================================================


def test_contract_is_frozen():
    c = OutputContract.from_mapping({"name": "x", "format": "json"})
    with pytest.raises(Exception):
        c.name = "y"  # type: ignore[misc]


def test_contracts_equal_by_value():
    a = OutputContract.from_mapping({"name": "x", "format": "json"})
    b = OutputContract.from_mapping({"name": "x", "format": "json"})
    assert a == b


# ===========================================================================
# Projection
# ===========================================================================


def test_projection_shape_for_markdown_sections():
    c = OutputContract.from_mapping({
        "name": "pr",
        "format": "markdown_sections",
        "description": "Pull request template",
        "sections": [
            {"name": "Summary", "required": True},
            {"name": "Test plan", "min_body_chars": 20},
        ],
        "extractor_hints": [r"```json\s*(.*?)```"],
    })
    p = c.project()
    assert p["schema_version"] == OUTPUT_CONTRACT_SCHEMA_VERSION
    assert p["format"] == "markdown_sections"
    assert p["description"] == "Pull request template"
    assert [s["name"] for s in p["sections"]] == ["Summary", "Test plan"]
    assert p["extractor_hints_count"] == 1
    assert "extractor_hints" not in p  # raw patterns not exported


def test_projection_with_schema():
    c = OutputContract.from_mapping({
        "name": "x", "format": "json",
        "schema": {
            "fields": {
                "a": {"type": "string", "required": True},
                "b": {"type": "integer"},
            },
        },
    })
    p = c.project()
    assert p["schema"]["required_field_names"] == ["a"]
    assert set(p["schema"]["field_names"]) == {"a", "b"}


def test_projection_no_schema():
    c = OutputContract.from_mapping({"name": "x", "format": "plain"})
    p = c.project()
    assert p["schema"] is None


# ===========================================================================
# Cross-field validation recap
# ===========================================================================


def test_json_contract_without_schema_allowed():
    """Weak contract (no schema) is allowed; Slice 2 validator only
    checks parseability."""
    OutputContract.from_mapping({"name": "x", "format": "json"})


def test_sections_list_non_list_rejected():
    with pytest.raises(OutputContractError):
        OutputContract.from_mapping({
            "name": "x", "format": "markdown_sections",
            "sections": "not-a-list",
        })


def test_section_entry_wrong_type_rejected():
    with pytest.raises(OutputContractError):
        OutputContract.from_mapping({
            "name": "x", "format": "markdown_sections",
            "sections": [42],
        })
