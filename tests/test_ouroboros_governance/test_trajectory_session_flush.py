"""Trajectories still awaiting a verdict must survive session teardown.

A generation is written only when its op reports a verdict, so every op in
flight when a soak ends is holding an unwritten trajectory.
``TrajectoryRecorder.aclose()`` was BUILT to flush exactly those -- and
nothing ever called it. A correct code path that no caller reaches is
indistinguishable, from the corpus's point of view, from one that does not
exist.

Measured on soak bt-2026-08-31-164353: 7 generations queued, 2 rows on
disk. The missing 5 were not dropped by a bug in the recorder; they were
discarded at teardown because the flush never ran. That is 71% of the
session's harvest, on a pipeline whose entire purpose is harvesting.

These tests pin BOTH halves, because either alone would have passed while
the data was being lost:

  1. ``aclose()`` writes pending generations that never got a verdict, and
  2. the battle-test harness actually CALLS it.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List

import pytest

from backend.core.ouroboros.governance.observability import (
    trajectory_recorder as tr,
)


def _cand(h: str) -> Dict[str, Any]:
    return {
        "candidate_id": h, "candidate_hash": h,
        "file_path": "m.py", "full_content": f"# {h}\n",
    }


@pytest.fixture()
def _recorder(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JARVIS_TRAJECTORY_RECORDER_ENABLED", "true")
    # Explicit path override: naming the directory env var wrong silently
    # redirects writes to the operator's REAL corpus.
    tr.reset_recorder_for_tests(path=tmp_path / "events" / "trajectories.jsonl")
    yield tr
    tr.reset_recorder_for_tests()


def _pending(op_id: str):
    return tr._PendingGeneration(
        op_id=op_id, prompt="fix the pagination helper", prompt_key="pk",
        candidates=(_cand(f"{op_id}-a"),),
        model_id="qwen2.5-coder:32b", provider_name="local_prime",
        is_noop=False, latency_ms=30000.0, prompt_tokens=27769,
        completion_tokens=100, cost_usd=0.0, task_type="code_repair",
        session_id="s", tokens_estimated=False,
    )


def test_aclose_flushes_generations_that_never_got_a_verdict(_recorder) -> None:
    """The in-flight harvest is written, not discarded."""

    async def _go() -> List[Dict[str, Any]]:
        rec = tr.get_recorder()
        for op in ("op-a", "op-b", "op-c"):
            await rec._admit_pending(_pending(op))
        assert len(rec._pending) == 3, "precondition: all three awaiting verdicts"

        await rec.aclose(timeout_s=5.0)

        return [
            json.loads(ln)
            for ln in rec.path.read_text(encoding="utf-8").splitlines() if ln
        ]

    rows = asyncio.run(_go())
    assert {r["metadata"]["op_id"] for r in rows} == {"op-a", "op-b", "op-c"}
    # No verdict ever arrived, so they must be honestly labelled unknown and
    # NOT trainable -- a flushed row must not be laundered into a success.
    assert {r["outcome"] for r in rows} == {"unknown"}
    assert all(r["metadata"]["should_train"] is False for r in rows)
    # The token measurement survives the flush; it is the reason these rows
    # are worth keeping at all.
    assert all(r["completion_tokens"] == 100 for r in rows)
    assert all(r["tokens_estimated"] is False for r in rows)


def test_aclose_restores_the_ttl_it_borrowed(_recorder) -> None:
    """The flush forces expiry by lowering the TTL; it must put it back.

    Leaking a 30s TTL into the process would make the NEXT session drop
    every generation whose op takes longer than half a minute.
    """
    import os

    async def _go() -> None:
        rec = tr.get_recorder()
        await rec._admit_pending(_pending("op-ttl"))
        await rec.aclose(timeout_s=5.0)

    sentinel = "777"
    os.environ["JARVIS_TRAJECTORY_RECORDER_PENDING_TTL_S"] = sentinel
    try:
        asyncio.run(_go())
        assert os.environ["JARVIS_TRAJECTORY_RECORDER_PENDING_TTL_S"] == sentinel
    finally:
        os.environ.pop("JARVIS_TRAJECTORY_RECORDER_PENDING_TTL_S", None)


def test_aclose_is_safe_with_nothing_pending(_recorder) -> None:
    """Teardown runs on every session, including ones that generated nothing."""

    async def _go() -> None:
        await tr.get_recorder().aclose(timeout_s=5.0)

    asyncio.run(_go())  # must not raise


def test_harness_actually_calls_the_flush() -> None:
    """The half that was missing.

    ``aclose`` was correct and unreachable. Asserting the recorder's
    behaviour alone would have stayed green through the entire data loss,
    so this pins the CALL SITE: the battle-test shutdown path must invoke
    the recorder's flush, and must do so before the component drain that
    the code itself documents as "slow, possibly-hanging".
    """
    from pathlib import Path

    src = Path(
        "backend/core/ouroboros/battle_test/harness.py"
    ).read_text(encoding="utf-8", errors="replace")

    assert "trajectory_recorder import" in src, (
        "harness no longer imports the trajectory recorder at shutdown"
    )
    assert "aclose(timeout_s=" in src, (
        "harness no longer calls the recorder's final flush -- every "
        "in-flight trajectory is being discarded at session end"
    )

    flush = src.index("aclose(timeout_s=")
    drain = src.index("await self._shutdown_components()")
    report = src.index("await self._generate_report()")
    assert flush < drain, (
        "the flush must precede the component drain: that drain is the "
        "hang-prone part, and this is a data-loss boundary"
    )
    assert flush < report, (
        "the flush must precede report generation so the summary counts a "
        "complete corpus"
    )
