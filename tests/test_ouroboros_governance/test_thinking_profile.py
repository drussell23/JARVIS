"""Tests for ``_compute_thinking_profile`` — route-aware extended thinking.

The helper lives in ``providers.py`` and drives Claude's ``extended_thinking``
API parameter. Behavior is exhaustively env-driven; these tests lock in the
resolution order so future refactors can't silently flip which ops reason
deeply before writing patches.

Resolution order (first hit wins):
  1. trivial → off (unless JARVIS_THINKING_BUDGET_TRIVIAL > 0)
  2. architectural → force-on (overrides global off)
  3. complex (by complexity OR route) → force-on
  4. simple → reduced budget
  5. moderate → mid-tier budget
  6. unknown/empty → global default + base_budget
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Iterator

import pytest

from backend.core.ouroboros.governance.providers import _compute_thinking_profile


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Strip every JARVIS_THINKING_* env var so defaults apply."""
    for key in list(os.environ.keys()):
        if key.startswith("JARVIS_THINKING_"):
            monkeypatch.delenv(key, raising=False)
    yield


def _ctx(task_complexity: str = "", provider_route: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        task_complexity=task_complexity,
        provider_route=provider_route,
    )


# ---------------------------------------------------------------------------
# Trivial (step 1)
# ---------------------------------------------------------------------------


class TestTrivial:
    def test_trivial_skips_by_default(self, clean_env):
        enabled, tokens, reason = _compute_thinking_profile(
            _ctx(task_complexity="trivial"),
            extended_thinking_default=True,
            base_budget=10000,
        )
        assert enabled is False
        assert tokens == 0
        assert reason == "trivial-skip"

    def test_trivial_respects_explicit_env_override(
        self, clean_env, monkeypatch: pytest.MonkeyPatch
    ):
        """Power user sets JARVIS_THINKING_BUDGET_TRIVIAL=3000 → honored."""
        monkeypatch.setenv("JARVIS_THINKING_BUDGET_TRIVIAL", "3000")
        enabled, tokens, reason = _compute_thinking_profile(
            _ctx(task_complexity="trivial"),
            extended_thinking_default=True,
            base_budget=10000,
        )
        assert enabled is True
        assert tokens == 3000
        assert reason == "trivial-explicit"

    def test_trivial_explicit_still_off_if_global_off(
        self, clean_env, monkeypatch: pytest.MonkeyPatch
    ):
        """JARVIS_THINKING_BUDGET_TRIVIAL>0 does NOT override global disable."""
        monkeypatch.setenv("JARVIS_THINKING_BUDGET_TRIVIAL", "3000")
        enabled, _, reason = _compute_thinking_profile(
            _ctx(task_complexity="trivial"),
            extended_thinking_default=False,
            base_budget=10000,
        )
        assert enabled is False
        assert reason == "trivial-skip"


# ---------------------------------------------------------------------------
# Architectural (step 2) — highest tier, force-on
# ---------------------------------------------------------------------------


class TestArchitectural:
    def test_architectural_force_on(self, clean_env):
        enabled, tokens, reason = _compute_thinking_profile(
            _ctx(task_complexity="architectural"),
            extended_thinking_default=True,
            base_budget=10000,
        )
        assert enabled is True
        assert tokens == 24000  # default architectural budget
        assert reason == "architectural-force"

    def test_architectural_overrides_global_disable(self, clean_env):
        """Per user directive: complex/architectural MUST reason deeply,
        even when extended_thinking is globally off."""
        enabled, tokens, reason = _compute_thinking_profile(
            _ctx(task_complexity="architectural"),
            extended_thinking_default=False,
            base_budget=10000,
        )
        assert enabled is True
        assert tokens == 24000
        assert reason == "architectural-force"

    def test_architectural_force_on_disabled_via_env(
        self, clean_env, monkeypatch: pytest.MonkeyPatch
    ):
        """With force-on=false AND global disabled, architectural stays off."""
        monkeypatch.setenv("JARVIS_THINKING_FORCE_ON_COMPLEX", "false")
        enabled, _, reason = _compute_thinking_profile(
            _ctx(task_complexity="architectural"),
            extended_thinking_default=False,
            base_budget=10000,
        )
        assert enabled is False
        assert reason == "architectural-but-disabled"

    def test_architectural_budget_env_override(
        self, clean_env, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("JARVIS_THINKING_BUDGET_ARCHITECTURAL", "32000")
        _, tokens, _ = _compute_thinking_profile(
            _ctx(task_complexity="architectural"),
            extended_thinking_default=True,
            base_budget=10000,
        )
        assert tokens == 32000


# ---------------------------------------------------------------------------
# Complex (step 3) — force-on by complexity OR route
# ---------------------------------------------------------------------------


class TestComplex:
    @pytest.mark.parametrize("complexity", ["complex", "heavy_code"])
    def test_complex_by_task_complexity(self, clean_env, complexity):
        enabled, tokens, reason = _compute_thinking_profile(
            _ctx(task_complexity=complexity),
            extended_thinking_default=True,
            base_budget=10000,
        )
        assert enabled is True
        assert tokens == 16000  # default complex budget
        assert reason == "complex-force"

    def test_complex_by_provider_route(self, clean_env):
        """Route=complex alone (task_complexity empty) also triggers force-on."""
        enabled, tokens, reason = _compute_thinking_profile(
            _ctx(task_complexity="", provider_route="complex"),
            extended_thinking_default=True,
            base_budget=10000,
        )
        assert enabled is True
        assert tokens == 16000
        assert reason == "complex-force"

    def test_complex_overrides_global_disable(self, clean_env):
        enabled, _, reason = _compute_thinking_profile(
            _ctx(task_complexity="complex"),
            extended_thinking_default=False,
            base_budget=10000,
        )
        assert enabled is True
        assert reason == "complex-force"

    def test_complex_force_on_disabled_via_env(
        self, clean_env, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("JARVIS_THINKING_FORCE_ON_COMPLEX", "false")
        enabled, _, reason = _compute_thinking_profile(
            _ctx(task_complexity="complex"),
            extended_thinking_default=False,
            base_budget=10000,
        )
        assert enabled is False
        assert reason == "complex-but-disabled"

    def test_complex_budget_env_override(
        self, clean_env, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("JARVIS_THINKING_BUDGET_COMPLEX", "20000")
        _, tokens, _ = _compute_thinking_profile(
            _ctx(task_complexity="complex"),
            extended_thinking_default=True,
            base_budget=10000,
        )
        assert tokens == 20000


# ---------------------------------------------------------------------------
# Simple (step 4) — reduced budget, respects global disable
# ---------------------------------------------------------------------------


class TestSimple:
    def test_simple_uses_simple_budget(self, clean_env):
        enabled, tokens, reason = _compute_thinking_profile(
            _ctx(task_complexity="simple"),
            extended_thinking_default=True,
            base_budget=10000,
        )
        assert enabled is True
        assert tokens == 4000
        assert reason == "simple"

    def test_simple_respects_global_disable(self, clean_env):
        """Simple is NOT force-on — global off applies."""
        enabled, _, reason = _compute_thinking_profile(
            _ctx(task_complexity="simple"),
            extended_thinking_default=False,
            base_budget=10000,
        )
        assert enabled is False
        assert reason == "global-disabled"

    def test_simple_budget_env_override(
        self, clean_env, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("JARVIS_THINKING_BUDGET_SIMPLE", "6000")
        _, tokens, _ = _compute_thinking_profile(
            _ctx(task_complexity="simple"),
            extended_thinking_default=True,
            base_budget=10000,
        )
        assert tokens == 6000


# ---------------------------------------------------------------------------
# Moderate (step 5)
# ---------------------------------------------------------------------------


class TestModerate:
    def test_moderate_uses_moderate_budget(self, clean_env):
        enabled, tokens, reason = _compute_thinking_profile(
            _ctx(task_complexity="moderate"),
            extended_thinking_default=True,
            base_budget=10000,
        )
        assert enabled is True
        assert tokens == 8000
        assert reason == "moderate"

    def test_moderate_respects_global_disable(self, clean_env):
        enabled, _, reason = _compute_thinking_profile(
            _ctx(task_complexity="moderate"),
            extended_thinking_default=False,
            base_budget=10000,
        )
        assert enabled is False
        assert reason == "global-disabled"


# ---------------------------------------------------------------------------
# Unknown/empty (step 6) — fallback to provider base budget
# ---------------------------------------------------------------------------


class TestDefault:
    def test_empty_task_complexity_uses_base_budget(self, clean_env):
        enabled, tokens, reason = _compute_thinking_profile(
            _ctx(task_complexity=""),
            extended_thinking_default=True,
            base_budget=12345,
        )
        assert enabled is True
        assert tokens == 12345
        assert reason == "default"

    def test_unknown_task_complexity_uses_base_budget(self, clean_env):
        enabled, tokens, reason = _compute_thinking_profile(
            _ctx(task_complexity="gibberish"),
            extended_thinking_default=True,
            base_budget=10000,
        )
        assert enabled is True
        assert tokens == 10000
        assert reason == "default"

    def test_unknown_respects_global_disable(self, clean_env):
        enabled, _, reason = _compute_thinking_profile(
            _ctx(task_complexity=""),
            extended_thinking_default=False,
            base_budget=10000,
        )
        assert enabled is False
        assert reason == "global-disabled"


# ---------------------------------------------------------------------------
# Resolution order sanity
# ---------------------------------------------------------------------------


class TestResolutionOrder:
    def test_trivial_beats_complex_route(self, clean_env):
        """task_complexity=trivial wins even if provider_route=complex."""
        enabled, _, reason = _compute_thinking_profile(
            _ctx(task_complexity="trivial", provider_route="complex"),
            extended_thinking_default=True,
            base_budget=10000,
        )
        assert enabled is False
        assert reason == "trivial-skip"

    def test_architectural_beats_complex(self, clean_env):
        """Architectural should route to architectural-force, not complex-force."""
        enabled, tokens, reason = _compute_thinking_profile(
            _ctx(task_complexity="architectural", provider_route="complex"),
            extended_thinking_default=True,
            base_budget=10000,
        )
        assert enabled is True
        assert tokens == 24000
        assert reason == "architectural-force"

    def test_case_insensitive_matching(self, clean_env):
        """Upstream components sometimes emit uppercase values."""
        enabled, _, reason = _compute_thinking_profile(
            _ctx(task_complexity="COMPLEX"),
            extended_thinking_default=True,
            base_budget=10000,
        )
        assert enabled is True
        assert reason == "complex-force"


# ---------------------------------------------------------------------------
# Minimum token floor
# ---------------------------------------------------------------------------


class TestMinimumFloor:
    def test_budget_below_1024_floored(
        self, clean_env, monkeypatch: pytest.MonkeyPatch
    ):
        """Claude API rejects thinking budgets below 1024 — helper floors."""
        monkeypatch.setenv("JARVIS_THINKING_BUDGET_COMPLEX", "500")
        _, tokens, _ = _compute_thinking_profile(
            _ctx(task_complexity="complex"),
            extended_thinking_default=True,
            base_budget=10000,
        )
        assert tokens == 1024
