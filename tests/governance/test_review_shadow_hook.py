"""Phase B Slice 1a — post-VALIDATE REVIEW subagent SHADOW MODE.

Pins the observer contract: the hook can evaluate, emit telemetry, and
learn — but it MUST NOT alter FSM state, mutate risk tier, route retries,
or raise. Slice 1b will promote this observer into an authority after a
3-consecutive-clean battle-test arc.
"""
from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from backend.core.ouroboros.governance.orchestrator import GovernedOrchestrator
from backend.core.ouroboros.governance.subagent_contracts import (
    SubagentResult,
    SubagentStatus,
    SubagentType,
)


def _make_orch(project_root: Path, sub_orch: Any = None) -> GovernedOrchestrator:
    """Construct a GovernedOrchestrator with enough state for
    ``_run_review_shadow``. Bypasses ``__init__`` because the real
    constructor wires half the governance stack — the shadow hook only
    needs ``_config.project_root`` and ``_subagent_orchestrator``.
    """
    inst = object.__new__(GovernedOrchestrator)
    inst._config = SimpleNamespace(project_root=project_root)
    inst._subagent_orchestrator = sub_orch
    return inst


def _make_sub_result(
    verdict: str = "APPROVE",
    score: float = 1.0,
    status: SubagentStatus = SubagentStatus.COMPLETED,
) -> SubagentResult:
    return SubagentResult(
        schema_version="subagent.1",
        subagent_id="test-sub",
        subagent_type=SubagentType.REVIEW,
        status=status,
        goal="test review",
        started_at_ns=0,
        finished_at_ns=1_000_000,
        findings=(),
        files_read=(),
        search_queries=(),
        summary=f"REVIEW verdict={verdict} score={score:.2f}",
        cost_usd=0.0,
        tool_calls=0,
        error_class="",
        error_detail="",
        type_payload=(
            ("verdict", verdict),
            ("semantic_integrity_score", round(score, 3)),
        ),
    )


class _SpySubOrchestrator:
    """Records dispatch_review calls without spawning a real subagent."""

    def __init__(self, result: SubagentResult) -> None:
        self._result = result
        self.calls: list = []

    async def dispatch_review(
        self,
        parent_ctx: Any,
        file_path: str,
        pre_apply_content: str,
        candidate_content: str,
        generation_intent: str,
        timeout_s: float = 60.0,
    ) -> SubagentResult:
        self.calls.append(
            dict(
                file_path=file_path,
                pre_apply_content_len=len(pre_apply_content),
                candidate_content_len=len(candidate_content),
                generation_intent=generation_intent,
                timeout_s=timeout_s,
            )
        )
        return self._result


def _ctx(op_id: str = "op-test", description: str = "shadow hook test") -> Any:
    return SimpleNamespace(op_id=op_id, description=description)


def _candidate_single(path: str = "backend/foo.py", content: str = "pass\n") -> dict:
    return {"file_path": path, "full_content": content}


def _candidate_multi(*pairs: tuple[str, str]) -> dict:
    return {
        "files": [
            {"file_path": p, "full_content": c} for p, c in pairs
        ],
    }


# ---------------------------------------------------------------------------
# (1) Flag off → no dispatch, no telemetry
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_shadow_flag_off_yields_no_dispatch(
    tmp_path: Path, monkeypatch: Any, caplog: Any
) -> None:
    monkeypatch.delenv("JARVIS_REVIEW_SUBAGENT_SHADOW", raising=False)
    spy = _SpySubOrchestrator(_make_sub_result())
    orch = _make_orch(tmp_path, spy)

    with caplog.at_level(logging.INFO):
        await orch._run_review_shadow(_ctx(), _candidate_single())

    assert spy.calls == [], "dispatch_review must not be called when flag is off"
    assert not any(
        "[REVIEW-SHADOW]" in r.getMessage() for r in caplog.records
    ), "no telemetry line when flag is off"


# ---------------------------------------------------------------------------
# (2) No orchestrator wired → no dispatch even with flag on
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_shadow_no_orchestrator_noops(
    tmp_path: Path, monkeypatch: Any, caplog: Any
) -> None:
    monkeypatch.setenv("JARVIS_REVIEW_SUBAGENT_SHADOW", "true")
    orch = _make_orch(tmp_path, sub_orch=None)

    with caplog.at_level(logging.DEBUG):
        await orch._run_review_shadow(_ctx(), _candidate_single())

    assert not any(
        "[REVIEW-SHADOW]" in r.getMessage() for r in caplog.records
    )


# ---------------------------------------------------------------------------
# (3) Flag on + orchestrator wired → dispatch + telemetry, returns None
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_shadow_on_dispatches_and_emits(
    tmp_path: Path, monkeypatch: Any, caplog: Any
) -> None:
    monkeypatch.setenv("JARVIS_REVIEW_SUBAGENT_SHADOW", "true")
    spy = _SpySubOrchestrator(_make_sub_result(verdict="APPROVE", score=1.0))
    orch = _make_orch(tmp_path, spy)
    target = tmp_path / "foo.py"
    target.write_text("# old\npass\n")

    with caplog.at_level(logging.INFO):
        result = await orch._run_review_shadow(
            _ctx(op_id="op-xyz"),
            _candidate_single(path="foo.py", content="# new\npass\n"),
        )

    assert result is None, "observer hook must never return a value"
    assert len(spy.calls) == 1
    call = spy.calls[0]
    assert call["file_path"] == "foo.py"
    assert call["pre_apply_content_len"] == len("# old\npass\n")
    assert call["candidate_content_len"] == len("# new\npass\n")
    assert call["generation_intent"] == "shadow hook test"

    lines = [r.getMessage() for r in caplog.records if "[REVIEW-SHADOW]" in r.getMessage()]
    assert len(lines) == 1, f"expected exactly one shadow line, got {len(lines)}: {lines}"
    assert "op=op-xyz" in lines[0]
    assert "aggregate=APPROVE" in lines[0]
    assert "files_reviewed=1" in lines[0]
    assert "approved=1" in lines[0]
    assert "observer — FSM proceeds regardless" in lines[0]


# ---------------------------------------------------------------------------
# (4) dispatch_review raises → exception swallowed, FSM proceeds
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_shadow_swallows_dispatch_exceptions(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setenv("JARVIS_REVIEW_SUBAGENT_SHADOW", "true")

    class _ExplodingSub:
        async def dispatch_review(self, **_: Any) -> SubagentResult:
            raise RuntimeError("simulated subagent explosion")

    orch = _make_orch(tmp_path, _ExplodingSub())

    # Must not raise — the observer contract forbids it.
    result = await orch._run_review_shadow(_ctx(), _candidate_single())
    assert result is None


# ---------------------------------------------------------------------------
# (5) All three verdicts yield identical FSM outcome (observer)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "verdict,expected_aggregate,expected_count_key",
    [
        ("APPROVE", "APPROVE", "approved"),
        ("APPROVE_WITH_RESERVATIONS", "APPROVE_WITH_RESERVATIONS", "reservations"),
        ("REJECT", "REJECT", "rejected"),
    ],
)
async def test_shadow_every_verdict_yields_no_fsm_mutation(
    tmp_path: Path, monkeypatch: Any, caplog: Any,
    verdict: str, expected_aggregate: str, expected_count_key: str,
) -> None:
    monkeypatch.setenv("JARVIS_REVIEW_SUBAGENT_SHADOW", "true")
    spy = _SpySubOrchestrator(_make_sub_result(verdict=verdict, score=0.5))
    orch = _make_orch(tmp_path, spy)

    # Snapshot state before — nothing on orch should change after the call.
    pre_state = dict(orch.__dict__)

    with caplog.at_level(logging.INFO):
        result = await orch._run_review_shadow(_ctx(), _candidate_single())

    assert result is None
    assert orch.__dict__ == pre_state, (
        f"observer mutated orchestrator state under {verdict} verdict"
    )

    lines = [r.getMessage() for r in caplog.records if "[REVIEW-SHADOW]" in r.getMessage()]
    assert len(lines) == 1
    assert f"aggregate={expected_aggregate}" in lines[0]
    assert f"{expected_count_key}=1" in lines[0]


# ---------------------------------------------------------------------------
# (6) Multi-file candidate aggregates to worst verdict
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_shadow_multi_file_aggregates_to_worst_verdict(
    tmp_path: Path, monkeypatch: Any, caplog: Any
) -> None:
    monkeypatch.setenv("JARVIS_REVIEW_SUBAGENT_SHADOW", "true")

    class _PerFileSub:
        """Returns verdict based on file path so aggregation is deterministic."""
        def __init__(self) -> None:
            self.calls: list = []

        async def dispatch_review(
            self, *, file_path: str, **_: Any
        ) -> SubagentResult:
            self.calls.append(file_path)
            if "reject" in file_path:
                return _make_sub_result(verdict="REJECT", score=0.1)
            if "reservations" in file_path:
                return _make_sub_result(
                    verdict="APPROVE_WITH_RESERVATIONS", score=0.7
                )
            return _make_sub_result(verdict="APPROVE", score=1.0)

    sub = _PerFileSub()
    orch = _make_orch(tmp_path, sub)

    with caplog.at_level(logging.INFO):
        await orch._run_review_shadow(
            _ctx(),
            _candidate_multi(
                ("clean.py", "pass\n"),
                ("reservations_here.py", "pass\n"),
                ("reject_me.py", "pass\n"),
            ),
        )

    assert len(sub.calls) == 3
    lines = [r.getMessage() for r in caplog.records if "[REVIEW-SHADOW]" in r.getMessage()]
    assert len(lines) == 1
    assert "aggregate=REJECT" in lines[0], (
        "REJECT must dominate APPROVE_WITH_RESERVATIONS + APPROVE"
    )
    assert "files_reviewed=3" in lines[0]
    assert "approved=1" in lines[0]
    assert "reservations=1" in lines[0]
    assert "rejected=1" in lines[0]


# ---------------------------------------------------------------------------
# (7) Empty / malformed candidate is a clean no-op
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_shadow_no_candidate_noops(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("JARVIS_REVIEW_SUBAGENT_SHADOW", "true")
    spy = _SpySubOrchestrator(_make_sub_result())
    orch = _make_orch(tmp_path, spy)

    await orch._run_review_shadow(_ctx(), None)
    assert spy.calls == []


# ---------------------------------------------------------------------------
# (8) set_subagent_orchestrator attaches and detaches cleanly
# ---------------------------------------------------------------------------

def test_set_subagent_orchestrator_attach_detach(tmp_path: Path) -> None:
    orch = _make_orch(tmp_path)
    assert orch._subagent_orchestrator is None

    sentinel = MagicMock(name="SubOrch")
    orch.set_subagent_orchestrator(sentinel)
    assert orch._subagent_orchestrator is sentinel

    orch.set_subagent_orchestrator(None)
    assert orch._subagent_orchestrator is None
