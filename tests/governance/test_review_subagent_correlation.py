"""REVIEW subagent correlation harness — Slice 1b graduation floor.

The graduation threshold from project_phase_b_subagent_roadmap.md:
  "3 consecutive sessions where REVIEW's verdict agrees with the actual
   APPLY+VERIFY outcome on ≥ 90% of ops."

That metric only becomes measurable if REVIEW actually produces
different verdicts on different *classes* of candidates. This harness
pins that contract as a unit-test-level invariant: if a known-bad
fixture can't be driven to REJECT and a known-good fixture can't reach
APPROVE, no amount of battle-testing will show the signal we need.

Six cases — three REJECT correlates, two APPROVE correlates, one
APPROVE_WITH_RESERVATIONS correlate. Each case is a minimal, readable
diff that deliberately exercises one of the three verdict paths:

  APPROVE  ← whitespace-only refactor, identical semantics.
  APPROVE  ← comment-only docstring tweak.
  REJECT   ← credential_shape_introduced (security-critical forced).
  REJECT   ← silent stubbing (function_body_collapsed, hard severity).
  REJECT   ← function deleted outright (function-name loss × 3).
  APPROVE_WITH_RESERVATIONS ← soft-severity pattern hits in isolation.

These are not replacements for the existing test_review_subagent.py
unit tests — they are *correlation* proofs that the verdict function
spans the full three-tier output space under realistic-shape inputs.
Slice 1a unit tests proved each mechanism in isolation; this harness
proves the cross-cutting behavior stays coherent.
"""
from __future__ import annotations

import datetime
from pathlib import Path
from typing import Tuple
from unittest.mock import MagicMock

import pytest

from backend.core.ouroboros.governance.agentic_review_subagent import (
    AgenticReviewSubagent,
)
from backend.core.ouroboros.governance.subagent_contracts import (
    REVIEW_VERDICT_APPROVE,
    REVIEW_VERDICT_APPROVE_WITH_RESERVATIONS,
    REVIEW_VERDICT_REJECT,
    SubagentContext,
    SubagentRequest,
    SubagentStatus,
    SubagentType,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_review_ctx(
    *, pre: str, new: str, tmp_path: Path,
    file_path: str = "src/target.py",
    intent: str = "refactor under review",
) -> SubagentContext:
    req = SubagentRequest(
        subagent_type=SubagentType.REVIEW,
        goal=f"review {file_path}",
        target_files=(file_path,),
        parallel_scopes=1,
        review_target_candidate={
            "file_path": file_path,
            "pre_apply_content": pre,
            "candidate_content": new,
            "generation_intent": intent,
        },
    )
    parent_ctx = MagicMock()
    parent_ctx.op_id = "op-corr-review"
    return SubagentContext(
        parent_op_id="op-corr-review",
        parent_ctx=parent_ctx,
        subagent_id="op-corr-review::sub-01",
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


async def _run_review(pre: str, new: str, tmp_path: Path) -> Tuple[str, float]:
    """Run the reviewer and return (verdict, score)."""
    reviewer = AgenticReviewSubagent(project_root=tmp_path)
    ctx = _make_review_ctx(pre=pre, new=new, tmp_path=tmp_path)
    result = await reviewer.review(ctx)
    assert result.status == SubagentStatus.COMPLETED, (
        f"expected COMPLETED, got {result.status} "
        f"(error={result.error_class}: {result.error_detail})"
    )
    payload = dict(result.type_payload)
    return str(payload["verdict"]), float(payload["semantic_integrity_score"])


# ---------------------------------------------------------------------------
# APPROVE correlates — semantically equivalent changes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_approve_whitespace_only_refactor(tmp_path: Path) -> None:
    """Pure whitespace change → no pattern hits → APPROVE with max score."""
    pre = (
        "def add(a, b):\n"
        "    return a + b\n"
    )
    new = (
        "def add(a, b):\n"
        "    # trivial re-indent; same semantics\n"
        "    return a + b\n"
    )
    verdict, score = await _run_review(pre=pre, new=new, tmp_path=tmp_path)
    assert verdict == REVIEW_VERDICT_APPROVE, (
        f"whitespace-only refactor should APPROVE; got {verdict} score={score}"
    )
    assert score >= 0.80


@pytest.mark.asyncio
async def test_approve_comment_only_tweak(tmp_path: Path) -> None:
    """Adding a comment inside a function body → APPROVE, full score."""
    pre = (
        "def greet(name):\n"
        "    return f'hello {name}'\n"
    )
    new = (
        "def greet(name):\n"
        "    # clarifies intent; no behavior change\n"
        "    return f'hello {name}'\n"
    )
    verdict, score = await _run_review(pre=pre, new=new, tmp_path=tmp_path)
    assert verdict == REVIEW_VERDICT_APPROVE
    assert score >= 0.80


# ---------------------------------------------------------------------------
# REJECT correlates — hard patterns + security-critical + silent stubbing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reject_credential_shape_introduced(tmp_path: Path) -> None:
    """Credential-shape pattern always forces REJECT regardless of score."""
    pre = (
        "def load_config():\n"
        "    return {}\n"
    )
    new = (
        "def load_config():\n"
        "    return {\n"
        "        'api_key': 'sk-1234567890abcdefghijklmnopqrstuvwxyz',\n"
        "    }\n"
    )
    verdict, _ = await _run_review(pre=pre, new=new, tmp_path=tmp_path)
    assert verdict == REVIEW_VERDICT_REJECT, (
        f"credential shape must force REJECT; got {verdict}"
    )


@pytest.mark.asyncio
async def test_reject_silent_stubbing_via_body_collapse(tmp_path: Path) -> None:
    """function_body_collapsed hard-pattern → REJECT.

    The silent-stub detector identified in the Slice 1b scoping
    decision (the pattern we're relying on instead of building a full
    AST-tree hash). Old body is substantive; new body is a bare
    ``raise NotImplementedError`` — same name, gutted behavior.
    """
    pre = (
        "def validate_user(user):\n"
        "    if not user:\n"
        "        return False\n"
        "    if not user.get('email'):\n"
        "        return False\n"
        "    if len(user.get('email', '')) < 3:\n"
        "        return False\n"
        "    return True\n"
    )
    new = (
        "def validate_user(user):\n"
        "    raise NotImplementedError\n"
    )
    verdict, score = await _run_review(pre=pre, new=new, tmp_path=tmp_path)
    assert verdict == REVIEW_VERDICT_REJECT, (
        f"body collapse must REJECT; got {verdict} score={score}"
    )


@pytest.mark.asyncio
async def test_reject_mass_function_deletion(tmp_path: Path) -> None:
    """Three functions deleted outright → function-name loss drives score
    below the reject floor (3 × 0.20 = 0.60 penalty on a 1.0 baseline)."""
    pre = (
        "def step_one(x):\n    return x + 1\n\n"
        "def step_two(x):\n    return x * 2\n\n"
        "def step_three(x):\n    return x - 3\n\n"
        "def entry(x):\n    return step_three(step_two(step_one(x)))\n"
    )
    new = (
        "def entry(x):\n    return x  # TODO: restore pipeline\n"
    )
    verdict, score = await _run_review(pre=pre, new=new, tmp_path=tmp_path)
    assert verdict == REVIEW_VERDICT_REJECT, (
        f"mass deletion must REJECT; got {verdict} score={score}"
    )
    # Baseline 1.0 − (3 × 0.20) = 0.40; below the approve_with_reservations
    # floor at 0.55, so REJECT is the correct verdict.
    assert score < 0.55


# ---------------------------------------------------------------------------
# APPROVE_WITH_RESERVATIONS correlate — soft pattern alone
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_approve_with_reservations_on_stacked_soft_patterns(
    tmp_path: Path,
) -> None:
    """Two independent soft-severity pattern hits should drop into the
    middle band (1.0 − 2×0.12 = 0.76, below the 0.80 APPROVE floor and
    above the 0.55 reservations floor).

    Uses ``silent_exception_swallow`` + ``hardcoded_url_swap`` — both
    soft-severity SemanticGuardian patterns. Neither is a hard fail on
    its own; stacked, they correctly surface the reservations band.

    Design note: a *single* soft pattern intentionally stays in APPROVE
    (0.88) — the author's tuning says one minor concern is acknowledged
    via the `reservations` field without blocking the verdict. This
    test pins the threshold behavior where stacking crosses the band.
    """
    pre = (
        "def fetch(text):\n"
        "    import json\n"
        "    url = 'https://api.example.com/v1/parse'\n"
        "    _ = url\n"
        "    return json.loads(text)\n"
    )
    new = (
        "def fetch(text):\n"
        "    import json\n"
        "    url = 'https://api.staging.example.com/v1/parse'\n"
        "    _ = url\n"
        "    try:\n"
        "        return json.loads(text)\n"
        "    except Exception:\n"
        "        pass\n"
    )
    verdict, score = await _run_review(pre=pre, new=new, tmp_path=tmp_path)
    assert verdict == REVIEW_VERDICT_APPROVE_WITH_RESERVATIONS, (
        f"stacked soft patterns must land in reservations band; "
        f"got {verdict} score={score}"
    )
    # Score lives in the [0.55, 0.80) band by construction.
    assert 0.55 <= score < 0.80, f"score {score} outside reservations band"
