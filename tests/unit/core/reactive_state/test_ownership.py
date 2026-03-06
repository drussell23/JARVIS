"""Tests for the OwnershipRule and OwnershipRegistry.

Covers frozen-ness, prefix resolution (exact and longest-match),
ownership checking (pass, fail, undeclared), ambiguity detection,
freeze semantics, and all_prefixes enumeration.
"""
from __future__ import annotations

import pytest

from backend.core.reactive_state.ownership import (
    OwnershipRegistry,
    OwnershipRule,
)


# -- Helpers ----------------------------------------------------------------


def _rule(prefix: str = "gcp.", writer: str = "gcp_controller", desc: str = "") -> OwnershipRule:
    return OwnershipRule(
        key_prefix=prefix,
        writer_domain=writer,
        description=desc or f"Owner of '{prefix}' keys",
    )


def _populated_registry() -> OwnershipRegistry:
    """Return a registry with several non-overlapping rules."""
    reg = OwnershipRegistry()
    reg.register(_rule("gcp.", "gcp_controller"))
    reg.register(_rule("gcp.node.", "gcp_node_manager"))
    reg.register(_rule("audio.", "voice_orchestrator"))
    reg.register(_rule("prime.", "prime_router"))
    return reg


# -- TestOwnershipRule ------------------------------------------------------


class TestOwnershipRule:
    """OwnershipRule is a frozen dataclass."""

    def test_ownership_rule_is_frozen(self):
        rule = _rule()
        with pytest.raises(AttributeError):
            rule.key_prefix = "something_else"  # type: ignore[misc]


# -- TestResolveOwner -------------------------------------------------------


class TestResolveOwner:
    """resolve_owner returns the writer_domain for the best prefix match."""

    def test_resolve_owner_exact_prefix_match(self):
        reg = _populated_registry()
        assert reg.resolve_owner("audio.active") == "voice_orchestrator"

    def test_resolve_owner_longest_prefix_match(self):
        reg = _populated_registry()
        # "gcp.node.status" matches both "gcp." and "gcp.node."
        # Longest prefix "gcp.node." should win.
        assert reg.resolve_owner("gcp.node.status") == "gcp_node_manager"

    def test_resolve_owner_unknown_key_returns_none(self):
        reg = _populated_registry()
        assert reg.resolve_owner("unknown.key") is None


# -- TestCheckOwnership -----------------------------------------------------


class TestCheckOwnership:
    """check_ownership validates writer against resolved owner."""

    def test_check_ownership_pass(self):
        reg = _populated_registry()
        assert reg.check_ownership("prime.endpoint", "prime_router") is True

    def test_check_ownership_fail_wrong_writer(self):
        reg = _populated_registry()
        assert reg.check_ownership("prime.endpoint", "rogue_writer") is False

    def test_check_ownership_undeclared_key_fails(self):
        reg = _populated_registry()
        assert reg.check_ownership("unknown.key", "any_writer") is False


# -- TestValidateNoAmbiguousOverlaps ----------------------------------------


class TestValidateNoAmbiguousOverlaps:
    """validate_no_ambiguous_overlaps detects duplicate prefixes with different owners."""

    def test_clean_registry_has_no_overlaps(self):
        reg = _populated_registry()
        errors = reg.validate_no_ambiguous_overlaps()
        assert errors == []

    def test_detects_duplicate_prefix_with_different_owners(self):
        reg = OwnershipRegistry()
        reg.register(_rule("gcp.", "gcp_controller"))
        reg.register(_rule("gcp.", "rogue_controller"))
        errors = reg.validate_no_ambiguous_overlaps()
        assert len(errors) == 1
        assert "gcp." in errors[0]


# -- TestFreeze -------------------------------------------------------------


class TestFreeze:
    """freeze() prevents further registration."""

    def test_freeze_prevents_registration(self):
        reg = OwnershipRegistry()
        reg.register(_rule("audio.", "voice_orchestrator"))
        reg.freeze()
        with pytest.raises(RuntimeError):
            reg.register(_rule("prime.", "prime_router"))


# -- TestAllPrefixes --------------------------------------------------------


class TestAllPrefixes:
    """all_prefixes returns the set of registered key prefixes."""

    def test_all_prefixes_returns_correct_set(self):
        reg = _populated_registry()
        expected = {"gcp.", "gcp.node.", "audio.", "prime."}
        assert reg.all_prefixes() == expected
