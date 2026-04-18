"""Regression spine — Phase 1 subagent dispatch graduated default.

Post-2026-04-18 Phase 1 graduation, the master switch
``JARVIS_SUBAGENT_DISPATCH_ENABLED`` defaults to ``true``. Three
consecutive clean Trinity cartography sessions (14/15/16) met
Manifesto §6 neuroplasticity threshold. The switch remains
env-tunable for isolation battle tests.

This file pins:
  1. Default return is True when env var is unset
  2. Explicit 'false' still disables (operator override works)
  3. Explicit 'true' remains enabled (no regression)
  4. Case-insensitive truthiness (existing contract)
  5. The subagent_contracts module docstring mentions graduation
     (pinning the source of truth so a future silent flip-back
     is loudly visible in diff review)
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts with a clean env var so we can pin defaults."""
    monkeypatch.delenv("JARVIS_SUBAGENT_DISPATCH_ENABLED", raising=False)


def test_dispatch_enabled_by_default_after_graduation() -> None:
    """Post-graduation default is True. Was False before 2026-04-18."""
    from backend.core.ouroboros.governance.subagent_contracts import (
        subagent_dispatch_enabled,
    )
    assert subagent_dispatch_enabled() is True, (
        "Master switch default must be True after Phase 1 graduation "
        "(2026-04-18). If this fails, someone reverted the graduation "
        "flip — check commit history for the revert and confirm whether "
        "the graduation criterion has been re-evaluated."
    )


def test_dispatch_can_be_disabled_via_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator override: JARVIS_SUBAGENT_DISPATCH_ENABLED=false still
    works for isolation battle tests and regression debugging."""
    from backend.core.ouroboros.governance.subagent_contracts import (
        subagent_dispatch_enabled,
    )
    monkeypatch.setenv("JARVIS_SUBAGENT_DISPATCH_ENABLED", "false")
    assert subagent_dispatch_enabled() is False


def test_dispatch_explicit_true_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit 'true' remains enabled — no regression from the
    pre-graduation default-off behavior when operators set the var."""
    from backend.core.ouroboros.governance.subagent_contracts import (
        subagent_dispatch_enabled,
    )
    monkeypatch.setenv("JARVIS_SUBAGENT_DISPATCH_ENABLED", "true")
    assert subagent_dispatch_enabled() is True


@pytest.mark.parametrize("val", ["FALSE", "False", "FaLsE", "0", "no"])
def test_dispatch_env_parsing_only_exact_false_disables(
    val: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The parser is `lower() == "true"` — so ONLY the literal 'true'
    (any case) enables. Anything else (including 'FALSE', '0', 'no')
    returns False. This is the existing contract and must not drift
    — if we loosen it, operator env typos become undetectable.
    """
    from backend.core.ouroboros.governance.subagent_contracts import (
        subagent_dispatch_enabled,
    )
    monkeypatch.setenv("JARVIS_SUBAGENT_DISPATCH_ENABLED", val)
    # Every value in the parametrize list is NOT equal to "true"
    # after .lower(), so all return False.
    assert subagent_dispatch_enabled() is False


def test_subagent_contracts_docstring_pins_graduation() -> None:
    """Structural pin: the module docstring mentions the graduation
    date. If someone silently flips the default back to False, the
    docstring will become stale and this test catches the drift.
    """
    src_path = (
        Path(__file__).resolve().parents[2]
        / "backend" / "core" / "ouroboros"
        / "governance" / "subagent_contracts.py"
    )
    src = src_path.read_text()
    assert "2026-04-18" in src, (
        "subagent_contracts.py must reference the 2026-04-18 graduation "
        "date — either in the module docstring or the "
        "subagent_dispatch_enabled docstring"
    )
    assert "default `true`" in src or 'default "true"' in src, (
        "Module docstring must declare the graduated default as True"
    )


def test_subagent_dispatch_enabled_function_default_is_true() -> None:
    """Source-level pin on the os.environ.get default value. If the
    default changes, this test breaks — forcing a conscious decision
    in code review rather than a silent flip-back.
    """
    import inspect
    from backend.core.ouroboros.governance.subagent_contracts import (
        subagent_dispatch_enabled,
    )
    src = inspect.getsource(subagent_dispatch_enabled)
    assert '"true"' in src, (
        "os.environ.get default must be \"true\" — the graduated state"
    )
    # Forbidden: the old default-false shape.
    assert 'os.environ.get("JARVIS_SUBAGENT_DISPATCH_ENABLED", "false")' not in src, (
        "Found pre-graduation default-false pattern — someone reverted "
        "the flip without updating regression"
    )
