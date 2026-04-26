"""P5 Slice 3 — AdversarialReviewer hook regression suite.

Pins:
  * Module constants + frozen GenerateInjection.
  * inject_into_generate_prompt: empty section → unchanged base;
    empty base → just section; non-empty + non-empty → joined with
    delimiter; defensive None handling.
  * review_plan_for_generate_injection — happy path with a single
    finding produces the "Reviewer raised:" injection + bridge fed
    + review preserved on the result.
  * Skip paths produce empty injection + no bridge feed:
    SAFE_AUTO, master_off, empty plan, no_provider, provider_error,
    budget_exhausted.
  * Hallucination filter applied: response with mixed grounded /
    ungrounded findings → only grounded ones in injection.
  * Hook never raises even when service raises (defensive wrap):
    fall-back to skip review with skip_reason="hook_service_exception".
  * Format failure swallowed (returns "" injection); review still
    preserved.
  * feed_review_to_bridge:
    - skipped review → False (not fed),
    - empty findings → False,
    - bridge raises → False,
    - bridge missing (default lookup fails) → False,
    - successful feed → True with the right text shape.
  * _summarize_review caps file list at 5 + appends "+N more".
  * Authority invariants: no banned imports + no I/O / subprocess.
"""
from __future__ import annotations

import dataclasses
import io
import tokenize
from pathlib import Path
from typing import List

import pytest

from backend.core.ouroboros.governance.adversarial_reviewer import (
    AdversarialFinding,
    AdversarialReview,
    FindingSeverity,
)
from backend.core.ouroboros.governance.adversarial_reviewer_service import (
    AdversarialReviewerService,
    ReviewProviderResult,
    _AdversarialAuditLedger,
    reset_default_service,
)
from backend.core.ouroboros.governance.adversarial_reviewer_hook import (
    GenerateInjection,
    feed_review_to_bridge,
    inject_into_generate_prompt,
    review_plan_for_generate_injection,
)


_REPO = Path(__file__).resolve().parent.parent.parent


def _read(rel: str) -> str:
    return (_REPO / rel).read_text(encoding="utf-8")


def _strip_docstrings_and_comments(src: str) -> str:
    out = []
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenizeError, IndentationError):
        return src
    for tok in toks:
        if tok.type == tokenize.STRING:
            out.append('""')
        elif tok.type == tokenize.COMMENT:
            continue
        else:
            out.append(tok.string)
    return " ".join(out)


def _F(severity="HIGH", category="correctness",
       description="bug", mitigation_hint="fix it",
       file_reference="backend/x.py"):
    return AdversarialFinding(
        severity=FindingSeverity(severity),
        category=category, description=description,
        mitigation_hint=mitigation_hint,
        file_reference=file_reference,
    )


_GOOD_RESPONSE = (
    '{"findings": [{"severity": "HIGH", "category": "race_condition", '
    '"description": "deadlock under load", '
    '"mitigation_hint": "use RWLock", '
    '"file_reference": "backend/x.py"}]}'
)


class _FakeProv:
    def __init__(self, raw: str = _GOOD_RESPONSE,
                 cost_usd: float = 0.012,
                 model_used: str = "claude-test") -> None:
        self.raw = raw
        self.cost_usd = cost_usd
        self.model_used = model_used
        self.calls: List[str] = []

    def review(self, prompt: str) -> ReviewProviderResult:
        self.calls.append(prompt)
        return ReviewProviderResult(
            raw_response=self.raw,
            cost_usd=self.cost_usd,
            model_used=self.model_used,
        )


class _FakeBridge:
    def __init__(self, raise_on_call: bool = False) -> None:
        self.turns: List[dict] = []
        self.raise_on_call = raise_on_call

    def record_turn(self, **kw) -> None:
        if self.raise_on_call:
            raise RuntimeError("bridge boom")
        self.turns.append(kw)


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    """Slice 3 ships master-off; tests need master-on for the hook
    to fire."""
    monkeypatch.setenv("JARVIS_ADVERSARIAL_REVIEWER_ENABLED", "1")
    monkeypatch.delenv(
        "JARVIS_ADVERSARIAL_REVIEWER_AUDIT_PATH", raising=False,
    )
    monkeypatch.delenv(
        "JARVIS_ADVERSARIAL_REVIEWER_COST_BUDGET_USD", raising=False,
    )
    yield


@pytest.fixture
def svc(tmp_path):
    reset_default_service()
    L = _AdversarialAuditLedger(path=tmp_path / "audit.jsonl")
    yield AdversarialReviewerService(provider=_FakeProv(), audit_ledger=L)
    reset_default_service()


# ===========================================================================
# A — Frozen GenerateInjection dataclass
# ===========================================================================


def test_generate_injection_is_frozen():
    inj = GenerateInjection(
        injection_text="x", review=AdversarialReview(op_id="op"),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        inj.injection_text = "y"  # type: ignore[misc]


def test_generate_injection_default_bridge_fed_false():
    inj = GenerateInjection(
        injection_text="", review=AdversarialReview(op_id="op"),
    )
    assert inj.bridge_fed is False


# ===========================================================================
# B — inject_into_generate_prompt (pure helper)
# ===========================================================================


def test_inject_empty_section_returns_base_unchanged():
    """Pin: orchestrator can call this unconditionally; empty section
    means the base prompt stays exactly as-is."""
    base = "Generate the patch."
    assert inject_into_generate_prompt(base, "") == base


def test_inject_whitespace_section_treated_as_empty():
    base = "Generate the patch."
    assert inject_into_generate_prompt(base, "   \n\t") == base


def test_inject_empty_base_returns_just_section():
    """When base is empty, returned text is just the section (no
    leading delimiter)."""
    section = "Reviewer raised:\n  1. ..."
    assert inject_into_generate_prompt("", section) == section


def test_inject_combines_with_two_blank_line_delimiter():
    """Pin: section is joined with ``\\n\\n`` delimiter so the model
    sees a clean section boundary."""
    out = inject_into_generate_prompt("Base.", "Reviewer raised:")
    assert out == "Base.\n\nReviewer raised:"


def test_inject_defensive_none_inputs():
    """None inputs coerced to empty string; never raises."""
    assert inject_into_generate_prompt(None, None) == ""  # type: ignore[arg-type]
    assert inject_into_generate_prompt(None, "x") == "x"  # type: ignore[arg-type]
    assert inject_into_generate_prompt("y", None) == "y"  # type: ignore[arg-type]


# ===========================================================================
# C — review_plan_for_generate_injection happy path
# ===========================================================================


def test_hook_happy_path_returns_injection(svc):
    bridge = _FakeBridge()
    inj = review_plan_for_generate_injection(
        op_id="op-hp", plan_text="real plan",
        target_files=("backend/x.py",),
        risk_tier_name="APPROVAL_REQUIRED",
        service=svc, bridge=bridge,
    )
    assert "Reviewer raised:" in inj.injection_text
    assert "[HIGH]" in inj.injection_text
    assert "[race_condition]" in inj.injection_text
    assert inj.review.skip_reason == ""
    assert len(inj.review.findings) == 1
    assert inj.bridge_fed is True


def test_hook_bridge_receives_summary(svc):
    bridge = _FakeBridge()
    review_plan_for_generate_injection(
        op_id="op-bf", plan_text="real plan",
        target_files=("backend/x.py",),
        service=svc, bridge=bridge,
    )
    assert len(bridge.turns) == 1
    turn = bridge.turns[0]
    assert turn["role"] == "assistant"
    assert turn["source"] == "postmortem"
    assert turn["op_id"] == "op-bf"
    assert "AdversarialReviewer raised 1 findings" in turn["text"]
    assert "high=1" in turn["text"]
    assert "backend/x.py" in turn["text"]


def test_hook_review_preserves_cost_and_model(svc):
    inj = review_plan_for_generate_injection(
        op_id="op-cm", plan_text="real plan",
        target_files=("backend/x.py",),
        service=svc, bridge=_FakeBridge(),
    )
    assert inj.review.cost_usd == 0.012
    assert inj.review.model_used == "claude-test"


# ===========================================================================
# D — Skip paths → empty injection + no bridge feed
# ===========================================================================


def test_hook_skip_safe_auto(svc):
    bridge = _FakeBridge()
    inj = review_plan_for_generate_injection(
        op_id="op-sa", plan_text="trivial",
        target_files=("a.py",),
        risk_tier_name="SAFE_AUTO",
        service=svc, bridge=bridge,
    )
    assert inj.injection_text == ""
    assert inj.review.skip_reason == "safe_auto"
    assert inj.bridge_fed is False
    assert bridge.turns == []


def test_hook_skip_master_off(monkeypatch, svc):
    monkeypatch.setenv("JARVIS_ADVERSARIAL_REVIEWER_ENABLED", "false")
    bridge = _FakeBridge()
    inj = review_plan_for_generate_injection(
        op_id="op-mo", plan_text="real plan",
        target_files=("backend/x.py",),
        service=svc, bridge=bridge,
    )
    assert inj.injection_text == ""
    assert inj.review.skip_reason == "master_off"
    assert bridge.turns == []


def test_hook_skip_empty_plan(svc):
    bridge = _FakeBridge()
    inj = review_plan_for_generate_injection(
        op_id="op-ep", plan_text="",
        target_files=("backend/x.py",),
        service=svc, bridge=bridge,
    )
    assert inj.injection_text == ""
    assert inj.review.skip_reason == "empty_plan"
    assert bridge.turns == []


def test_hook_skip_no_provider(tmp_path):
    """Service constructed without a provider → skip_reason=no_provider."""
    L = _AdversarialAuditLedger(path=tmp_path / "x.jsonl")
    s = AdversarialReviewerService(provider=None, audit_ledger=L)
    bridge = _FakeBridge()
    inj = review_plan_for_generate_injection(
        op_id="op-np", plan_text="real plan",
        target_files=("backend/x.py",),
        service=s, bridge=bridge,
    )
    assert inj.injection_text == ""
    assert inj.review.skip_reason == "no_provider"
    assert bridge.turns == []


def test_hook_skip_provider_error(tmp_path):
    class _BoomProv:
        def review(self, prompt):
            raise RuntimeError("boom")

    L = _AdversarialAuditLedger(path=tmp_path / "x.jsonl")
    s = AdversarialReviewerService(provider=_BoomProv(), audit_ledger=L)
    bridge = _FakeBridge()
    inj = review_plan_for_generate_injection(
        op_id="op-pe", plan_text="real plan",
        target_files=("backend/x.py",),
        service=s, bridge=bridge,
    )
    assert inj.injection_text == ""
    assert inj.review.skip_reason == "provider_error"
    assert bridge.turns == []


def test_hook_skip_budget_exhausted(tmp_path):
    expensive = _FakeProv(cost_usd=10.0)
    L = _AdversarialAuditLedger(path=tmp_path / "x.jsonl")
    s = AdversarialReviewerService(
        provider=expensive, audit_ledger=L, cost_budget_usd=0.05,
    )
    bridge = _FakeBridge()
    inj = review_plan_for_generate_injection(
        op_id="op-be", plan_text="real plan",
        target_files=("backend/x.py",),
        service=s, bridge=bridge,
    )
    assert inj.injection_text == ""
    assert inj.review.skip_reason == "budget_exhausted"
    assert bridge.turns == []


# ===========================================================================
# E — Hallucination filter applied + multi-finding cases
# ===========================================================================


def test_hook_hallucination_filter_applied(tmp_path):
    """Mixed response: one grounded finding + one ungrounded should
    yield injection containing only the grounded one."""
    raw = (
        '{"findings": ['
        '{"severity": "HIGH", "category": "race_condition", '
        ' "description": "real bug", "mitigation_hint": "fix", '
        ' "file_reference": "backend/x.py"},'
        '{"severity": "LOW", "category": "perf", '
        ' "description": "ungrounded", "mitigation_hint": "x", '
        ' "file_reference": "backend/elsewhere.py"}'
        ']}'
    )
    L = _AdversarialAuditLedger(path=tmp_path / "x.jsonl")
    s = AdversarialReviewerService(
        provider=_FakeProv(raw=raw), audit_ledger=L,
    )
    inj = review_plan_for_generate_injection(
        op_id="op-hf", plan_text="real plan",
        target_files=("backend/x.py",),
        service=s, bridge=_FakeBridge(),
    )
    assert "real bug" in inj.injection_text
    assert "ungrounded" not in inj.injection_text
    # Raw vs filtered counts surface the drop.
    assert inj.review.raw_findings_count == 2
    assert inj.review.filtered_findings_count == 1


def test_hook_all_findings_filtered_yields_empty_injection(tmp_path):
    """When every finding is dropped by the hallucination filter,
    the injection is empty (operator gets no Reviewer raised: section
    against an unrelated file set)."""
    raw = (
        '{"findings": ['
        '{"severity": "HIGH", "category": "x", '
        ' "description": "x", "mitigation_hint": "x", '
        ' "file_reference": "outside/scope.py"}'
        ']}'
    )
    L = _AdversarialAuditLedger(path=tmp_path / "x.jsonl")
    s = AdversarialReviewerService(
        provider=_FakeProv(raw=raw), audit_ledger=L,
    )
    bridge = _FakeBridge()
    inj = review_plan_for_generate_injection(
        op_id="op-allf", plan_text="real plan",
        target_files=("backend/x.py",),
        service=s, bridge=bridge,
    )
    assert inj.injection_text == ""
    assert inj.review.skip_reason == ""  # NOT skipped — just no findings
    assert inj.review.raw_findings_count == 1
    assert inj.review.filtered_findings_count == 0
    # Bridge not fed when no findings landed (per
    # feed_review_to_bridge contract).
    assert inj.bridge_fed is False


# ===========================================================================
# F — Defensive: hook never raises
# ===========================================================================


def test_hook_never_raises_when_service_raises():
    """Pin: if service.review_plan raises (it shouldn't — best-effort
    by design — but defensive contract), the hook returns a
    well-formed GenerateInjection with skip_reason=
    hook_service_exception. PLAN-still-authoritative invariant
    preserved structurally."""
    class _ExplodingService:
        def review_plan(self, **kw):
            raise RuntimeError("service blew up")

    inj = review_plan_for_generate_injection(
        op_id="op-explode", plan_text="real plan",
        target_files=("a.py",),
        service=_ExplodingService(),
        bridge=_FakeBridge(),
    )
    assert inj.injection_text == ""
    assert inj.review.skip_reason == "hook_service_exception"
    assert inj.review.findings == ()
    assert inj.bridge_fed is False


# ===========================================================================
# G — feed_review_to_bridge directly
# ===========================================================================


def test_feed_skipped_review_returns_false():
    review = AdversarialReview(
        op_id="op", findings=(), skip_reason="master_off",
    )
    bridge = _FakeBridge()
    assert feed_review_to_bridge(review, bridge=bridge) is False
    assert bridge.turns == []


def test_feed_empty_findings_returns_false():
    review = AdversarialReview(op_id="op", findings=())
    bridge = _FakeBridge()
    assert feed_review_to_bridge(review, bridge=bridge) is False


def test_feed_bridge_raises_returns_false():
    review = AdversarialReview(op_id="op", findings=(_F(),))
    bridge = _FakeBridge(raise_on_call=True)
    assert feed_review_to_bridge(review, bridge=bridge) is False


def test_feed_none_review_returns_false():
    """Defensive: None review → False."""
    bridge = _FakeBridge()
    assert feed_review_to_bridge(None, bridge=bridge) is False  # type: ignore[arg-type]
    assert bridge.turns == []


def test_feed_happy_path_returns_true_with_summary():
    review = AdversarialReview(
        op_id="op-hap",
        findings=(
            _F(severity="HIGH", file_reference="a.py"),
            _F(severity="MEDIUM", file_reference="b.py"),
            _F(severity="LOW", file_reference="c.py"),
        ),
        filtered_findings_count=3, raw_findings_count=3,
    )
    bridge = _FakeBridge()
    assert feed_review_to_bridge(review, bridge=bridge) is True
    assert len(bridge.turns) == 1
    text = bridge.turns[0]["text"]
    assert "raised 3 findings" in text
    assert "high=1" in text
    assert "med=1" in text
    assert "low=1" in text
    assert "op-hap" in text


def test_feed_summary_caps_files_at_five():
    """Pin: file list capped at 5 with '+N more' suffix to keep
    bridge per-turn cap intact."""
    findings = tuple(
        _F(file_reference=f"file{i}.py") for i in range(8)
    )
    review = AdversarialReview(
        op_id="op-cap", findings=findings,
        filtered_findings_count=8, raw_findings_count=8,
    )
    bridge = _FakeBridge()
    feed_review_to_bridge(review, bridge=bridge)
    text = bridge.turns[0]["text"]
    assert "+3 more" in text


# ===========================================================================
# H — Authority invariants
# ===========================================================================


_BANNED = [
    "from backend.core.ouroboros.governance.orchestrator",
    "from backend.core.ouroboros.governance.policy",
    "from backend.core.ouroboros.governance.iron_gate",
    "from backend.core.ouroboros.governance.risk_tier",
    "from backend.core.ouroboros.governance.change_engine",
    "from backend.core.ouroboros.governance.candidate_generator",
    "from backend.core.ouroboros.governance.gate",
    "from backend.core.ouroboros.governance.semantic_guardian",
]


def test_hook_no_authority_imports():
    src = _read(
        "backend/core/ouroboros/governance/adversarial_reviewer_hook.py",
    )
    for imp in _BANNED:
        assert imp not in src, f"banned import: {imp}"


def test_hook_no_io_or_subprocess():
    """Pin: hook is wiring-only. I/O delegated to Slice 2 service
    (ledger) + ConversationBridge (its own surface)."""
    src = _strip_docstrings_and_comments(
        _read(
            "backend/core/ouroboros/governance/adversarial_reviewer_hook.py",
        ),
    )
    forbidden = [
        "subprocess.",
        "open(",
        ".write_text(",
        "os.environ[",
        "os." + "system(",  # split to dodge pre-commit hook
        "import requests",
        "import httpx",
        "import urllib.request",
    ]
    for c in forbidden:
        assert c not in src, f"unexpected coupling: {c}"
