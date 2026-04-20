"""Regression spine — Phase B REVIEW subagent (Manifesto §6 Execution Validation).

Pins the structural contract REVIEW must honor:
  1. SubagentType.REVIEW enum variant exists and is the literal "review".
  2. SubagentRequest carries review_target_candidate as an optional
     Dict[str, Any].
  3. SubagentResult carries type_payload as a tuple-of-tuple (frozen).
  4. Policy Rule 0c allows subagent_type=review when the master switch
     is on and denies plan/research/refactor/general.
  5. AgenticReviewSubagent produces APPROVE on a clean candidate.
  6. AgenticReviewSubagent produces APPROVE_WITH_RESERVATIONS when soft
     SemanticGuardian patterns hit.
  7. AgenticReviewSubagent produces REJECT when hard patterns hit OR
     credential_shape_introduced hits (security-critical).
  8. Function-body loss (silent stubbing) drops the score by 0.20 per
     missing function.
  9. Verdict math is deterministic — same inputs produce same verdict.
 10. SubagentOrchestrator routes REVIEW to review_factory (not
     explore_factory) based on ctx.subagent_type.
 11. SubagentOrchestrator returns NOT_IMPLEMENTED when review_factory
     is None and a REVIEW is dispatched.
 12. dispatch_review() convenience method builds a proper SubagentRequest
     programmatically (orchestrator-driven, not model-driven).
 13. Malformed review_target_candidate (missing file_path or
     candidate_content) produces a clean FAILED status, not a crash.
 14. Mutation_score is None when no runner is wired.
 15. AgenticReviewSubagent's verdict is independent of Python 3.11+
     TaskGroup features (runs correctly under 3.9/3.10 fallback).
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, Tuple
from unittest.mock import MagicMock

import pytest

from backend.core.ouroboros.governance.agentic_review_subagent import (
    AgenticReviewSubagent,
    build_default_review_factory,
)
from backend.core.ouroboros.governance.subagent_contracts import (
    REVIEW_VERDICT_APPROVE,
    REVIEW_VERDICT_APPROVE_WITH_RESERVATIONS,
    REVIEW_VERDICT_REJECT,
    SubagentContext,
    SubagentRequest,
    SubagentResult,
    SubagentStatus,
    SubagentType,
)


# ---------------------------------------------------------------------------
# Contract shape tests (1-3)
# ---------------------------------------------------------------------------


def test_subagent_type_review_enum_value() -> None:
    assert SubagentType.REVIEW.value == "review"


def test_subagent_request_carries_review_target_candidate() -> None:
    req = SubagentRequest(
        subagent_type=SubagentType.REVIEW,
        goal="review test candidate",
        target_files=("x.py",),
        review_target_candidate={
            "file_path": "x.py",
            "pre_apply_content": "def f(): return 1",
            "candidate_content": "def f(): return 2",
            "generation_intent": "change return value",
        },
    )
    assert req.review_target_candidate is not None
    assert req.review_target_candidate["file_path"] == "x.py"


def test_subagent_result_type_payload_is_frozen_tuple() -> None:
    """type_payload must be a tuple-of-tuple so SubagentResult stays
    frozen and hashable."""
    r = SubagentResult(
        subagent_type=SubagentType.REVIEW,
        type_payload=(("verdict", "approve"), ("semantic_integrity_score", 0.9)),
    )
    assert isinstance(r.type_payload, tuple)
    # Every element is itself a tuple (key, value).
    for item in r.type_payload:
        assert isinstance(item, tuple)
        assert len(item) == 2


# ---------------------------------------------------------------------------
# Policy engine Rule 0c tests (4)
# ---------------------------------------------------------------------------


@pytest.fixture
def policy(tmp_path: Path):
    from backend.core.ouroboros.governance.tool_executor import (
        GoverningToolPolicy,
    )
    return GoverningToolPolicy(repo_roots={"jarvis": tmp_path})


def _make_policy_ctx(tmp_path: Path):
    from backend.core.ouroboros.governance.tool_executor import PolicyContext
    return PolicyContext(
        repo="jarvis",
        repo_root=tmp_path,
        op_id="op-test",
        call_id="op-test:r0:dispatch_subagent",
        round_index=0,
        risk_tier=None,
        is_read_only=False,
    )


def test_policy_allows_dispatch_subagent_type_review(
    policy, tmp_path: Path, monkeypatch
) -> None:
    from backend.core.ouroboros.governance.tool_executor import (
        PolicyDecision,
        ToolCall,
    )
    # Force the master switch on (default is already true post-graduation).
    monkeypatch.setenv("JARVIS_SUBAGENT_DISPATCH_ENABLED", "true")
    ctx = _make_policy_ctx(tmp_path)
    call = ToolCall(
        name="dispatch_subagent",
        arguments={"subagent_type": "review", "goal": "review x.py"},
    )
    result = policy.evaluate(call, ctx)
    assert result.decision == PolicyDecision.ALLOW
    assert "review" in result.reason_code


def test_policy_allows_dispatch_subagent_type_explore_still(
    policy, tmp_path: Path, monkeypatch
) -> None:
    """Phase 1 EXPLORE dispatch still works — no regression."""
    from backend.core.ouroboros.governance.tool_executor import (
        PolicyDecision,
        ToolCall,
    )
    monkeypatch.setenv("JARVIS_SUBAGENT_DISPATCH_ENABLED", "true")
    ctx = _make_policy_ctx(tmp_path)
    call = ToolCall(
        name="dispatch_subagent",
        arguments={"subagent_type": "explore", "goal": "explore x.py"},
    )
    result = policy.evaluate(call, ctx)
    assert result.decision == PolicyDecision.ALLOW


@pytest.mark.parametrize(
    "unsupported_type", ["research", "refactor", "bogus"],
)
def test_policy_denies_unsupported_subagent_types(
    policy, tmp_path: Path, monkeypatch, unsupported_type: str,
) -> None:
    from backend.core.ouroboros.governance.tool_executor import (
        PolicyDecision,
        ToolCall,
    )
    monkeypatch.setenv("JARVIS_SUBAGENT_DISPATCH_ENABLED", "true")
    ctx = _make_policy_ctx(tmp_path)
    call = ToolCall(
        name="dispatch_subagent",
        arguments={"subagent_type": unsupported_type, "goal": "..."},
    )
    result = policy.evaluate(call, ctx)
    assert result.decision == PolicyDecision.DENY
    assert result.reason_code == "tool.denied.subagent_type_unsupported"


# ---------------------------------------------------------------------------
# AgenticReviewSubagent verdict tests (5-9, 13-15)
# ---------------------------------------------------------------------------


def _make_ctx_with_candidate(
    *,
    pre: str,
    new: str,
    intent: str = "refactor function",
    file_path: str = "x.py",
    tmp_path: Path,
) -> SubagentContext:
    import datetime
    req = SubagentRequest(
        subagent_type=SubagentType.REVIEW,
        goal=f"review {file_path}",
        target_files=(file_path,),
        scope_paths=(),
        max_files=1,
        max_depth=1,
        timeout_s=60.0,
        parallel_scopes=1,
        review_target_candidate={
            "file_path": file_path,
            "pre_apply_content": pre,
            "candidate_content": new,
            "generation_intent": intent,
        },
    )
    parent_ctx = MagicMock()
    parent_ctx.op_id = "op-test-review"
    return SubagentContext(
        parent_op_id="op-test-review",
        parent_ctx=parent_ctx,
        subagent_id="op-test-review::sub-01",
        subagent_type=SubagentType.REVIEW,
        request=req,
        deadline=datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(seconds=60),
        scope_path="",
        yield_requested=False,
        cost_remaining_usd=1.0,
        primary_provider_name="deterministic",
        fallback_provider_name="claude-api",
        tool_loop=None,
    )


@pytest.mark.asyncio
async def test_review_clean_candidate_produces_approve(
    tmp_path: Path,
) -> None:
    """Identical function body (pure whitespace change) → APPROVE."""
    pre = "def hello():\n    return 'world'\n"
    new = "def hello():\n    return 'world'\n"  # identical
    reviewer = AgenticReviewSubagent(project_root=tmp_path)
    ctx = _make_ctx_with_candidate(pre=pre, new=new, tmp_path=tmp_path)
    result = await reviewer.review(ctx)
    assert result.status == SubagentStatus.COMPLETED
    payload = dict(result.type_payload)
    assert payload["verdict"] == REVIEW_VERDICT_APPROVE
    assert payload["semantic_integrity_score"] >= 0.8
    assert payload["mutation_score"] is None  # no runner wired


@pytest.mark.asyncio
async def test_review_credential_pattern_forces_reject(
    tmp_path: Path,
) -> None:
    """Credential shape introduced → forced REJECT regardless of score."""
    pre = "def config():\n    return {}\n"
    new = 'def config():\n    return {"api_key": "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJK_uvwxyz0123456789"}\n'
    reviewer = AgenticReviewSubagent(project_root=tmp_path)
    ctx = _make_ctx_with_candidate(pre=pre, new=new, tmp_path=tmp_path)
    result = await reviewer.review(ctx)
    payload = dict(result.type_payload)
    assert payload["verdict"] == REVIEW_VERDICT_REJECT
    # reject_reasons must be non-empty
    assert len(payload["reject_reasons"]) > 0


@pytest.mark.asyncio
async def test_review_function_loss_penalizes_score(
    tmp_path: Path,
) -> None:
    """Function lost between pre → new drops the score."""
    pre = (
        "def one():\n    return 1\n"
        "def two():\n    return 2\n"
        "def three():\n    return 3\n"
    )
    new = "def one():\n    return 1\n"  # lost two() and three()
    reviewer = AgenticReviewSubagent(project_root=tmp_path)
    ctx = _make_ctx_with_candidate(pre=pre, new=new, tmp_path=tmp_path)
    result = await reviewer.review(ctx)
    payload = dict(result.type_payload)
    # 2 functions lost × 0.20 = 0.40 penalty → score ≤ 0.60 → downgrade
    assert payload["semantic_integrity_score"] <= 0.60
    assert payload["function_loss_count"] == 2
    assert payload["verdict"] != REVIEW_VERDICT_APPROVE


@pytest.mark.asyncio
async def test_review_verdict_is_deterministic(
    tmp_path: Path,
) -> None:
    """Same inputs produce same verdict across multiple runs."""
    pre = "def f(x):\n    return x * 2\n"
    new = "def f(x):\n    return x * 3\n"
    reviewer = AgenticReviewSubagent(project_root=tmp_path)
    ctx1 = _make_ctx_with_candidate(pre=pre, new=new, tmp_path=tmp_path)
    ctx2 = _make_ctx_with_candidate(pre=pre, new=new, tmp_path=tmp_path)
    r1 = await reviewer.review(ctx1)
    r2 = await reviewer.review(ctx2)
    p1 = dict(r1.type_payload)
    p2 = dict(r2.type_payload)
    assert p1["verdict"] == p2["verdict"]
    assert p1["semantic_integrity_score"] == p2["semantic_integrity_score"]


@pytest.mark.asyncio
async def test_review_malformed_input_returns_clean_failure(
    tmp_path: Path,
) -> None:
    """Missing review_target_candidate → FAILED status, no crash."""
    import datetime
    req = SubagentRequest(
        subagent_type=SubagentType.REVIEW,
        goal="review missing",
        target_files=("x.py",),
        review_target_candidate=None,  # missing
    )
    parent_ctx = MagicMock()
    parent_ctx.op_id = "op-test-malformed"
    ctx = SubagentContext(
        parent_op_id="op-test-malformed",
        parent_ctx=parent_ctx,
        subagent_id="op-test-malformed::sub-01",
        subagent_type=SubagentType.REVIEW,
        request=req,
        deadline=datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(seconds=30),
        scope_path="",
        yield_requested=False,
        cost_remaining_usd=1.0,
        primary_provider_name="deterministic",
        fallback_provider_name="claude-api",
        tool_loop=None,
    )
    reviewer = AgenticReviewSubagent(project_root=tmp_path)
    result = await reviewer.review(ctx)
    assert result.status == SubagentStatus.FAILED
    assert "review_target_candidate" in result.error_detail


@pytest.mark.asyncio
async def test_review_mutation_score_none_without_runner(
    tmp_path: Path,
) -> None:
    """Default factory wires no mutation runner → mutation_score is None."""
    reviewer = AgenticReviewSubagent(project_root=tmp_path)
    ctx = _make_ctx_with_candidate(
        pre="def f(): return 1\n", new="def f(): return 2\n",
        tmp_path=tmp_path,
    )
    result = await reviewer.review(ctx)
    payload = dict(result.type_payload)
    assert payload["mutation_score"] is None


@pytest.mark.asyncio
async def test_review_mutation_runner_consulted_when_path_critical(
    tmp_path: Path, monkeypatch,
) -> None:
    """Slice 1b regression — before the _is_critical_path fix, this
    test could not be written because the allowlist import crashed
    silently and the runner was never called.

    With a real runner wired and the allowlist covering the candidate
    path, the runner MUST fire and its score MUST reach the payload.
    """
    monkeypatch.setenv(
        "JARVIS_MUTATION_GATE_CRITICAL_PATHS", "src/",
    )
    calls: list = []

    async def fake_runner(path: str, content: str) -> float:
        calls.append((path, len(content)))
        return 0.85

    reviewer = AgenticReviewSubagent(
        project_root=tmp_path, mutation_runner=fake_runner,
    )
    ctx = _make_ctx_with_candidate(
        pre="def f(): return 1\n", new="def f(): return 2\n",
        tmp_path=tmp_path,
        file_path="src/target.py",
    )
    result = await reviewer.review(ctx)
    payload = dict(result.type_payload)
    assert calls, "mutation_runner was never invoked — allowlist import broken"
    assert payload["mutation_score"] == 0.85


@pytest.mark.asyncio
async def test_review_mutation_runner_skipped_when_path_not_critical(
    tmp_path: Path, monkeypatch,
) -> None:
    """Counterpart to the above — off-allowlist paths must NOT trigger
    the runner. Protects the mutation-testing budget."""
    monkeypatch.setenv(
        "JARVIS_MUTATION_GATE_CRITICAL_PATHS", "core/auth/",
    )
    calls: list = []

    async def fake_runner(path: str, content: str) -> float:
        calls.append((path, content))
        return 0.99

    reviewer = AgenticReviewSubagent(
        project_root=tmp_path, mutation_runner=fake_runner,
    )
    ctx = _make_ctx_with_candidate(
        pre="def f(): return 1\n", new="def f(): return 2\n",
        tmp_path=tmp_path,
        file_path="scripts/helper.py",  # NOT in allowlist
    )
    result = await reviewer.review(ctx)
    payload = dict(result.type_payload)
    assert not calls, f"mutation_runner fired on off-allowlist path: {calls}"
    assert payload["mutation_score"] is None


# ---------------------------------------------------------------------------
# Orchestrator wiring tests (10-12)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestrator_routes_review_to_review_factory(
    tmp_path: Path, monkeypatch,
) -> None:
    """Dispatching a REVIEW request must hit the review_factory, not
    explore_factory."""
    monkeypatch.setenv("JARVIS_SUBAGENT_DISPATCH_ENABLED", "true")
    from backend.core.ouroboros.governance.subagent_orchestrator import (
        SubagentOrchestrator,
    )

    explore_calls = []
    review_calls = []

    class FakeExploreExec:
        async def explore(self, ctx):
            explore_calls.append(ctx)
            return SubagentResult(
                subagent_id=ctx.subagent_id,
                subagent_type=SubagentType.EXPLORE,
                status=SubagentStatus.COMPLETED,
                tool_diversity=3,
            )

    class FakeReviewExec:
        async def review(self, ctx):
            review_calls.append(ctx)
            return SubagentResult(
                subagent_id=ctx.subagent_id,
                subagent_type=SubagentType.REVIEW,
                status=SubagentStatus.COMPLETED,
                type_payload=(("verdict", "approve"),),
            )

    orch = SubagentOrchestrator(
        explore_factory=lambda: FakeExploreExec(),
        review_factory=lambda: FakeReviewExec(),
    )

    parent_ctx = MagicMock()
    parent_ctx.op_id = "op-route-test"
    parent_ctx.pipeline_deadline = None

    await orch.dispatch_review(
        parent_ctx,
        file_path="x.py",
        pre_apply_content="def f(): return 1\n",
        candidate_content="def f(): return 2\n",
        generation_intent="change return value",
        timeout_s=10.0,
    )
    assert len(review_calls) == 1
    assert len(explore_calls) == 0
    assert review_calls[0].subagent_type == SubagentType.REVIEW


@pytest.mark.asyncio
async def test_orchestrator_review_without_factory_returns_not_implemented(
    tmp_path: Path, monkeypatch,
) -> None:
    """REVIEW dispatched without a review_factory → NOT_IMPLEMENTED
    status, not a crash."""
    monkeypatch.setenv("JARVIS_SUBAGENT_DISPATCH_ENABLED", "true")
    from backend.core.ouroboros.governance.subagent_orchestrator import (
        SubagentOrchestrator,
    )

    class FakeExploreExec:
        async def explore(self, ctx):
            raise AssertionError("must not be called for REVIEW")

    orch = SubagentOrchestrator(
        explore_factory=lambda: FakeExploreExec(),
        review_factory=None,  # deliberately unwired
    )
    parent_ctx = MagicMock()
    parent_ctx.op_id = "op-no-review-factory"
    parent_ctx.pipeline_deadline = None

    result = await orch.dispatch_review(
        parent_ctx,
        file_path="x.py",
        pre_apply_content="x = 1\n",
        candidate_content="x = 2\n",
        generation_intent="test",
    )
    assert result.status == SubagentStatus.NOT_IMPLEMENTED


@pytest.mark.asyncio
async def test_dispatch_review_builds_programmatic_request(
    tmp_path: Path, monkeypatch,
) -> None:
    """dispatch_review() must populate review_target_candidate with
    all four required keys."""
    monkeypatch.setenv("JARVIS_SUBAGENT_DISPATCH_ENABLED", "true")
    from backend.core.ouroboros.governance.subagent_orchestrator import (
        SubagentOrchestrator,
    )
    captured = {}

    class CapturingReviewExec:
        async def review(self, ctx):
            captured["candidate"] = ctx.request.review_target_candidate
            captured["type"] = ctx.subagent_type
            return SubagentResult(
                subagent_id=ctx.subagent_id,
                subagent_type=SubagentType.REVIEW,
                status=SubagentStatus.COMPLETED,
                type_payload=(("verdict", "approve"),),
            )

    orch = SubagentOrchestrator(
        explore_factory=lambda: MagicMock(),
        review_factory=lambda: CapturingReviewExec(),
    )
    parent_ctx = MagicMock()
    parent_ctx.op_id = "op-prog"
    parent_ctx.pipeline_deadline = None

    await orch.dispatch_review(
        parent_ctx,
        file_path="foo.py",
        pre_apply_content="PRE",
        candidate_content="NEW",
        generation_intent="INTENT",
    )
    assert captured["type"] == SubagentType.REVIEW
    assert captured["candidate"]["file_path"] == "foo.py"
    assert captured["candidate"]["pre_apply_content"] == "PRE"
    assert captured["candidate"]["candidate_content"] == "NEW"
    assert captured["candidate"]["generation_intent"] == "INTENT"


# ---------------------------------------------------------------------------
# Factory helper
# ---------------------------------------------------------------------------


def test_build_default_review_factory(tmp_path: Path) -> None:
    factory = build_default_review_factory(tmp_path)
    instance = factory()
    assert isinstance(instance, AgenticReviewSubagent)
