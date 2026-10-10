"""A measurement through O+V's own parser must not teach O+V.

Seen live 2026-10-10: the held-out baseline recorded ``[LessonMemory] ...
op=heldout-...`` -- the benchmark's tasks were leaking into the organism's
lessons (and could into capability evidence and the training corpus), so
the held-out set stopped being held out.
"""
from __future__ import annotations

import asyncio
from typing import Any, List

import pytest

from backend.core.ouroboros.governance import evaluation_scope as es


def test_the_marker_is_scoped_and_follows_into_tasks_and_threads() -> None:
    seen: List[str] = []

    async def go() -> None:
        with es.measuring("bench"):
            seen.append(es.measuring_label())
            await asyncio.get_running_loop().create_task(_capture(seen))
            seen.append(await asyncio.to_thread(es.measuring_label))
        seen.append(es.measuring_label())

    async def _capture(out: List[str]) -> None:
        out.append(es.measuring_label())

    asyncio.run(go())
    assert seen == ["bench", "bench", "bench", ""]


def test_lessons_are_not_persisted_inside_a_measurement(monkeypatch) -> None:
    from backend.core.ouroboros.governance import failure_mode_memory as fmm
    from backend.core.ouroboros.governance import lesson_memory
    writes: List[Any] = []
    monkeypatch.setattr(fmm, "record_failure_mode", lambda rec: writes.append(rec) or "new")
    with es.measuring("bench"):
        assert lesson_memory.record_lesson_sync(object()) == "suppressed_evaluation"
    assert writes == []
    lesson_memory.record_lesson_sync(object())
    assert len(writes) == 1, "outside a measurement lessons persist as before"


def test_capability_evidence_is_not_written_inside_a_measurement(tmp_path, monkeypatch) -> None:
    from backend.core.ouroboros.governance import served_model_capability as smc
    monkeypatch.setenv("JARVIS_SERVED_CAPABILITY_LEDGER_PATH", str(tmp_path / "cap.jsonl"))
    led = smc.ServedCapabilityLedger()
    with es.measuring("bench"):
        assert led.record("m", False, op_id="heldout-1") is False
    assert led.recent("m") == ()
    assert led.record("m", True, op_id="op-1") is True


def test_a_measured_answer_is_not_a_trajectory_row(monkeypatch) -> None:
    from backend.core.ouroboros.governance.observability import trajectory_recorder as tr
    calls: List[Any] = []
    monkeypatch.setattr(tr, "get_recorder", lambda: type(
        "R", (), {"record_generation": lambda self, **k: calls.append(k) or True})())
    with es.measuring("bench"):
        assert tr.record_generation(op_id="heldout-1") is False
    assert calls == []
    assert tr.record_generation(op_id="op-1") is True


def test_the_evaluation_scope_marks_its_body(monkeypatch) -> None:
    """The ONE scope the benchmark and the handoff's smoke share sets it."""
    from backend.core.ouroboros.governance.observability import training_handoff as th
    from backend.core.ouroboros.governance import local_inference_director as lid

    async def _http(*a: Any, **k: Any) -> None:
        return None

    class _Client:
        def __init__(self, cfg: Any) -> None:
            pass

    monkeypatch.setattr(th, "_http", _http)
    monkeypatch.setattr(lid, "LocalPrimeClient", _Client)
    labels: List[str] = []

    async def go() -> None:
        async with th.evaluation_scope("m", ttl_s=10):
            labels.append(es.measuring_label())
        labels.append(es.measuring_label())

    asyncio.run(go())
    assert labels[0].startswith("evaluation:") and labels[1] == ""
