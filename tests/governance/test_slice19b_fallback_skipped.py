"""Slice 19b — fallback=None → 'fallback_skipped' (not 'fallback_failed').

Closes the cascade FSM ↔ ExhaustionWatcher semantic mismatch surfaced by
soak bt-2026-05-26-180129 (PURE-DW v14).

# The bug — fallback semantics in DW-only mode

Slice 19a (PR #59070) added JARVIS_PROVIDER_CLAUDE_DISABLED env to
skip ClaudeProvider construction entirely (self._fallback stays None).
v14 then proved DW serves real work (265s Venom loop, 23 tool calls,
76K tokens, $0.0085 per op) — but when DW returned 0 candidates the
orchestrator's cascade FSM invoked _call_fallback. With self._fallback=None
the call hit AttributeError, classified as "fallback_failed",
ExhaustionWatcher incremented consecutive counter. 3 consecutive
"fallback_failed" → hibernation cycle 1 → BG pool paused → soak idle.

The semantic error: "no fallback configured" is NOT "fallback broke".
Operator-attested absence of a provider tier shouldn't be treated as
provider distress.

# Fix mechanism

**candidate_generator.py — _call_fallback early guard:**

  if self._fallback is None:
      logger.info(...)
      self._raise_exhausted(
          "fallback_skipped:no_fallback_configured",
          context=context, deadline=deadline,
          fallback_state="absent_by_configuration",
      )

Distinct cause prefix ``fallback_skipped:`` (vs ``fallback_failed:``).
The full exhaustion-report breadcrumb still fires for observability,
but ExhaustionWatcher classifies it differently.

**provider_exhaustion_watcher.py — record_exhaustion filter:**

  if "fallback_skipped:" in reason:
      # Recorded in total_exhaustions for observability,
      # but does NOT advance the consecutive counter.
      return False

# Discipline

* fallback_skipped events ARE counted in total_exhaustions (audit trail
  preserved) but DON'T advance consecutive counter toward hibernation.
* fallback_failed semantics unchanged for genuine provider distress
  (AttributeError-from-missing-provider was the special case; real
  provider exceptions continue triggering hibernation).
* No new env knob — fallback=None is the signal; Slice 19a's existing
  env (JARVIS_PROVIDER_CLAUDE_DISABLED) is the operator surface.

# Test surface (2 AST pins + 6 spine)
"""

from __future__ import annotations

import asyncio
import ast
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CG_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "candidate_generator.py"
)
EW_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "provider_exhaustion_watcher.py"
)


# ──────────────────────────────────────────────────────────────────────
# AST PINS — 2
# ──────────────────────────────────────────────────────────────────────


def test_ast_pin_call_fallback_emits_fallback_skipped_on_none() -> None:
    """``_call_fallback`` MUST check ``self._fallback is None`` early
    and call ``_raise_exhausted`` with cause prefix
    ``fallback_skipped:`` — NOT let the path fall through to a
    fallback_failed AttributeError."""
    src = CG_FILE.read_text()
    assert "fallback_skipped:no_fallback_configured" in src, (
        "candidate_generator missing fallback_skipped cause emission — "
        "Slice 19b reverted; PURE-DW soaks will re-trigger hibernation"
    )
    # AST walk to confirm the emission is inside _call_fallback
    tree = ast.parse(src, filename=str(CG_FILE))
    found = False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_call_fallback"
        ):
            body_src = ast.unparse(node)
            if (
                "self._fallback is None" in body_src
                and "fallback_skipped:no_fallback_configured" in body_src
                and "Slice 19b" in body_src
            ):
                found = True
                break
    assert found, (
        "_call_fallback body missing Slice 19b guard — emission may "
        "exist elsewhere but cascade FSM won't reach it"
    )


def test_ast_pin_exhaustion_watcher_filters_fallback_skipped() -> None:
    """``ExhaustionWatcher.record_exhaustion`` MUST filter
    ``fallback_skipped:`` reasons out of the consecutive counter.
    Without this, the cause prefix from candidate_generator is
    semantically distinct but operationally identical → hibernation
    still fires."""
    src = EW_FILE.read_text()
    assert "fallback_skipped:" in src, (
        "ExhaustionWatcher does NOT match fallback_skipped: prefix — "
        "Slice 19b filter dead code"
    )
    # AST walk to confirm the filter sits inside record_exhaustion
    # BEFORE the consecutive increment
    tree = ast.parse(src, filename=str(EW_FILE))
    found = False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "record_exhaustion"
        ):
            body_src = ast.unparse(node)
            if (
                '"fallback_skipped:"' in body_src
                or "'fallback_skipped:'" in body_src
            ) and "Slice 19b" in body_src:
                found = True
                break
    assert found, (
        "record_exhaustion missing Slice 19b filter — consecutive "
        "counter still increments on fallback_skipped"
    )


# ──────────────────────────────────────────────────────────────────────
# Spine — 6
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_spine_exhaustion_watcher_filters_fallback_skipped_reason() -> None:
    """End-to-end: record_exhaustion called with a fallback_skipped
    reason does NOT advance the consecutive counter (the v14
    bt-2026-05-26-180129 hibernation regression guard)."""
    from backend.core.ouroboros.governance.provider_exhaustion_watcher import (
        ProviderExhaustionWatcher,
    )
    # Build a watcher with a controller stub
    controller = MagicMock()
    controller.enter_hibernation = MagicMock(return_value=True)
    watcher = ProviderExhaustionWatcher(
        controller=controller, threshold=3,
    )
    # Three fallback_skipped events → MUST stay at consecutive=0
    for i in range(3):
        await watcher.record_exhaustion(
            reason=f"all_providers_exhausted:fallback_skipped:no_fallback_configured",
            op_id=f"op-{i}",
        )
    snapshot = watcher.snapshot()
    assert snapshot["consecutive"] == 0, (
        f"Slice 19b filter failed: consecutive={snapshot['consecutive']} "
        "after 3 fallback_skipped events; expected 0"
    )
    # Total events still counted for observability
    assert snapshot["total_exhaustions"] == 3
    # Hibernation NOT triggered
    controller.enter_hibernation.assert_not_called()


@pytest.mark.asyncio
async def test_spine_exhaustion_watcher_still_counts_genuine_fallback_failed() -> None:
    """Genuine fallback_failed events (real provider distress) MUST
    still advance the counter and trigger hibernation at threshold.
    Slice 19b only filters the fallback_skipped case."""
    from backend.core.ouroboros.governance.provider_exhaustion_watcher import (
        ProviderExhaustionWatcher,
    )
    controller = MagicMock()
    controller.enter_hibernation = MagicMock(return_value=True)
    watcher = ProviderExhaustionWatcher(
        controller=controller, threshold=3,
    )
    for i in range(3):
        await watcher.record_exhaustion(
            reason=f"all_providers_exhausted:fallback_failed",
            op_id=f"op-{i}",
        )
    snapshot = watcher.snapshot()
    assert snapshot["consecutive"] == 3
    # Hibernation triggered
    controller.enter_hibernation.assert_called_once()


@pytest.mark.asyncio
async def test_spine_fallback_skipped_does_not_dedup_with_failed() -> None:
    """Mixing fallback_skipped + fallback_failed must count ONLY the
    failed ones toward consecutive. Two skipped + two failed = 2
    consecutive (not 4)."""
    from backend.core.ouroboros.governance.provider_exhaustion_watcher import (
        ProviderExhaustionWatcher,
    )
    controller = MagicMock()
    controller.enter_hibernation = MagicMock(return_value=True)
    watcher = ProviderExhaustionWatcher(
        controller=controller, threshold=3,
    )
    await watcher.record_exhaustion(
        reason="all_providers_exhausted:fallback_skipped:no_fallback_configured",
        op_id="op-skipped-1",
    )
    await watcher.record_exhaustion(
        reason="all_providers_exhausted:fallback_failed",
        op_id="op-failed-1",
    )
    await watcher.record_exhaustion(
        reason="all_providers_exhausted:fallback_skipped:no_fallback_configured",
        op_id="op-skipped-2",
    )
    await watcher.record_exhaustion(
        reason="all_providers_exhausted:fallback_failed",
        op_id="op-failed-2",
    )
    snapshot = watcher.snapshot()
    assert snapshot["consecutive"] == 2, (
        f"Slice 19b filter leaked: consecutive={snapshot['consecutive']} "
        "after 2 fallback_failed (interleaved with 2 fallback_skipped); "
        "expected 2"
    )


def test_spine_fallback_skipped_cause_is_distinct_substring() -> None:
    """The cause prefix MUST be distinct enough that no legitimate
    other cause matches it accidentally. ``fallback_skipped:`` (with
    colon) is the spine — substring match must not catch
    ``fallback_failed`` or any other cause."""
    distinct_cause = "fallback_skipped:no_fallback_configured"
    distractor_causes = [
        "fallback_failed",
        "fallback_failed:something",
        "fallback_round_starved",
        "fallback_disabled_by_env:standard",
        "queue_only_dispatch",
        "primary_consecutive_failures_exceeded",
        "circuit_breaker_tripped:terminal_config",
    ]
    # Slice 19b filter uses `"fallback_skipped:" in reason`
    assert "fallback_skipped:" in distinct_cause
    for d in distractor_causes:
        assert "fallback_skipped:" not in d, (
            f"Cause {d!r} accidentally matches fallback_skipped: filter "
            "— Slice 19b classification leaks"
        )


@pytest.mark.asyncio
async def test_spine_record_success_still_resets_after_skipped(
) -> None:
    """A successful generation after fallback_skipped events MUST still
    clear the dedup set. Slice 19b doesn't break the existing success
    path."""
    from backend.core.ouroboros.governance.provider_exhaustion_watcher import (
        ProviderExhaustionWatcher,
    )
    controller = MagicMock()
    controller.enter_hibernation = MagicMock(return_value=True)
    watcher = ProviderExhaustionWatcher(
        controller=controller, threshold=3,
    )
    # Two skipped (no consecutive advance)
    await watcher.record_exhaustion(
        reason="all_providers_exhausted:fallback_skipped:no_fallback_configured",
        op_id="op-1",
    )
    await watcher.record_exhaustion(
        reason="all_providers_exhausted:fallback_skipped:no_fallback_configured",
        op_id="op-2",
    )
    # Then a success
    await watcher.record_success()
    snapshot = watcher.snapshot()
    assert snapshot["consecutive"] == 0
    # total_successes incremented
    assert snapshot["total_successes"] == 1


def test_spine_documentation_attribution_present() -> None:
    """Both source files MUST carry Slice 19b attribution + bt-2026-05-26-180129
    soak link so future readers can trace the semantic correction."""
    cg_src = CG_FILE.read_text()
    ew_src = EW_FILE.read_text()
    for src, name in ((cg_src, "candidate_generator"), (ew_src, "provider_exhaustion_watcher")):
        assert "Slice 19b" in src, (
            f"{name} missing Slice 19b attribution"
        )
    # bt soak link in at least one file (it's the same forensic for both)
    assert "bt-2026-05-26-180129" in cg_src or "bt-2026-05-26-180129" in ew_src, (
        "Missing bt-2026-05-26-180129 PURE-DW v14 soak attribution "
        "in either file — forensic trail lost"
    )
