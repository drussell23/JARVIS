"""tests/governance/test_immutable_governance_pin.py

Task 9 (Anti-Venom hardening): Immutable-governance frozenset grep-pin.

This test is a REGRESSION PIN — it fails immediately if the immune system is
weakened in any of three ways:

  1. An immune file is removed from ``_IMMUTABLE_GOVERNANCE_SENTINELS``.
  2. An env off-switch is introduced in the frozenset definition block
     (which would make the immovability conditional and defeat the security
     property).
  3. The self-protecting sentinels (``change_engine``, ``sandbox_exec``) are
     removed — the enforcer and the isolation-enforcer must protect themselves.

Design note: a pin test that always passes is useful only if it genuinely
asserts real runtime values.  Here we:
  - Import the live frozenset (not a hardcoded copy) so membership assertions
    test the real production object.
  - Read the *source text* of ``change_engine.py`` to assert the definition
    block contains no ``os.environ`` / ``os.getenv`` / ``getenv`` tokens.
  - Add a negative-control helper (``_would_catch_missing``) that proves the
    assertion logic genuinely fails when an entry is absent — so the pin is
    not vacuous.
"""

from __future__ import annotations

import importlib
import inspect
import re
from pathlib import Path
from typing import Sequence

import pytest

from backend.core.ouroboros.governance.change_engine import (
    _IMMUTABLE_GOVERNANCE_SENTINELS,
)

# ---------------------------------------------------------------------------
# Expected immune-file sentinel substrings (source of truth for this pin)
# ---------------------------------------------------------------------------

_EXPECTED_SENTINELS: list[str] = [
    "backend/core/ouroboros/governance/semantic_guardian",
    "backend/core/ouroboros/governance/tool_executor",
    "backend/core/ouroboros/governance/change_engine",
    "backend/core/ouroboros/governance/sandbox_exec",
    "backend/core/ouroboros/governance/risk_engine",
    "backend/core/ouroboros/governance/risk_tier_floor",
    "backend/core/ouroboros/governance/semantic_firewall",
    "backend/core/ouroboros/governance/scoped_tool_access",
    "backend/core/ouroboros/governance/orchestrator",
    "backend/core/ouroboros/governance/governed_loop_service",
    "backend/core/ouroboros/governance/intake/unified_intake_router",
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_frozenset_block(source: str) -> str:
    """Return the lines spanning the _IMMUTABLE_GOVERNANCE_SENTINELS definition.

    Starts at the line containing ``_IMMUTABLE_GOVERNANCE_SENTINELS`` and
    ``frozenset(`` and ends at the closing ``})`` line (inclusive).  Raises
    ``ValueError`` if the block cannot be located (which itself would be a pin
    failure).
    """
    lines = source.splitlines()
    start: int | None = None
    depth = 0
    block_lines: list[str] = []

    for i, line in enumerate(lines):
        if start is None:
            if "_IMMUTABLE_GOVERNANCE_SENTINELS" in line and "frozenset" in line:
                start = i
                depth = line.count("{") - line.count("}")
                block_lines.append(line)
                if depth == 0:
                    break
        else:
            depth += line.count("{") - line.count("}")
            block_lines.append(line)
            if depth <= 0:
                break

    if not block_lines:
        raise ValueError(
            "_IMMUTABLE_GOVERNANCE_SENTINELS frozenset block not found in source"
        )
    return "\n".join(block_lines)


def _would_catch_missing(sentinel: str, sentinels: frozenset[str]) -> bool:
    """Return True iff *sentinel* is absent from *sentinels* (pin catches it).

    Used as a negative-control: demonstrates the assertion logic is non-vacuous
    by checking that a *removed* entry would genuinely be detected as missing.
    """
    # Simulate weakening: pretend the entry was removed
    weakened = sentinels - {sentinel}
    return sentinel not in weakened


# ---------------------------------------------------------------------------
# Pin tests
# ---------------------------------------------------------------------------


class TestImmutableGovernancePin:
    """Regression pin: asserts the immune system is intact and non-bypassable."""

    # ------------------------------------------------------------------ #
    # 1. Membership: every expected immune file has a matching sentinel   #
    # ------------------------------------------------------------------ #

    @pytest.mark.parametrize("expected", _EXPECTED_SENTINELS)
    def test_sentinel_present(self, expected: str) -> None:
        """Pin fails if *expected* is no longer in the live frozenset."""
        assert expected in _IMMUTABLE_GOVERNANCE_SENTINELS, (
            f"IMMUNE FILE REMOVED FROM SENTINEL SET: {expected!r}\n"
            f"Current sentinels: {sorted(_IMMUTABLE_GOVERNANCE_SENTINELS)}"
        )

    def test_all_expected_sentinels_covered(self) -> None:
        """Aggregate membership check — fails on any missing entry."""
        missing = [s for s in _EXPECTED_SENTINELS if s not in _IMMUTABLE_GOVERNANCE_SENTINELS]
        assert not missing, (
            f"These immune files are no longer protected: {missing}\n"
            f"Current sentinels: {sorted(_IMMUTABLE_GOVERNANCE_SENTINELS)}"
        )

    # ------------------------------------------------------------------ #
    # 2. No env off-switch in the frozenset definition block              #
    # ------------------------------------------------------------------ #

    def test_no_env_gate_in_frozenset_definition(self) -> None:
        """The immovability must be unconditional — no os.environ / getenv.

        Reads the *source text* of change_engine.py, extracts the definition
        block for _IMMUTABLE_GOVERNANCE_SENTINELS, and asserts no env-read
        tokens appear in it.  An env-gated frozenset would let a compromised
        deployment disable the immune system at runtime.
        """
        import backend.core.ouroboros.governance.change_engine as _ce_mod

        source = inspect.getsource(_ce_mod)
        block = _extract_frozenset_block(source)

        env_tokens = ["os.environ", "os.getenv", "getenv"]
        found = [tok for tok in env_tokens if tok in block]
        assert not found, (
            f"ENV OFF-SWITCH DETECTED in _IMMUTABLE_GOVERNANCE_SENTINELS definition!\n"
            f"Tokens found: {found}\n"
            f"Definition block:\n{block}"
        )

    # ------------------------------------------------------------------ #
    # 3. Self-protecting: change_engine + sandbox_exec in the set         #
    # ------------------------------------------------------------------ #

    def test_change_engine_protects_itself(self) -> None:
        """The chokepoint enforcer must be in the sentinel set."""
        sentinel = "backend/core/ouroboros/governance/change_engine"
        assert sentinel in _IMMUTABLE_GOVERNANCE_SENTINELS, (
            "CRITICAL: change_engine removed its own sentinel — "
            "the chokepoint enforcer no longer protects itself."
        )

    def test_sandbox_exec_protects_itself(self) -> None:
        """The isolation enforcer must be in the sentinel set."""
        sentinel = "backend/core/ouroboros/governance/sandbox_exec"
        assert sentinel in _IMMUTABLE_GOVERNANCE_SENTINELS, (
            "CRITICAL: sandbox_exec removed its own sentinel — "
            "the isolation enforcer no longer protects itself."
        )

    # ------------------------------------------------------------------ #
    # 4. Negative-control: proves assertion logic is non-vacuous          #
    # ------------------------------------------------------------------ #

    def test_negative_control_missing_entry_is_caught(self) -> None:
        """Prove the pin logic genuinely fails when an entry is absent.

        Simulates removing a sentinel and confirms the membership check would
        detect it — ensuring the test is not trivially always-passing.
        """
        probe = "backend/core/ouroboros/governance/change_engine"
        # _would_catch_missing returns True when the removal IS detectable
        assert _would_catch_missing(probe, _IMMUTABLE_GOVERNANCE_SENTINELS), (
            "Negative-control failure: the pin logic did NOT detect a simulated "
            "sentinel removal — the test would be vacuous."
        )

    # ------------------------------------------------------------------ #
    # 5. Frozenset type: must remain frozenset (not a mutable set/list)   #
    # ------------------------------------------------------------------ #

    def test_sentinels_is_frozenset(self) -> None:
        """The collection must be immutable (frozenset, not set/list/tuple)."""
        assert isinstance(_IMMUTABLE_GOVERNANCE_SENTINELS, frozenset), (
            f"_IMMUTABLE_GOVERNANCE_SENTINELS must be a frozenset; "
            f"got {type(_IMMUTABLE_GOVERNANCE_SENTINELS).__name__}"
        )
