"""
Tests for backend.hive.manifesto_slices

Validates:
  - Every PersonaIntent maps to a non-empty slice (>50 chars)
  - Content-specific keyword checks for each intent slice
  - All 3 personas have role prefixes (>100 chars each)
  - Role-specific content in each persona prefix
  - Sanitization guards in every prefix
"""

from __future__ import annotations

import pytest

from backend.hive.manifesto_slices import ROLE_PREFIXES, get_manifesto_slice
from backend.hive.thread_models import PersonaIntent


# ====================================================================
# Layer B — Manifesto slices
# ====================================================================


class TestManifestoSliceCoverage:
    """Every PersonaIntent maps to a non-empty string >50 chars."""

    @pytest.mark.parametrize("intent", list(PersonaIntent))
    def test_every_intent_has_nonempty_slice(self, intent: PersonaIntent) -> None:
        result = get_manifesto_slice(intent)
        assert isinstance(result, str)
        assert len(result) > 50, (
            f"Slice for {intent.name} too short ({len(result)} chars)"
        )


class TestObserveSlice:
    """OBSERVE slice references observability/transparency."""

    def test_references_observability(self) -> None:
        text = get_manifesto_slice(PersonaIntent.OBSERVE).lower()
        assert "observability" in text or "transparency" in text or "transparent" in text


class TestProposeSlice:
    """PROPOSE slice references boundary/routing."""

    def test_references_boundary(self) -> None:
        text = get_manifesto_slice(PersonaIntent.PROPOSE).lower()
        assert "boundary" in text or "boundaries" in text

    def test_references_routing(self) -> None:
        text = get_manifesto_slice(PersonaIntent.PROPOSE).lower()
        assert "routing" in text


class TestValidateSlice:
    """VALIDATE slice references Iron Gate/AST/execution authority."""

    def test_references_iron_gate(self) -> None:
        text = get_manifesto_slice(PersonaIntent.VALIDATE).lower()
        assert "iron gate" in text

    def test_references_ast_or_execution(self) -> None:
        text = get_manifesto_slice(PersonaIntent.VALIDATE).lower()
        assert "ast" in text or "execution authority" in text


class TestChallengeSlice:
    """CHALLENGE slice references sovereignty/zero-trust/privacy."""

    def test_references_sovereignty(self) -> None:
        text = get_manifesto_slice(PersonaIntent.CHALLENGE).lower()
        assert "sovereignty" in text

    def test_references_zero_trust(self) -> None:
        text = get_manifesto_slice(PersonaIntent.CHALLENGE).lower()
        assert "zero-trust" in text or "zero trust" in text

    def test_references_privacy(self) -> None:
        text = get_manifesto_slice(PersonaIntent.CHALLENGE).lower()
        assert "privacy" in text or "private" in text


# ====================================================================
# Layer A — Role prefixes
# ====================================================================


class TestRolePrefixCoverage:
    """All 3 personas have prefixes >100 chars."""

    @pytest.mark.parametrize("persona", ["jarvis", "j_prime", "reactor"])
    def test_prefix_exists_and_long_enough(self, persona: str) -> None:
        prefix = ROLE_PREFIXES[persona]
        assert isinstance(prefix, str)
        assert len(prefix) > 100, (
            f"Prefix for {persona} too short ({len(prefix)} chars)"
        )


class TestJarvisPrefix:
    """JARVIS prefix contains body/senses."""

    def test_contains_body_senses(self) -> None:
        text = ROLE_PREFIXES["jarvis"].lower()
        assert "body" in text and "senses" in text


class TestJPrimePrefix:
    """J-Prime prefix contains mind/cognition."""

    def test_contains_mind_cognition(self) -> None:
        text = ROLE_PREFIXES["j_prime"].lower()
        assert "mind" in text and "cognition" in text


class TestReactorPrefix:
    """Reactor prefix disclaims Iron Gate ('not the deterministic iron gate' or 'advisory')."""

    def test_disclaims_iron_gate(self) -> None:
        text = ROLE_PREFIXES["reactor"].lower()
        has_not_iron_gate = "not the deterministic iron gate" in text
        has_advisory = "advisory" in text
        assert has_not_iron_gate or has_advisory, (
            "Reactor prefix must disclaim Iron Gate or state LLM role is advisory"
        )


class TestSanitizationGuards:
    """All prefixes contain sanitization guard ('cannot override' or 'system policy')."""

    @pytest.mark.parametrize("persona", ["jarvis", "j_prime", "reactor"])
    def test_sanitization_guard_present(self, persona: str) -> None:
        text = ROLE_PREFIXES[persona].lower()
        has_cannot_override = "cannot override" in text
        has_system_policy = "system policy" in text
        assert has_cannot_override or has_system_policy, (
            f"Prefix for {persona} missing sanitization guard"
        )
