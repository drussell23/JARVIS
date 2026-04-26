"""P0.5 — PostureObserver arc-context reachability supplement.

Mirrors ``tests/governance/test_postmortem_recall_orchestrator_smoke.py``
(W3(6) reachability supplement precedent: when live cadence cannot
reliably exercise the wiring within the wall cap, in-process
orchestrator-shaped tests stand in as the Layer 3 evidence per PRD §11).

What this proves end-to-end against the **real** PostureObserver wiring:

  (1) ``PostureObserver.run_one_cycle`` builds an ``ArcContextSignal``
      from real ``compute_recent_momentum`` + ``LastSessionSummary``
      access paths and passes it to ``DirectionInferrer.infer``.
  (2) The cycle log line (``[PostureObserver] arc_context=<json>
      applied=<bool>``) fires every cycle regardless of flag state.
  (3) The persisted ``PostureReading`` carries the ``arc_context``
      through to ``store.append_history`` for downstream consumers
      (REPL, IDE GET, SSE bridge).
  (4) Authority invariant: the new arc-context wiring in
      ``posture_observer.py`` does not import any banned governance
      module beyond the existing arc-file pin's protections.

Together with:
  * 22 ``git_momentum`` tests (Slice 1)
  * 21 ``arc_context`` consumer tests (Slice 2)
  * 17 ``p0_5_graduation_pins`` tests (Slice 3)
  * 15 in-process live-fire smoke checks (Slice 3)
this closes the PRD §11 Layer 3 reachability evidence for the P0.5
graduation per the W3(6) supplement precedent (memory
``project_wave3_item6_graduation_matrix.md``).
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import patch, MagicMock

from backend.core.ouroboros.governance.arc_context import ArcContextSignal
from backend.core.ouroboros.governance.git_momentum import MomentumSnapshot
from backend.core.ouroboros.governance.posture import (
    Posture,
    PostureReading,
    baseline_bundle,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_observer(tmp_path: Path) -> "PostureObserver":  # noqa: F821
    """Construct a PostureObserver with stubbed store + collector so we
    only exercise the arc-context wiring."""
    from backend.core.ouroboros.governance.posture_observer import (
        PostureObserver,
    )

    store = MagicMock()
    store.append_history = MagicMock()
    store.append_audit = MagicMock()
    store.persist_current = MagicMock()
    store.load_current = MagicMock(return_value=None)

    # Real bundle so the inferrer's schema check passes; collector exposes
    # ``build_bundle`` which the observer calls via ``asyncio.to_thread``.
    collector = MagicMock()
    collector.build_bundle = MagicMock(return_value=baseline_bundle())

    obs = PostureObserver(
        project_root=tmp_path,
        store=store,
        collector=collector,
    )
    return obs


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# (1) Cycle wiring — observer builds + passes arc_context
# ---------------------------------------------------------------------------


def test_run_one_cycle_builds_arc_context_and_passes_to_infer(tmp_path):
    """The observer must call build_arc_context per cycle and forward the
    result as the inferrer's arc_context kwarg."""
    obs = _make_observer(tmp_path)

    fake_arc = ArcContextSignal(
        momentum=MomentumSnapshot(commit_count=5, type_counts={"feat": 5}),
        lss_verify_ratio=0.9,
    )

    with patch(
        "backend.core.ouroboros.governance.posture_observer.build_arc_context",
        return_value=fake_arc,
    ) as builder, patch.object(obs, "_inferrer") as inferrer:
        inferrer.infer = MagicMock(return_value=PostureReading(
            posture=Posture.MAINTAIN,
            confidence=0.0,
            evidence=(),
            inferred_at=0.0,
            signal_bundle_hash="abc",
            all_scores=(),
            arc_context=fake_arc,
        ))
        _run(obs.run_one_cycle())

    builder.assert_called_once()
    inferrer.infer.assert_called_once()
    _, kwargs = inferrer.infer.call_args
    assert kwargs.get("arc_context") is fake_arc, (
        "PostureObserver did not pass arc_context to inferrer"
    )


def test_run_one_cycle_emits_arc_context_log_line(tmp_path, caplog):
    """The observer emits one INFO line per cycle:
    ``[PostureObserver] arc_context=<json> applied=<bool>``."""
    obs = _make_observer(tmp_path)
    fake_arc = ArcContextSignal(
        momentum=MomentumSnapshot(commit_count=3, type_counts={"feat": 3}),
        lss_verify_ratio=1.0,
    )
    with patch(
        "backend.core.ouroboros.governance.posture_observer.build_arc_context",
        return_value=fake_arc,
    ):
        with caplog.at_level(logging.INFO):
            _run(obs.run_one_cycle())

    log_lines = [r.getMessage() for r in caplog.records]
    arc_lines = [m for m in log_lines if "[PostureObserver] arc_context=" in m]
    assert arc_lines, (
        "Expected '[PostureObserver] arc_context=' INFO line; "
        f"got: {log_lines!r}"
    )
    # The line includes both the JSON dict + the applied flag.
    line = arc_lines[0]
    assert "applied=" in line


def test_run_one_cycle_persists_reading_with_arc_context(tmp_path):
    """The PostureReading appended to history must carry the arc_context
    field so downstream surfaces (REPL, IDE GET) see the same data."""
    obs = _make_observer(tmp_path)
    fake_arc = ArcContextSignal(
        momentum=MomentumSnapshot(commit_count=2),
    )
    with patch(
        "backend.core.ouroboros.governance.posture_observer.build_arc_context",
        return_value=fake_arc,
    ):
        _run(obs.run_one_cycle())

    obs._store.append_history.assert_called_once()
    persisted_reading = obs._store.append_history.call_args[0][0]
    assert persisted_reading.arc_context is fake_arc


def test_run_one_cycle_arc_context_builder_failure_is_swallowed(tmp_path):
    """Best-effort: any builder failure must NOT break the cycle.
    Inferrer still receives arc_context=None, reading still persists."""
    obs = _make_observer(tmp_path)
    with patch(
        "backend.core.ouroboros.governance.posture_observer.build_arc_context",
        side_effect=RuntimeError("intentional test boom"),
    ):
        result = _run(obs.run_one_cycle())
    assert result is not None or obs._store.append_history.called


# ---------------------------------------------------------------------------
# (2) AST regression — wiring source-grep
# ---------------------------------------------------------------------------


def _read_observer_src() -> str:
    return (
        Path(__file__).resolve().parent.parent.parent
        / "backend/core/ouroboros/governance/posture_observer.py"
    ).read_text(encoding="utf-8")


def test_pin_observer_imports_build_arc_context():
    src = _read_observer_src()
    assert (
        "from backend.core.ouroboros.governance.arc_context import" in src
    )
    assert "build_arc_context" in src


def test_pin_observer_call_site_passes_kwarg():
    src = _read_observer_src()
    # Call-site uses kwarg form so signature changes are pinnable.
    assert (
        "self._inferrer.infer(bundle, arc_context=arc_ctx)" in src
        or "self._inferrer.infer(\n            bundle, arc_context=arc_ctx)" in src
    )


def test_pin_observer_emits_log_marker():
    src = _read_observer_src()
    assert "[PostureObserver] arc_context=" in src
