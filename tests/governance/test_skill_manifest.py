"""Slice 1 tests — SkillManifest primitive."""
from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any, Dict

import pytest

from backend.core.ouroboros.governance.skill_manifest import (
    SKILL_MANIFEST_SCHEMA_VERSION,
    SkillArgsError,
    SkillManifest,
    SkillManifestError,
    validate_args,
)


# ===========================================================================
# Schema version
# ===========================================================================


def test_schema_version_stable():
    assert SKILL_MANIFEST_SCHEMA_VERSION == "skill_manifest.v1"


# ===========================================================================
# Happy-path parsing
# ===========================================================================


def _minimal_mapping() -> Dict[str, Any]:
    return {
        "name": "greet",
        "description": "Say hello to the operator",
        "trigger": "Use when the operator says hi",
        "entrypoint": "my_plugin.skills.greet:run",
    }


def test_minimal_manifest_parses():
    m = SkillManifest.from_mapping(_minimal_mapping())
    assert m.name == "greet"
    assert m.description == "Say hello to the operator"
    assert m.trigger == "Use when the operator says hi"
    assert m.entrypoint == "my_plugin.skills.greet:run"
    assert m.version == "0.0.0"
    assert m.plugin_namespace is None
    assert m.qualified_name == "greet"
    assert m.permissions == ()
    assert m.args_schema == {}


def test_full_manifest_parses():
    m = SkillManifest.from_mapping({
        **_minimal_mapping(),
        "plugin_namespace": "ralph-loop",
        "version": "1.2.3",
        "author": "derek@example.com",
        "usage": "Example usage text",
        "permissions": ["read_only", "filesystem_read"],
        "args_schema": {
            "name": {"type": "string", "required": True},
            "count": {"type": "integer", "default": 1, "minimum": 1},
        },
    })
    assert m.plugin_namespace == "ralph-loop"
    assert m.qualified_name == "ralph-loop:greet"
    assert m.version == "1.2.3"
    assert list(m.permissions) == ["read_only", "filesystem_read"]
    assert "name" in m.args_schema


def test_dotted_entrypoint_accepted():
    m = SkillManifest.from_mapping({
        **_minimal_mapping(),
        "entrypoint": "my_plugin.skills.greet.run",
    })
    assert m.entrypoint == "my_plugin.skills.greet.run"


# ===========================================================================
# Validation — required fields
# ===========================================================================


@pytest.mark.parametrize("missing", ["name", "description", "trigger", "entrypoint"])
def test_required_field_missing_rejected(missing: str):
    data = _minimal_mapping()
    data.pop(missing)
    with pytest.raises(SkillManifestError):
        SkillManifest.from_mapping(data)


def test_non_mapping_data_rejected():
    with pytest.raises(SkillManifestError):
        SkillManifest.from_mapping("not a dict")  # type: ignore[arg-type]


def test_empty_name_rejected():
    with pytest.raises(SkillManifestError):
        SkillManifest.from_mapping({**_minimal_mapping(), "name": ""})


# ===========================================================================
# Validation — name format
# ===========================================================================


@pytest.mark.parametrize("name", [
    "UPPERCASE",
    "has space",
    "has.dot",
    "has/slash",
    "-leading-hyphen",
    "way_too_long_" * 10,
])
def test_bad_name_rejected(name: str):
    with pytest.raises(SkillManifestError):
        SkillManifest.from_mapping({**_minimal_mapping(), "name": name})


@pytest.mark.parametrize("name", [
    "greet", "loop", "my-skill", "skill_1",
    "a", "ab", "0leading-digit-allowed",
])
def test_valid_names_accepted(name: str):
    m = SkillManifest.from_mapping({**_minimal_mapping(), "name": name})
    assert m.name == name


# ===========================================================================
# Validation — namespace format
# ===========================================================================


def test_invalid_namespace_rejected():
    with pytest.raises(SkillManifestError):
        SkillManifest.from_mapping({
            **_minimal_mapping(),
            "plugin_namespace": "HAS SPACE",
        })


def test_valid_namespace_accepted():
    m = SkillManifest.from_mapping({
        **_minimal_mapping(),
        "plugin_namespace": "ralph-loop",
    })
    assert m.qualified_name == "ralph-loop:greet"


# ===========================================================================
# Validation — version format
# ===========================================================================


@pytest.mark.parametrize("v", [
    "1", "1.0", "1.0.0", "0.1.2", "v0.1.0", "1.0.0-alpha",
    "1.0.0-rc.1",
])
def test_valid_versions(v: str):
    m = SkillManifest.from_mapping({**_minimal_mapping(), "version": v})
    assert m.version == v


@pytest.mark.parametrize("v", [
    "not-a-version", "1.0.0.0", "latest", "v.1",
])
def test_invalid_versions_rejected(v: str):
    with pytest.raises(SkillManifestError):
        SkillManifest.from_mapping({**_minimal_mapping(), "version": v})


# ===========================================================================
# Validation — entrypoint format
# ===========================================================================


@pytest.mark.parametrize("ep", [
    "",
    "nodots",
    "has spaces.fn",
    "has/slash.fn",
    "1starts_with_digit.fn",
])
def test_invalid_entrypoint_rejected(ep: str):
    with pytest.raises(SkillManifestError):
        SkillManifest.from_mapping({**_minimal_mapping(), "entrypoint": ep})


# ===========================================================================
# Validation — permissions allowlist
# ===========================================================================


def test_unknown_permission_rejected():
    with pytest.raises(SkillManifestError):
        SkillManifest.from_mapping({
            **_minimal_mapping(),
            "permissions": ["admin"],
        })


def test_permissions_dedupe():
    m = SkillManifest.from_mapping({
        **_minimal_mapping(),
        "permissions": ["read_only", "read_only", "network"],
    })
    assert list(m.permissions) == ["read_only", "network"]


def test_permissions_non_list_rejected():
    with pytest.raises(SkillManifestError):
        SkillManifest.from_mapping({
            **_minimal_mapping(),
            "permissions": "read_only",
        })


def test_non_string_permission_rejected():
    with pytest.raises(SkillManifestError):
        SkillManifest.from_mapping({
            **_minimal_mapping(),
            "permissions": [123],
        })


# ===========================================================================
# Validation — args_schema dialect
# ===========================================================================


def test_arg_schema_bad_shape_rejected():
    with pytest.raises(SkillManifestError):
        SkillManifest.from_mapping({
            **_minimal_mapping(),
            "args_schema": "not a dict",
        })


def test_arg_spec_must_be_mapping():
    with pytest.raises(SkillManifestError):
        SkillManifest.from_mapping({
            **_minimal_mapping(),
            "args_schema": {"x": "not a mapping"},
        })


def test_arg_unknown_type_rejected():
    with pytest.raises(SkillManifestError):
        SkillManifest.from_mapping({
            **_minimal_mapping(),
            "args_schema": {"x": {"type": "color"}},
        })


def test_arg_unknown_key_rejected():
    with pytest.raises(SkillManifestError):
        SkillManifest.from_mapping({
            **_minimal_mapping(),
            "args_schema": {"x": {"type": "string", "nosuchkey": 1}},
        })


def test_arg_enum_empty_rejected():
    with pytest.raises(SkillManifestError):
        SkillManifest.from_mapping({
            **_minimal_mapping(),
            "args_schema": {"x": {"type": "string", "enum": []}},
        })


def test_arg_name_with_space_rejected():
    with pytest.raises(SkillManifestError):
        SkillManifest.from_mapping({
            **_minimal_mapping(),
            "args_schema": {"has space": {"type": "string"}},
        })


# ===========================================================================
# Immutability + equality
# ===========================================================================


def test_manifest_is_frozen():
    m = SkillManifest.from_mapping(_minimal_mapping())
    with pytest.raises(Exception):
        m.name = "other"  # type: ignore[misc]


def test_manifests_equal_by_value():
    a = SkillManifest.from_mapping(_minimal_mapping())
    b = SkillManifest.from_mapping(_minimal_mapping())
    assert a == b


def test_manifest_projection_shape():
    m = SkillManifest.from_mapping({
        **_minimal_mapping(),
        "plugin_namespace": "rp",
        "permissions": ["read_only"],
    })
    p = m.project()
    assert p["schema_version"] == "skill_manifest.v1"
    assert p["qualified_name"] == "rp:greet"
    assert p["permissions"] == ["read_only"]


# ===========================================================================
# YAML file loading
# ===========================================================================


def test_yaml_file_round_trip(tmp_path: Path):
    yaml_text = textwrap.dedent("""
    name: greet
    description: say hello
    trigger: when operator says hi
    entrypoint: my_plugin.greet:run
    version: 1.0.0
    permissions:
      - read_only
    args_schema:
      who:
        type: string
        required: true
    """).strip()
    path = tmp_path / "greet.yaml"
    path.write_text(yaml_text)
    m = SkillManifest.from_yaml_file(path)
    assert m.name == "greet"
    assert m.path is not None
    assert m.path.resolve() == path.resolve()


def test_yaml_missing_file_rejected(tmp_path: Path):
    with pytest.raises(SkillManifestError):
        SkillManifest.from_yaml_file(tmp_path / "nope.yaml")


def test_yaml_empty_file_rejected(tmp_path: Path):
    path = tmp_path / "empty.yaml"
    path.write_text("")
    with pytest.raises(SkillManifestError):
        SkillManifest.from_yaml_file(path)


def test_yaml_malformed_rejected(tmp_path: Path):
    path = tmp_path / "bad.yaml"
    path.write_text("not: valid: yaml: [[[")
    with pytest.raises(SkillManifestError):
        SkillManifest.from_yaml_file(path)


# ===========================================================================
# validate_args — runtime arg check
# ===========================================================================


def test_validate_args_required_missing():
    schema = {"name": {"type": "string", "required": True}}
    with pytest.raises(SkillArgsError):
        validate_args(schema, {})


def test_validate_args_applies_default():
    schema = {"count": {"type": "integer", "default": 3}}
    result = validate_args(schema, {})
    assert result == {"count": 3}


def test_validate_args_rejects_unknown_arg():
    schema = {"name": {"type": "string"}}
    with pytest.raises(SkillArgsError):
        validate_args(schema, {"other": 1})


def test_validate_args_type_check():
    schema = {"count": {"type": "integer"}}
    with pytest.raises(SkillArgsError):
        validate_args(schema, {"count": "not-a-number"})


def test_validate_args_bool_not_integer():
    """bool is technically an int subclass — guarded explicitly."""
    schema = {"count": {"type": "integer"}}
    with pytest.raises(SkillArgsError):
        validate_args(schema, {"count": True})


def test_validate_args_enum_check():
    schema = {"color": {"type": "string", "enum": ["red", "blue"]}}
    with pytest.raises(SkillArgsError):
        validate_args(schema, {"color": "green"})


def test_validate_args_minimum():
    schema = {"count": {"type": "integer", "minimum": 1}}
    with pytest.raises(SkillArgsError):
        validate_args(schema, {"count": 0})


def test_validate_args_maximum():
    schema = {"count": {"type": "integer", "maximum": 10}}
    with pytest.raises(SkillArgsError):
        validate_args(schema, {"count": 11})


def test_validate_args_string_length():
    schema = {"name": {"type": "string", "min_length": 2, "max_length": 5}}
    with pytest.raises(SkillArgsError):
        validate_args(schema, {"name": "x"})
    with pytest.raises(SkillArgsError):
        validate_args(schema, {"name": "way-too-long"})
    out = validate_args(schema, {"name": "abc"})
    assert out == {"name": "abc"}


def test_validate_args_returns_new_dict():
    schema = {"x": {"type": "integer", "default": 1}}
    caller_args: Dict[str, Any] = {}
    result = validate_args(schema, caller_args)
    assert caller_args == {}
    assert result == {"x": 1}


def test_validate_args_non_mapping_rejected():
    with pytest.raises(SkillArgsError):
        validate_args({"x": {"type": "string"}}, "not a mapping")  # type: ignore[arg-type]


def test_validate_args_empty_schema_and_empty_args():
    assert validate_args({}, {}) == {}
