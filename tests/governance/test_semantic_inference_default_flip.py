"""Tier 0a -- regression spine for the JARVIS_SEMANTIC_INFERENCE_ENABLED
default flip (false -> true on 2026-05-03).

Pre-flip discovery: every consumer (StrategicDirection prompt
injection, ProactiveExploration cluster bias, GET routes,
ClusterIntelligence-CrossSession arc) was structurally live but
operationally dormant under default-False. Soak v4 proved
substrate dark in production.

This pin guards against silent regression of the master flag
default in a future refactor. Three guarantees:

  1. ``_is_enabled()`` returns ``True`` when env var is unset.
  2. ``_is_enabled()`` returns ``True`` when env var is empty
     string (whitespace handling).
  3. ``_is_enabled()`` returns ``False`` when operator explicitly
     opts out via ``"false"``.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.semantic_index import (
    _is_enabled,
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_SEMANTIC_INFERENCE_ENABLED", raising=False,
    )


class TestSemanticInferenceDefault:
    def test_default_true_post_tier0a_flip(self):
        assert _is_enabled() is True

    def test_empty_string_treated_as_explicit_false(self, monkeypatch):
        """semantic_index._env_bool treats empty string as
        explicit non-truthy (KeyError-only-default semantics).
        Differs from the asymmetric env semantics used elsewhere
        in the codebase. Pinning the actual behavior so a future
        refactor of _env_bool surfaces the change loudly."""
        monkeypatch.setenv(
            "JARVIS_SEMANTIC_INFERENCE_ENABLED", "",
        )
        assert _is_enabled() is False

    @pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "On"])
    def test_explicit_truthy(self, monkeypatch, raw):
        monkeypatch.setenv(
            "JARVIS_SEMANTIC_INFERENCE_ENABLED", raw,
        )
        assert _is_enabled() is True

    @pytest.mark.parametrize(
        "raw", ["0", "false", "FALSE", "no", "off"],
    )
    def test_operator_escape_hatch(self, monkeypatch, raw):
        """Explicit ``"false"`` (and equivalents) overrides the
        graduated default -- operators retain a clean escape
        hatch."""
        monkeypatch.setenv(
            "JARVIS_SEMANTIC_INFERENCE_ENABLED", raw,
        )
        assert _is_enabled() is False
