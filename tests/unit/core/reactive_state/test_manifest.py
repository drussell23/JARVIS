"""Tests for the declarative manifest tying ownership, schemas, and consistency groups.

Covers structural integrity of OWNERSHIP_RULES, KEY_SCHEMAS, and
CONSISTENCY_GROUPS, as well as cross-referencing between all three
registries and validation of every schema's default against its
own constraints.
"""
from __future__ import annotations

import pytest

from backend.core.reactive_state.manifest import (
    CONSISTENCY_GROUPS,
    ConsistencyGroup,
    KEY_SCHEMAS,
    OWNERSHIP_RULES,
    build_ownership_registry,
    build_schema_registry,
)
from backend.core.reactive_state.ownership import OwnershipRegistry
from backend.core.reactive_state.schemas import SchemaRegistry


# -- Test 1: OWNERSHIP_RULES structural integrity ----------------------------


class TestOwnershipRulesStructure:
    """OWNERSHIP_RULES must declare enough prefixes to cover the codebase."""

    def test_ownership_rules_has_at_least_six_entries(self):
        assert len(OWNERSHIP_RULES) >= 6


# -- Test 2: KEY_SCHEMAS structural integrity --------------------------------


class TestKeySchemasStructure:
    """KEY_SCHEMAS must declare enough keys to cover the codebase."""

    def test_key_schemas_has_at_least_eleven_entries(self):
        assert len(KEY_SCHEMAS) >= 11


# -- Test 3: build_ownership_registry returns frozen registry ----------------


class TestBuildOwnershipRegistry:
    """build_ownership_registry must produce a frozen OwnershipRegistry."""

    def test_returns_frozen_ownership_registry(self):
        registry = build_ownership_registry()
        assert isinstance(registry, OwnershipRegistry)
        with pytest.raises(RuntimeError):
            from backend.core.reactive_state.ownership import OwnershipRule
            registry.register(
                OwnershipRule("test.", "test_writer", "Should fail")
            )


# -- Test 4: No ambiguous overlaps in ownership rules -----------------------


class TestNoAmbiguousOverlaps:
    """OWNERSHIP_RULES must not contain duplicate prefixes with different owners."""

    def test_no_ambiguous_overlaps(self):
        registry = build_ownership_registry()
        errors = registry.validate_no_ambiguous_overlaps()
        assert errors == [], f"Ambiguous overlaps found: {errors}"


# -- Test 5: build_schema_registry returns SchemaRegistry --------------------


class TestBuildSchemaRegistry:
    """build_schema_registry must produce a SchemaRegistry."""

    def test_returns_schema_registry(self):
        registry = build_schema_registry()
        assert isinstance(registry, SchemaRegistry)


# -- Test 6: Every schema key has an owner -----------------------------------


class TestSchemaOwnershipCrossCheck:
    """Every key defined in KEY_SCHEMAS must have a matching owner in OWNERSHIP_RULES."""

    def test_every_schema_key_has_owner(self):
        ownership = build_ownership_registry()
        schema_reg = build_schema_registry()
        orphans = []
        for key in schema_reg.all_keys():
            owner = ownership.resolve_owner(key)
            if owner is None:
                orphans.append(key)
        assert orphans == [], f"Keys without owners: {orphans}"


# -- Test 7: Consistency groups reference only valid keys --------------------


class TestConsistencyGroupsValidity:
    """Every key referenced in a ConsistencyGroup must exist in KEY_SCHEMAS."""

    def test_consistency_groups_reference_valid_keys(self):
        schema_reg = build_schema_registry()
        all_keys = schema_reg.all_keys()
        invalid: list[tuple[str, str]] = []
        for group in CONSISTENCY_GROUPS:
            for key in group.keys:
                if key not in all_keys:
                    invalid.append((group.name, key))
        assert invalid == [], f"Invalid keys in consistency groups: {invalid}"


# -- Test 8: lifecycle keys owned by supervisor ------------------------------


class TestLifecycleOwnership:
    """All lifecycle.* keys must be owned by 'supervisor'."""

    def test_lifecycle_keys_owned_by_supervisor(self):
        ownership = build_ownership_registry()
        schema_reg = build_schema_registry()
        lifecycle_keys = [k for k in schema_reg.all_keys() if k.startswith("lifecycle.")]
        assert len(lifecycle_keys) > 0, "No lifecycle keys found"
        for key in lifecycle_keys:
            owner = ownership.resolve_owner(key)
            assert owner == "supervisor", (
                f"Expected 'supervisor' for '{key}', got '{owner}'"
            )


# -- Test 9: gcp keys owned by gcp_controller -------------------------------


class TestGcpOwnership:
    """All gcp.* keys must be owned by 'gcp_controller'."""

    def test_gcp_keys_owned_by_gcp_controller(self):
        ownership = build_ownership_registry()
        schema_reg = build_schema_registry()
        gcp_keys = [k for k in schema_reg.all_keys() if k.startswith("gcp.")]
        assert len(gcp_keys) > 0, "No gcp keys found"
        for key in gcp_keys:
            owner = ownership.resolve_owner(key)
            assert owner == "gcp_controller", (
                f"Expected 'gcp_controller' for '{key}', got '{owner}'"
            )


# -- Test 10: Every schema's default passes its own validation ---------------


class TestSchemaDefaultsPassValidation:
    """Every schema's default value must pass its own validate() method."""

    def test_every_default_passes_validation(self):
        failures: list[tuple[str, str]] = []
        for schema in KEY_SCHEMAS:
            error = schema.validate(schema.default)
            if error is not None:
                failures.append((schema.key, error))
        assert failures == [], f"Schema defaults that fail validation: {failures}"
