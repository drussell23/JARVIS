"""
Task #107 spine — gate-adoption audit sampler is env-gated + bounded.

Operator-approved 2026-05-15 under six bounds (measurement only, no
subsystem, no substrate PR until a loop is named).  The audit sampler
periodically snapshots the not-done task population during the
pre-first-raw-event window of a Claude stream, so a thinking=on-vs-off
boundary diff can name the ungated task family behind the Task #105
SPLIT Tier-C thinking=on gap.

This spine pins (no behavior assertions on a live stream — pure
source/AST + FlagRegistry):

  * The audit snapshot call site exists ONLY behind its own
    default-false env flag (distinct from the one-shot boundary flag).
  * The sampler is hard-bounded (self-terminating stop conditions +
    absolute sample cap) so it can never leak a task.
  * It is cancelled when the first raw event arrives (tidy + instant).
  * FlagRegistry seeds present: enable (BOOL, default False, SAFETY)
    + interval (FLOAT, default 15.0, TUNING), both sourced to
    providers.py.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


_PROVIDERS_SRC = (
    Path(__file__).parents[2]
    / "backend" / "core" / "ouroboros" / "governance" / "providers.py"
)
_SEED_SRC = (
    Path(__file__).parents[2]
    / "backend" / "core" / "ouroboros" / "governance" / "flag_registry_seed.py"
)


def _providers_text() -> str:
    return _PROVIDERS_SRC.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# AST / source pins — the sampler exists only behind the flag
# ---------------------------------------------------------------------------


def test_audit_snapshot_logged_only_behind_its_own_flag():
    src = _providers_text()
    # The audit log line must exist
    assert "[ClaudeProvider.stream.boundary.audit]" in src, (
        "Task #107 audit sampler log line must exist"
    )
    # It must be gated by the AUDIT flag, NOT the one-shot boundary flag
    assert "JARVIS_CLAUDE_STREAM_BOUNDARY_AUDIT_ENABLED" in src, (
        "audit sampler must be gated by its own default-false flag"
    )
    # The audit env-gate check must appear BEFORE the sampler task is
    # created (source order: flag check guards create_task).
    flag_idx = src.index('"JARVIS_CLAUDE_STREAM_BOUNDARY_AUDIT_ENABLED", ""')
    create_idx = src.index("_boundary_audit_sampler()")
    assert flag_idx < create_idx, (
        "the audit flag check MUST guard the sampler task creation "
        "(call site exists only behind the flag)"
    )


def test_audit_flag_is_distinct_from_oneshot_boundary_flag():
    src = _providers_text()
    # Both flags exist and are different env vars — the audit is
    # additive, not a reuse of the one-shot gate.
    assert "JARVIS_CLAUDE_STREAM_BOUNDARY_LOG_ENABLED" in src
    assert "JARVIS_CLAUDE_STREAM_BOUNDARY_AUDIT_ENABLED" in src
    assert (
        "JARVIS_CLAUDE_STREAM_BOUNDARY_LOG_ENABLED"
        != "JARVIS_CLAUDE_STREAM_BOUNDARY_AUDIT_ENABLED"
    )


def test_sampler_is_hard_bounded_cannot_leak():
    """The sampler must have ALL THREE self-terminating stop
    conditions so it can never leak a background task even if the
    stream errors before any event:
      (1) stop when first raw event seen,
      (2) stop at a wall deadline,
      (3) absolute sample-count hard cap.
    """
    src = _providers_text()
    tree = ast.parse(src)
    sampler = None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_boundary_audit_sampler"
        ):
            sampler = node
            break
    assert sampler is not None, "_boundary_audit_sampler must exist"
    seg = ast.get_source_segment(src, sampler) or ""
    # (1) first-raw-event stop
    assert "_first_raw_event_logged[0]" in seg and "return" in seg, (
        "sampler must stop when the first raw event is logged"
    )
    # (2) wall deadline stop
    assert "_audit_deadline" in seg, (
        "sampler must stop at a wall deadline (never run unbounded)"
    )
    # (3) absolute sample-count hard cap
    assert "_seq > 60" in seg, (
        "sampler must have an absolute sample-count hard cap"
    )
    # CancelledError handled cleanly (cancel on first-raw-event path)
    assert "asyncio.CancelledError" in seg, (
        "sampler must handle CancelledError cleanly (instant tidy "
        "cancel when first raw event arrives)"
    )


def test_sampler_cancelled_on_first_raw_event():
    src = _providers_text()
    assert "_audit_task.cancel()" in src, (
        "the audit task MUST be cancelled when the first raw event "
        "arrives (tidy + instant; it would self-terminate within one "
        "interval anyway)"
    )
    # The cancel must be guarded (None when flag off / not done check)
    assert "_audit_task is not None and not _audit_task.done()" in src, (
        "cancel must be guarded — _audit_task is None when the flag "
        "is off"
    )


def test_audit_task_initialized_unconditionally_to_none():
    """`_audit_task` must be bound (=None) BEFORE the flag check so the
    later cancel reference is always valid even when the flag is off."""
    src = _providers_text()
    assert '_audit_task: "Optional[asyncio.Task]" = None' in src, (
        "_audit_task must be initialized to None unconditionally "
        "before the flag-gated create_task"
    )
    none_idx = src.index('_audit_task: "Optional[asyncio.Task]" = None')
    create_idx = src.index("_boundary_audit_sampler()")
    assert none_idx < create_idx


def test_sampler_is_log_only_never_raises():
    """The snapshot body must be wrapped so a failing all_tasks() /
    logging call can never raise into the event loop."""
    src = _providers_text()
    tree = ast.parse(src)
    sampler = None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_boundary_audit_sampler"
        ):
            sampler = node
            break
    assert sampler is not None
    seg = ast.get_source_segment(src, sampler) or ""
    assert "except Exception:" in seg, (
        "the snapshot body must swallow exceptions — log-only, never "
        "raises into the event loop"
    )


# ---------------------------------------------------------------------------
# FlagRegistry seeds
# ---------------------------------------------------------------------------


def test_seed_audit_enable_flag():
    src = _SEED_SRC.read_text(encoding="utf-8")
    idx = src.find('name="JARVIS_CLAUDE_STREAM_BOUNDARY_AUDIT_ENABLED"')
    assert idx > 0, "audit-enable FlagSpec must exist"
    window = src[idx:idx + 1500]
    assert "default=False" in window, (
        "audit gate MUST default False (diagnostic, opt-in)"
    )
    assert "Category.SAFETY" in window
    assert "providers.py" in window


def test_seed_audit_interval_flag():
    src = _SEED_SRC.read_text(encoding="utf-8")
    idx = src.find('name="JARVIS_CLAUDE_STREAM_BOUNDARY_AUDIT_INTERVAL_S"')
    assert idx > 0, "audit-interval FlagSpec must exist"
    window = src[idx:idx + 1200]
    assert "default=15.0" in window
    assert "Category.TUNING" in window
    assert "providers.py" in window
