"""§Layer 8 closure (v2.96) — atexit-fallback marker discrimination.

Closes the cadence-arc Layer 8 root cause diagnosed 2026-05-10
during the bt-2026-05-10-221432 soak:

* The v2.92 Layer 7 dual-clock watchdog fired wall_clock_cap
  correctly at 2400s wall (sleep-resilient).
* The v2.88 Layer 6 atexit fallback wrote summary.json before
  the ShutdownWatchdog os._exit(75) at the 30s deadline.
* But the partial summary stamped
  ``session_outcome=incomplete_kill`` via the cause→outcome
  adapter (which mapped WALL_CLOCK_CAP to incomplete_kill).
* AND the soak-classifier's _SHUTDOWN_NOISE_STOP_REASONS set
  included ``wall_clock_cap``.
* Result: soak #3 (a harness-intended wall-clock-cap termination
  with the clean shutdown path slow-but-completing successfully)
  classified as ``outcome=infra`` — operator-visible as the
  third row in the Phase 9 ladder failing to graduate
  JARVIS_EXECUTION_MONITOR_BRIDGE_ENABLED.

The Layer 8 structural fix discriminates at the SOURCE of the
session_outcome stamp (not at the downstream classifier). Two
seam edits:

1. ``termination_hook_default_adapters._CAUSE_TO_SESSION_OUTCOME``:
   WALL_CLOCK_CAP / IDLE_TIMEOUT / BUDGET_EXCEEDED → ``complete``
   (was ``incomplete_kill``). These are harness-intended
   terminations per CLAUDE.md clean-bar-equivalence footnote.
   External-signal causes (SIGTERM/SIGINT/SIGHUP) stay
   ``incomplete_kill``.

2. ``live_fire_soak._SHUTDOWN_NOISE_STOP_REASONS``: remove
   ``wall_clock_cap`` from the noise set. Mirrors ``idle_timeout``
   which has always been clean per CLAUDE.md. ``harness_idle_
   timeout`` stays in (it's the distinct hardware-bus-timeout
   case).

This file pins both invariants AND the load-bearing forward-fix
test: under the post-Layer-8 harness, the bt-2026-05-10-221432
signature classifies as CLEAN.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path


def _read_adapter_src() -> str:
    from backend.core.ouroboros.battle_test import (
        termination_hook_default_adapters,
    )
    return Path(
        inspect.getfile(termination_hook_default_adapters),
    ).read_text(encoding="utf-8")


def _read_classifier_src() -> str:
    from backend.core.ouroboros.governance.graduation import (
        live_fire_soak,
    )
    return Path(
        inspect.getfile(live_fire_soak),
    ).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Fix #1 — cause→outcome adapter
# ---------------------------------------------------------------------------


def test_wall_clock_cap_maps_to_complete():
    from backend.core.ouroboros.battle_test.termination_hook import (
        TerminationCause,
    )
    from backend.core.ouroboros.battle_test.termination_hook_default_adapters import (  # noqa: E501
        _CAUSE_TO_SESSION_OUTCOME,
    )
    assert (
        _CAUSE_TO_SESSION_OUTCOME[TerminationCause.WALL_CLOCK_CAP]
        == "complete"
    ), (
        "Layer 8 (v2.96): WALL_CLOCK_CAP maps to 'complete' — "
        "harness-intended termination per CLAUDE.md clean-bar-"
        "equivalence with idle_timeout. Drift regresses to soak "
        "#3 (bt-2026-05-10-221432) misclassification."
    )


def test_idle_timeout_maps_to_complete():
    from backend.core.ouroboros.battle_test.termination_hook import (
        TerminationCause,
    )
    from backend.core.ouroboros.battle_test.termination_hook_default_adapters import (  # noqa: E501
        _CAUSE_TO_SESSION_OUTCOME,
    )
    assert (
        _CAUSE_TO_SESSION_OUTCOME[TerminationCause.IDLE_TIMEOUT]
        == "complete"
    ), (
        "Layer 8 (v2.96): IDE_TIMEOUT maps to 'complete' — "
        "harness-intended termination, symmetric with "
        "wall_clock_cap."
    )


def test_budget_exceeded_maps_to_complete():
    from backend.core.ouroboros.battle_test.termination_hook import (
        TerminationCause,
    )
    from backend.core.ouroboros.battle_test.termination_hook_default_adapters import (  # noqa: E501
        _CAUSE_TO_SESSION_OUTCOME,
    )
    assert (
        _CAUSE_TO_SESSION_OUTCOME[
            TerminationCause.BUDGET_EXCEEDED
        ]
        == "complete"
    ), (
        "Layer 8 (v2.96): BUDGET_EXCEEDED maps to 'complete' — "
        "harness-intended termination (the cost cap fired per "
        "design)."
    )


def test_external_signals_still_incomplete_kill():
    """Layer 8 preserves the legacy semantics for external signal
    causes — the harness did NOT intend these terminations."""
    from backend.core.ouroboros.battle_test.termination_hook import (
        TerminationCause,
    )
    from backend.core.ouroboros.battle_test.termination_hook_default_adapters import (  # noqa: E501
        _CAUSE_TO_SESSION_OUTCOME,
    )
    for sig in (
        TerminationCause.SIGTERM,
        TerminationCause.SIGINT,
        TerminationCause.SIGHUP,
    ):
        assert (
            _CAUSE_TO_SESSION_OUTCOME[sig] == "incomplete_kill"
        ), (
            f"External-signal cause {sig.value!r} MUST stay "
            f"incomplete_kill — Layer 8 does NOT change the "
            f"external-signal classification"
        )


def test_normal_exit_still_none():
    """NORMAL_EXIT preserves the writer's default-args path
    (clean shutdown writes session_outcome=complete itself)."""
    from backend.core.ouroboros.battle_test.termination_hook import (
        TerminationCause,
    )
    from backend.core.ouroboros.battle_test.termination_hook_default_adapters import (  # noqa: E501
        _CAUSE_TO_SESSION_OUTCOME,
    )
    assert (
        _CAUSE_TO_SESSION_OUTCOME[TerminationCause.NORMAL_EXIT]
        is None
    )


def test_unknown_safe_default_incomplete_kill():
    """Safe default — if we don't know why we're terminating,
    treat as interrupted."""
    from backend.core.ouroboros.battle_test.termination_hook import (
        TerminationCause,
    )
    from backend.core.ouroboros.battle_test.termination_hook_default_adapters import (  # noqa: E501
        _CAUSE_TO_SESSION_OUTCOME,
    )
    assert (
        _CAUSE_TO_SESSION_OUTCOME[TerminationCause.UNKNOWN]
        == "incomplete_kill"
    )


# ---------------------------------------------------------------------------
# Fix #2 — soak classifier noise set
# ---------------------------------------------------------------------------


def test_wall_clock_cap_NOT_in_shutdown_noise_set():
    """Layer 8 (v2.96, 2026-05-10): wall_clock_cap is REMOVED
    from _SHUTDOWN_NOISE_STOP_REASONS — it's a harness-intended
    clean stop per CLAUDE.md, not noise."""
    from backend.core.ouroboros.governance.graduation.live_fire_soak import (  # noqa: E501
        _SHUTDOWN_NOISE_STOP_REASONS,
    )
    assert "wall_clock_cap" not in _SHUTDOWN_NOISE_STOP_REASONS, (
        "Layer 8 invariant: wall_clock_cap MUST NOT be in the "
        "noise set. Pre-Layer-8 it was, which caused soak #3 "
        "(bt-2026-05-10-221432) to misclassify as outcome=infra."
    )


def test_external_signals_still_in_shutdown_noise_set():
    """External signals stay noise (the harness did NOT intend
    these terminations)."""
    from backend.core.ouroboros.governance.graduation.live_fire_soak import (  # noqa: E501
        _SHUTDOWN_NOISE_STOP_REASONS,
    )
    for sig in ("sigterm", "sigint", "sighup"):
        assert sig in _SHUTDOWN_NOISE_STOP_REASONS, (
            f"External-signal {sig!r} MUST stay in noise set — "
            f"Layer 8 only removes harness-intended causes"
        )


# ---------------------------------------------------------------------------
# Forward-fix integration — the soak #3 signature post-Layer-8
# ---------------------------------------------------------------------------


def test_layer8_forward_fix_signature_routes_clean():
    """The load-bearing Layer 8 integration test: a future
    Phase 9 soak that hits the exact bt-2026-05-10-221432 pattern
    (host sleep mid-soak triggers Layer 7 dual-clock wall_clock_cap
    + slow shutdown triggers Layer 6 atexit fallback) under the
    post-Layer-8 harness MUST classify as CLEAN.

    Inputs reflect the post-Layer-8 stamping:
      session_outcome = "complete"
        (was "incomplete_kill" pre-Layer-8 — the bug)
      stop_reason = "wall_clock_cap+atexit_fallback"
        (the composite from the partial-shutdown insurance path)
      failure_class_counts = {}
        (no infra-class errors recorded)

    Expected: outcome=clean — the row counts toward the 3-clean
    graduation ladder."""
    from backend.core.ouroboros.governance.graduation.live_fire_soak import (  # noqa: E501
        classify_outcome,
    )
    outcome, runner_attributed, notes = classify_outcome({
        "session_outcome": "complete",
        "stop_reason": "wall_clock_cap+atexit_fallback",
        "failure_class_counts": {},
    })
    assert outcome == "clean", (
        f"Layer 8 forward-fix: post-Layer-8 wall_clock_cap+"
        f"atexit_fallback with session_outcome=complete MUST "
        f"classify as clean. Got outcome={outcome!r} "
        f"notes={notes!r}. This is the load-bearing graduation-"
        f"ladder invariant."
    )
    assert runner_attributed is False


def test_layer8_legacy_incomplete_kill_still_infra():
    """Backward-compat: legacy partial-summary rows from
    pre-Layer-8 harnesses (or hand-edited rows) that carry
    session_outcome=incomplete_kill still route to infra. The
    _INCOMPLETE_OUTCOME_VALUES path is preserved — Layer 8 fixes
    the SOURCE (cause→outcome adapter), NOT the classifier
    predicate."""
    from backend.core.ouroboros.governance.graduation.live_fire_soak import (  # noqa: E501
        classify_outcome,
    )
    outcome, _, _ = classify_outcome({
        "session_outcome": "incomplete_kill",
        "stop_reason": "wall_clock_cap+atexit_fallback",
        "failure_class_counts": {},
    })
    assert outcome == "infra", (
        "Legacy pre-Layer-8 rows with incomplete_kill marker "
        "still classify as infra — Layer 8 fixes the SOURCE not "
        "the predicate"
    )


# ---------------------------------------------------------------------------
# AST pins — Layer 8 source-of-truth discipline
# ---------------------------------------------------------------------------


def test_ast_pin_adapter_cites_layer_8():
    """The cause→outcome adapter source MUST cite Layer 8
    (v2.96) so future readers find the design doc."""
    src = _read_adapter_src()
    assert "Layer 8" in src and "v2.96" in src, (
        "termination_hook_default_adapters MUST cite Layer 8 "
        "+ v2.96 in source for discoverability"
    )
    assert "CLAUDE.md" in src, (
        "adapter MUST cite CLAUDE.md as the authoritative "
        "source for clean-bar-equivalence semantics"
    )


def test_ast_pin_classifier_cites_layer_8():
    """The classifier noise set source MUST cite Layer 8 (v2.96)
    so future readers see the rationale for removing
    wall_clock_cap."""
    src = _read_classifier_src()
    assert "Layer 8" in src and "v2.96" in src, (
        "live_fire_soak MUST cite Layer 8 + v2.96 in source"
    )


def test_ast_pin_adapter_mapping_uses_complete_literal():
    """Bytes-pin: the adapter MUST contain the literal
    ``WALL_CLOCK_CAP: \"complete\"`` (or its idiomatic ``: "complete"``
    suffix) — drift to ``incomplete_kill`` regresses to pre-
    Layer-8 misclassification."""
    src = _read_adapter_src()
    # Permissive: match either single or double quotes; the
    # important pin is that WALL_CLOCK_CAP's value is "complete".
    tree = ast.parse(src)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key_node, val_node in zip(node.keys, node.values):
                if (
                    isinstance(key_node, ast.Attribute)
                    and key_node.attr == "WALL_CLOCK_CAP"
                    and isinstance(val_node, ast.Constant)
                    and val_node.value == "complete"
                ):
                    found = True
                    break
            if found:
                break
    assert found, (
        "WALL_CLOCK_CAP MUST map to literal \"complete\" — "
        "drift to 'incomplete_kill' regresses Layer 8"
    )


def test_ast_pin_classifier_noise_set_size():
    """Bytes-pin: the _SHUTDOWN_NOISE_STOP_REASONS frozenset
    MUST contain exactly 4 entries (sigterm/sighup/sigint/
    harness_idle_timeout). Adding wall_clock_cap back regresses
    Layer 8."""
    from backend.core.ouroboros.governance.graduation.live_fire_soak import (  # noqa: E501
        _SHUTDOWN_NOISE_STOP_REASONS,
    )
    assert len(_SHUTDOWN_NOISE_STOP_REASONS) == 4, (
        f"Layer 8 invariant: noise set has exactly 4 entries "
        f"(external-signal causes only). Got "
        f"{len(_SHUTDOWN_NOISE_STOP_REASONS)} — drift here "
        f"regresses the harness-intended clean-bar discipline."
    )
