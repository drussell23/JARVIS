"""Pricing Oracle — regression spine.

Pins the family-pattern fallback that closes the Static Pricing
Blindspot diagnosed in soak #6 (BG-route models with no API-side
pricing got SPECULATIVE-quarantined by has_ambiguous_metadata()).

Coverage map:
  §1   Master flag asymmetric env semantics
  §2   PricingPattern frozen + value-equality
  §3   Registry idempotency + overwrite contract
  §4   Pattern matching (case-insensitive fnmatch)
  §5   First-match-wins (specific before generic)
  §6   Resolution cache (positive + negative)
  §7   Cache invalidation on registry mutation
  §8   Seed patterns cover the documented families
  §9   Resolver NEVER raises
  §10  Authority invariants (no forbidden imports)
  §11  Master-off short-circuits to None
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import pricing_oracle
from backend.core.ouroboros.governance.pricing_oracle import (
    PricingPattern,
    cache_size,
    list_pricing_patterns,
    pricing_oracle_enabled,
    register_pricing_pattern,
    reset_for_tests,
    resolve_pricing,
    unregister_pricing_pattern,
)


@pytest.fixture(autouse=True)
def _reset():
    reset_for_tests()
    yield
    reset_for_tests()


# ===========================================================================
# §1 — Master flag
# ===========================================================================


def test_master_flag_default_true(monkeypatch) -> None:
    monkeypatch.delenv("JARVIS_PRICING_ORACLE_ENABLED", raising=False)
    assert pricing_oracle_enabled() is True


@pytest.mark.parametrize("val", ["", " ", "  ", "\t"])
def test_master_flag_empty_default_true(monkeypatch, val) -> None:
    monkeypatch.setenv("JARVIS_PRICING_ORACLE_ENABLED", val)
    assert pricing_oracle_enabled() is True


@pytest.mark.parametrize(
    "val", ["0", "false", "no", "off", "False", "NO", "garbage"],
)
def test_master_flag_falsy_disables(monkeypatch, val) -> None:
    monkeypatch.setenv("JARVIS_PRICING_ORACLE_ENABLED", val)
    assert pricing_oracle_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE"])
def test_master_flag_explicit_true(monkeypatch, val) -> None:
    monkeypatch.setenv("JARVIS_PRICING_ORACLE_ENABLED", val)
    assert pricing_oracle_enabled() is True


# ===========================================================================
# §2 — PricingPattern dataclass
# ===========================================================================


def test_pricing_pattern_frozen() -> None:
    p = PricingPattern(
        pattern_kind="x", glob_pattern="*x*",
        pricing_in_per_m_usd=1.0, pricing_out_per_m_usd=2.0,
    )
    with pytest.raises((AttributeError, Exception)):
        p.pattern_kind = "y"  # type: ignore[misc]


def test_pricing_pattern_equality() -> None:
    a = PricingPattern("k", "*g*", 1.0, 2.0, "d")
    b = PricingPattern("k", "*g*", 1.0, 2.0, "d")
    assert a == b


def test_pricing_pattern_hashable() -> None:
    p = PricingPattern("k", "*g*", 1.0, 2.0)
    hash(p)  # must not raise


# ===========================================================================
# §3 — Registry mutation contract
# ===========================================================================


def test_register_appends_in_order() -> None:
    reset_for_tests()
    pricing_oracle._REGISTRY.clear()
    register_pricing_pattern(PricingPattern("a", "*a*", 1, 1))
    register_pricing_pattern(PricingPattern("b", "*b*", 1, 1))
    register_pricing_pattern(PricingPattern("c", "*c*", 1, 1))
    kinds = [p.pattern_kind for p in list_pricing_patterns()]
    assert kinds == ["a", "b", "c"]


def test_register_idempotent_on_identical() -> None:
    reset_for_tests()
    pricing_oracle._REGISTRY.clear()
    p = PricingPattern("x", "*x*", 1.0, 2.0)
    register_pricing_pattern(p)
    register_pricing_pattern(p)
    register_pricing_pattern(p)
    assert len(list_pricing_patterns()) == 1


def test_register_rejects_different_content_without_overwrite() -> None:
    reset_for_tests()
    pricing_oracle._REGISTRY.clear()
    register_pricing_pattern(PricingPattern("k", "*x*", 1.0, 2.0))
    register_pricing_pattern(PricingPattern("k", "*y*", 9.0, 9.0))
    out = list_pricing_patterns()
    assert len(out) == 1
    # Original retained
    assert out[0].glob_pattern == "*x*"


def test_register_overwrite_replaces_in_place() -> None:
    reset_for_tests()
    pricing_oracle._REGISTRY.clear()
    register_pricing_pattern(PricingPattern("a", "*a*", 1, 1))
    register_pricing_pattern(PricingPattern("k", "*x*", 1.0, 2.0))
    register_pricing_pattern(PricingPattern("c", "*c*", 1, 1))
    register_pricing_pattern(
        PricingPattern("k", "*y*", 9.0, 9.0), overwrite=True,
    )
    out = list_pricing_patterns()
    kinds = [p.pattern_kind for p in out]
    # Order preserved
    assert kinds == ["a", "k", "c"]
    # Content replaced
    middle = [p for p in out if p.pattern_kind == "k"][0]
    assert middle.glob_pattern == "*y*"
    assert middle.pricing_in_per_m_usd == 9.0


def test_register_rejects_invalid_inputs() -> None:
    reset_for_tests()
    pricing_oracle._REGISTRY.clear()
    # Empty kind
    register_pricing_pattern(PricingPattern("", "*x*", 1, 1))
    # Whitespace kind
    register_pricing_pattern(PricingPattern("  ", "*x*", 1, 1))
    # Empty glob
    register_pricing_pattern(PricingPattern("k", "", 1, 1))
    # Negative price
    register_pricing_pattern(PricingPattern("k", "*x*", -1, 1))
    register_pricing_pattern(PricingPattern("k", "*x*", 1, -1))
    # Non-PricingPattern
    register_pricing_pattern("not a pattern")  # type: ignore[arg-type]
    register_pricing_pattern(None)  # type: ignore[arg-type]
    assert list_pricing_patterns() == ()


def test_unregister_removes() -> None:
    register_pricing_pattern(PricingPattern("custom", "*foo*", 1.0, 2.0))
    assert any(
        p.pattern_kind == "custom" for p in list_pricing_patterns()
    )
    assert unregister_pricing_pattern("custom") is True
    assert not any(
        p.pattern_kind == "custom" for p in list_pricing_patterns()
    )


def test_unregister_returns_false_for_missing() -> None:
    assert unregister_pricing_pattern("never_registered_xyz") is False


def test_unregister_handles_invalid() -> None:
    assert unregister_pricing_pattern("") is False
    assert unregister_pricing_pattern("   ") is False


# ===========================================================================
# §4 — Pattern matching (case-insensitive fnmatch)
# ===========================================================================


def test_resolve_matches_case_insensitive() -> None:
    reset_for_tests()
    pricing_oracle._REGISTRY.clear()
    register_pricing_pattern(
        PricingPattern("test", "*qwen*397b*", 0.10, 0.40),
    )
    # Various casings of the same id
    assert resolve_pricing("Qwen-3.5-397B-A17B") == (0.10, 0.40)
    assert resolve_pricing("QWEN-3.5-397B-A17B") == (0.10, 0.40)
    assert resolve_pricing("qwen-3.5-397b-a17b") == (0.10, 0.40)


def test_resolve_returns_none_on_miss() -> None:
    reset_for_tests()
    pricing_oracle._REGISTRY.clear()
    register_pricing_pattern(
        PricingPattern("k", "*nothing-matches*", 1, 1),
    )
    assert resolve_pricing("totally-unknown-model-v1") is None


def test_resolve_rejects_invalid_input() -> None:
    assert resolve_pricing("") is None
    assert resolve_pricing("   ") is None
    assert resolve_pricing(None) is None  # type: ignore[arg-type]
    assert resolve_pricing(12345) is None  # type: ignore[arg-type]


# ===========================================================================
# §5 — First-match-wins (specific before generic)
# ===========================================================================


def test_first_match_wins_specific_before_generic() -> None:
    reset_for_tests()
    pricing_oracle._REGISTRY.clear()
    # Specific pattern registered first
    register_pricing_pattern(
        PricingPattern("specific", "*qwen*397b*", 0.10, 0.40),
    )
    # Generic family fallback registered second
    register_pricing_pattern(
        PricingPattern("generic", "*qwen*", 0.20, 0.60),
    )
    # 397B should match specific
    assert resolve_pricing("qwen-3.5-397b-a17b") == (0.10, 0.40)
    # Other Qwen falls through to generic
    assert resolve_pricing("qwen-72b") == (0.20, 0.60)


def test_registration_order_determines_priority() -> None:
    """If generic registers first, even specific ids match generic."""
    reset_for_tests()
    pricing_oracle._REGISTRY.clear()
    # Generic FIRST (wrong order on purpose)
    register_pricing_pattern(
        PricingPattern("generic", "*qwen*", 0.20, 0.60),
    )
    # Specific second
    register_pricing_pattern(
        PricingPattern("specific", "*qwen*397b*", 0.10, 0.40),
    )
    # 397B id matches generic first
    assert resolve_pricing("qwen-397b") == (0.20, 0.60)


# ===========================================================================
# §6 — Resolution cache
# ===========================================================================


def test_cache_returns_same_result_on_repeat() -> None:
    reset_for_tests()
    pricing_oracle._REGISTRY.clear()
    register_pricing_pattern(PricingPattern("k", "*foo*", 1.0, 2.0))
    r1 = resolve_pricing("foo-bar")
    r2 = resolve_pricing("foo-bar")
    assert r1 == r2 == (1.0, 2.0)


def test_cache_caches_negative_results() -> None:
    reset_for_tests()
    pricing_oracle._REGISTRY.clear()
    register_pricing_pattern(PricingPattern("k", "*matches-nothing*", 1, 1))
    assert cache_size() == 0
    assert resolve_pricing("not-a-match") is None
    # Negative cached
    assert cache_size() == 1
    assert resolve_pricing("not-a-match") is None
    # Still 1 — not re-walked
    assert cache_size() == 1


def test_cache_returns_cached_value_after_registry_unchanged() -> None:
    reset_for_tests()
    pricing_oracle._REGISTRY.clear()
    register_pricing_pattern(PricingPattern("k", "*foo*", 1.0, 2.0))
    assert resolve_pricing("foo-bar") == (1.0, 2.0)
    n_after_first = cache_size()
    resolve_pricing("foo-bar")
    resolve_pricing("foo-bar")
    # Cache size unchanged after repeat hits
    assert cache_size() == n_after_first


# ===========================================================================
# §7 — Cache invalidation on registry mutation
# ===========================================================================


def test_cache_invalidated_on_register() -> None:
    reset_for_tests()
    pricing_oracle._REGISTRY.clear()
    # Cache a negative result
    assert resolve_pricing("foo-bar") is None
    assert cache_size() >= 1
    # Now register a matching pattern — cache must be invalidated
    register_pricing_pattern(PricingPattern("k", "*foo*", 1.0, 2.0))
    # Subsequent lookup re-walks and finds the new match
    assert resolve_pricing("foo-bar") == (1.0, 2.0)


def test_cache_invalidated_on_unregister() -> None:
    reset_for_tests()
    pricing_oracle._REGISTRY.clear()
    register_pricing_pattern(PricingPattern("k", "*foo*", 1.0, 2.0))
    assert resolve_pricing("foo-bar") == (1.0, 2.0)
    unregister_pricing_pattern("k")
    # Cache flushed → next lookup misses cleanly
    assert resolve_pricing("foo-bar") is None


def test_cache_invalidated_on_overwrite() -> None:
    reset_for_tests()
    pricing_oracle._REGISTRY.clear()
    register_pricing_pattern(PricingPattern("k", "*foo*", 1.0, 2.0))
    assert resolve_pricing("foo-bar") == (1.0, 2.0)
    register_pricing_pattern(
        PricingPattern("k", "*foo*", 9.0, 9.0), overwrite=True,
    )
    # New price returned
    assert resolve_pricing("foo-bar") == (9.0, 9.0)


# ===========================================================================
# §8 — Seed patterns cover the documented families
# ===========================================================================


def test_seed_qwen_397b_resolves() -> None:
    """Soak #6 root-cause case: Qwen 3.5 397B with no API pricing."""
    out = resolve_pricing("Qwen-3.5-397B-A17B")
    assert out == (0.10, 0.40)


def test_seed_qwen_397b_alt_id_format() -> None:
    out = resolve_pricing("qwen-3.5-397b-instruct")
    assert out == (0.10, 0.40)


def test_seed_qwen_72b_resolves() -> None:
    out = resolve_pricing("Qwen-3.5-72B")
    assert out == (0.30, 0.90)


def test_seed_qwen_generic_fallback() -> None:
    """An unknown Qwen variant falls through to the generic pattern."""
    out = resolve_pricing("qwen-some-unknown-variant")
    assert out == (0.20, 0.60)


def test_seed_deepseek_v3_resolves() -> None:
    out = resolve_pricing("deepseek-v3-chat")
    assert out == (0.27, 1.10)


def test_seed_llama_3_70b_resolves() -> None:
    out = resolve_pricing("Llama-3-70B-Instruct")
    assert out == (0.59, 0.79)


def test_seed_llama_3_8b_resolves() -> None:
    out = resolve_pricing("llama-3-8b-instruct")
    assert out == (0.07, 0.07)


def test_seed_gpt_oss_resolves() -> None:
    out = resolve_pricing("gpt-oss-20b")
    assert out == (0.10, 0.40)


def test_seed_mistral_resolves() -> None:
    out = resolve_pricing("mistral-large-2")
    assert out == (0.50, 1.50)


def test_seed_unrelated_model_returns_none() -> None:
    out = resolve_pricing("anthropic-claude-99-internal")
    assert out is None


def test_reset_re_seeds() -> None:
    reset_for_tests()
    # All seed patterns back
    n_seed = len(list_pricing_patterns())
    assert n_seed >= 11  # at least the 11 documented seeds
    # Wipe and re-seed
    pricing_oracle._REGISTRY.clear()
    assert list_pricing_patterns() == ()
    reset_for_tests()
    assert len(list_pricing_patterns()) == n_seed


# ===========================================================================
# §9 — NEVER raises
# ===========================================================================


def test_resolve_never_raises_on_corrupt_pattern() -> None:
    """Even if a pattern has malformed glob, resolver must not raise."""
    reset_for_tests()
    pricing_oracle._REGISTRY.clear()
    # Force a malformed pattern in directly (bypassing register_*
    # validation) to test the resolver's defensive try/except
    bad = PricingPattern.__new__(PricingPattern)
    object.__setattr__(bad, "pattern_kind", "bad")
    object.__setattr__(bad, "glob_pattern", "[invalid")  # malformed glob
    object.__setattr__(bad, "pricing_in_per_m_usd", 1.0)
    object.__setattr__(bad, "pricing_out_per_m_usd", 2.0)
    object.__setattr__(bad, "description", "")
    object.__setattr__(
        bad, "schema_version", pricing_oracle.PRICING_ORACLE_SCHEMA_VERSION,
    )
    pricing_oracle._REGISTRY.append(bad)
    pricing_oracle._invalidate_cache()
    # Must not raise
    resolve_pricing("anything")  # noqa


def test_register_never_raises_on_malformed_input() -> None:
    """Defensive paths must swallow all exceptions."""
    register_pricing_pattern(None)  # type: ignore[arg-type]
    register_pricing_pattern(42)  # type: ignore[arg-type]
    register_pricing_pattern("string")  # type: ignore[arg-type]
    register_pricing_pattern({"not": "a pattern"})  # type: ignore[arg-type]


# ===========================================================================
# §10 — Authority invariants (AST-pinned)
# ===========================================================================


_FORBIDDEN_IMPORTS = (
    "backend.core.ouroboros.governance.orchestrator",
    "backend.core.ouroboros.governance.phase_runners",
    "backend.core.ouroboros.governance.candidate_generator",
    "backend.core.ouroboros.governance.iron_gate",
    "backend.core.ouroboros.governance.change_engine",
    "backend.core.ouroboros.governance.policy",
    "backend.core.ouroboros.governance.semantic_guardian",
)


def test_authority_no_forbidden_imports() -> None:
    src = Path(inspect.getfile(pricing_oracle)).read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                for forbidden in _FORBIDDEN_IMPORTS:
                    assert forbidden not in alias.name, (
                        f"forbidden import: {alias.name}"
                    )
        elif isinstance(node, ast.ImportFrom) and node.module:
            for forbidden in _FORBIDDEN_IMPORTS:
                assert forbidden not in node.module, (
                    f"forbidden import: {node.module}"
                )


def test_authority_pure_stdlib_only() -> None:
    """The oracle module imports only stdlib + typing — no third-party,
    no JARVIS modules."""
    src = Path(inspect.getfile(pricing_oracle)).read_text()
    tree = ast.parse(src)
    allowed_roots = {"fnmatch", "logging", "os", "threading",
                     "dataclasses", "typing", "__future__"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                assert root in allowed_roots, (
                    f"non-stdlib import: {alias.name}"
                )
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            assert root in allowed_roots, (
                f"non-stdlib import: {node.module}"
            )


# ===========================================================================
# §11 — Master-off short-circuits
# ===========================================================================


def test_master_off_returns_none_for_known_seed(monkeypatch) -> None:
    """When the master flag is off, even seed-pattern hits return None."""
    monkeypatch.setenv("JARVIS_PRICING_ORACLE_ENABLED", "false")
    # Without the flag, even a seed pattern that would otherwise match
    # returns None — caller falls back to the legacy "no pricing →
    # ambiguous" path.
    assert resolve_pricing("Qwen-3.5-397B-A17B") is None


def test_master_off_does_not_populate_cache(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_PRICING_ORACLE_ENABLED", "false")
    reset_for_tests()
    n_before = cache_size()
    resolve_pricing("Qwen-3.5-397B-A17B")
    resolve_pricing("anything")
    # Master-off short-circuit happens BEFORE cache write
    assert cache_size() == n_before
