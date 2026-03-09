"""Tests for CrossRepoNarrator — inbound cross-repo event narration."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock


def _make_event(event_type, repo="prime", op_id="op-001"):
    from backend.core.ouroboros.cross_repo import CrossRepoEvent, RepoType

    return CrossRepoEvent(
        id=op_id,
        type=event_type,
        source_repo=RepoType(repo) if repo in ("jarvis", "prime", "reactor") else RepoType.PRIME,
        target_repo=RepoType.JARVIS,
        payload={"op_id": op_id, "goal": "Fix test", "reason_code": "validation_failed"},
        timestamp=0.0,
    )


async def test_improvement_request_calls_emit_intent():
    from backend.core.ouroboros.governance.comms.cross_repo_narrator import CrossRepoNarrator
    from backend.core.ouroboros.cross_repo import EventType

    comm = MagicMock()
    comm.emit_intent = AsyncMock()
    narrator = CrossRepoNarrator(comm=comm)
    await narrator.on_improvement_request(_make_event(EventType.IMPROVEMENT_REQUEST, repo="prime"))

    comm.emit_intent.assert_awaited_once()
    call_kwargs = comm.emit_intent.call_args.kwargs
    assert "prime" in call_kwargs["goal"]


async def test_improvement_complete_calls_emit_decision_applied():
    from backend.core.ouroboros.governance.comms.cross_repo_narrator import CrossRepoNarrator
    from backend.core.ouroboros.cross_repo import EventType

    comm = MagicMock()
    comm.emit_decision = AsyncMock()
    narrator = CrossRepoNarrator(comm=comm)
    await narrator.on_improvement_complete(_make_event(EventType.IMPROVEMENT_COMPLETE, repo="prime"))

    comm.emit_decision.assert_awaited_once()
    assert comm.emit_decision.call_args.kwargs["outcome"] == "applied"


async def test_improvement_failed_calls_emit_postmortem():
    from backend.core.ouroboros.governance.comms.cross_repo_narrator import CrossRepoNarrator
    from backend.core.ouroboros.cross_repo import EventType

    comm = MagicMock()
    comm.emit_postmortem = AsyncMock()
    narrator = CrossRepoNarrator(comm=comm)
    await narrator.on_improvement_failed(_make_event(EventType.IMPROVEMENT_FAILED, repo="reactor-core"))

    comm.emit_postmortem.assert_awaited_once()


async def test_handler_never_raises_on_comm_failure():
    from backend.core.ouroboros.governance.comms.cross_repo_narrator import CrossRepoNarrator
    from backend.core.ouroboros.cross_repo import EventType

    comm = MagicMock()
    comm.emit_intent = AsyncMock(side_effect=RuntimeError("comm down"))
    narrator = CrossRepoNarrator(comm=comm)

    # Must not raise
    await narrator.on_improvement_request(_make_event(EventType.IMPROVEMENT_REQUEST))
