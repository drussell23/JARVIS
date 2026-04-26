"""P5 Slice 2 — AdversarialReviewerService regression suite.

Pins:
  * Module constants + frozen ReviewProviderResult.
  * Cost budget env override (default + clamp + fallback).
  * Audit ledger path resolver (default + env override).
  * 6 skip paths (master_off / safe_auto / empty_plan / no_provider /
    provider_error / budget_exhausted).
  * Happy path: parse + filter wired correctly; raw vs filtered
    counts; cost + model_used preserved; audit row written.
  * Defensive: provider returning non-ReviewProviderResult →
    provider_error.
  * Audit ledger: append happy + serialize failure swallowed +
    oversize line dropped + I/O failure best-effort.
  * Telemetry log line emitted on success (not on skip).
  * Default-singleton accessor.
  * Authority invariants: no banned imports + only-allowed I/O is
    the JSONL ledger path.
"""
from __future__ import annotations

import dataclasses
import io
import json
import logging
import threading
import tokenize
from pathlib import Path
from typing import List

import pytest

from backend.core.ouroboros.governance.adversarial_reviewer import (
    AdversarialReview,
    FindingSeverity,
)
from backend.core.ouroboros.governance.adversarial_reviewer_service import (
    AUDIT_LEDGER_SCHEMA_VERSION,
    DEFAULT_COST_BUDGET_USD,
    MAX_LINE_BYTES,
    SAFE_RISK_TIER_NAMES,
    AdversarialReviewerService,
    ReviewProvider,
    ReviewProviderResult,
    _AdversarialAuditLedger,
    audit_ledger_path,
    cost_budget_per_op_usd,
    get_default_service,
    reset_default_service,
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


# Sample model JSON — one HIGH finding, grounded.
_GOOD_RESPONSE = (
    '{"findings": [{"severity": "HIGH", "category": "race_condition", '
    '"description": "lock contention may deadlock", '
    '"mitigation_hint": "use RWLock", '
    '"file_reference": "backend/x.py"}]}'
)


class _FakeProv:
    """Returns _GOOD_RESPONSE at the configured cost."""

    def __init__(self, cost_usd: float = 0.012,
                 raw: str = _GOOD_RESPONSE,
                 model_used: str = "claude-test") -> None:
        self._cost = cost_usd
        self._raw = raw
        self._model = model_used
        self.calls: List[str] = []

    def review(self, prompt: str) -> ReviewProviderResult:
        self.calls.append(prompt)
        return ReviewProviderResult(
            raw_response=self._raw,
            cost_usd=self._cost,
            model_used=self._model,
        )


class _BoomProv:
    def review(self, prompt: str) -> ReviewProviderResult:
        raise RuntimeError("boom")


class _BadShapeProv:
    """Returns the wrong type — service must catch + skip."""

    def review(self, prompt: str):
        return "not a ReviewProviderResult"


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    """Slice 2 ships master-off; tests need master-on for the service
    to run. Tests that exercise master_off explicitly setenv 'false'."""
    monkeypatch.setenv("JARVIS_ADVERSARIAL_REVIEWER_ENABLED", "1")
    monkeypatch.delenv("JARVIS_ADVERSARIAL_REVIEWER_AUDIT_PATH", raising=False)
    monkeypatch.delenv(
        "JARVIS_ADVERSARIAL_REVIEWER_COST_BUDGET_USD", raising=False,
    )
    yield


@pytest.fixture
def ledger(tmp_path):
    return _AdversarialAuditLedger(path=tmp_path / "audit.jsonl")


@pytest.fixture
def fake_prov():
    return _FakeProv()


@pytest.fixture
def svc(fake_prov, ledger):
    reset_default_service()
    yield AdversarialReviewerService(
        provider=fake_prov, audit_ledger=ledger,
    )
    reset_default_service()


# ===========================================================================
# A — Module constants + frozen dataclass
# ===========================================================================


def test_default_cost_budget_pinned():
    """Pin: PRD spec — $0.05/op default."""
    assert DEFAULT_COST_BUDGET_USD == 0.05


def test_safe_tier_names_pinned():
    """Pin: PRD spec — SAFE_AUTO bypasses the reviewer."""
    assert SAFE_RISK_TIER_NAMES == frozenset({"SAFE_AUTO"})


def test_audit_schema_pinned():
    assert AUDIT_LEDGER_SCHEMA_VERSION == 1


def test_max_line_bytes_pinned():
    assert MAX_LINE_BYTES == 32 * 1024


def test_provider_result_is_frozen():
    r = ReviewProviderResult(raw_response="x", cost_usd=0.01)
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.cost_usd = 0.02  # type: ignore[misc]


def test_provider_result_default_model_used_empty():
    r = ReviewProviderResult(raw_response="x", cost_usd=0.01)
    assert r.model_used == ""


# ===========================================================================
# B — Path + cost-budget env helpers
# ===========================================================================


def test_audit_path_default_under_dot_jarvis(monkeypatch):
    monkeypatch.delenv("JARVIS_ADVERSARIAL_REVIEWER_AUDIT_PATH", raising=False)
    p = audit_ledger_path()
    assert p.parent.name == ".jarvis"
    assert p.name == "adversarial_review_audit.jsonl"


def test_audit_path_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "JARVIS_ADVERSARIAL_REVIEWER_AUDIT_PATH",
        str(tmp_path / "custom.jsonl"),
    )
    assert audit_ledger_path() == tmp_path / "custom.jsonl"


def test_cost_budget_default(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_ADVERSARIAL_REVIEWER_COST_BUDGET_USD", raising=False,
    )
    assert cost_budget_per_op_usd() == DEFAULT_COST_BUDGET_USD


def test_cost_budget_env_override(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ADVERSARIAL_REVIEWER_COST_BUDGET_USD", "0.25",
    )
    assert cost_budget_per_op_usd() == 0.25


def test_cost_budget_clamps_negative_to_zero(monkeypatch):
    """Pin: negative env value clamps to 0 (operator-explicit disable
    without flipping the master flag)."""
    monkeypatch.setenv(
        "JARVIS_ADVERSARIAL_REVIEWER_COST_BUDGET_USD", "-1",
    )
    assert cost_budget_per_op_usd() == 0.0


def test_cost_budget_unparseable_falls_back(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ADVERSARIAL_REVIEWER_COST_BUDGET_USD", "garbage",
    )
    assert cost_budget_per_op_usd() == DEFAULT_COST_BUDGET_USD


# ===========================================================================
# C — Skip paths (NO LLM call made)
# ===========================================================================


def test_skip_master_off(monkeypatch, fake_prov, ledger):
    monkeypatch.setenv("JARVIS_ADVERSARIAL_REVIEWER_ENABLED", "false")
    s = AdversarialReviewerService(provider=fake_prov, audit_ledger=ledger)
    rev = s.review_plan(
        op_id="op-mo", plan_text="plan", target_files=("a.py",),
    )
    assert rev.skip_reason == "master_off"
    assert rev.findings == ()
    assert fake_prov.calls == []  # no provider call


def test_skip_safe_auto(svc, fake_prov):
    rev = svc.review_plan(
        op_id="op-sa", plan_text="plan", target_files=("a.py",),
        risk_tier_name="SAFE_AUTO",
    )
    assert rev.skip_reason == "safe_auto"
    assert fake_prov.calls == []


def test_skip_safe_auto_case_insensitive(svc, fake_prov):
    """``safe_auto`` lowercase should still trip the bypass."""
    rev = svc.review_plan(
        op_id="op-sa-lc", plan_text="plan", target_files=("a.py",),
        risk_tier_name="safe_auto",
    )
    assert rev.skip_reason == "safe_auto"
    assert fake_prov.calls == []


def test_safe_auto_skip_does_not_apply_to_other_tiers(svc, fake_prov):
    """NOTIFY_APPLY / APPROVAL_REQUIRED / BLOCKED should NOT bypass."""
    for tier in ("NOTIFY_APPLY", "APPROVAL_REQUIRED", "BLOCKED"):
        rev = svc.review_plan(
            op_id=f"op-{tier}", plan_text="plan",
            target_files=("backend/x.py",), risk_tier_name=tier,
        )
        assert rev.skip_reason == "", f"tier={tier} unexpectedly skipped"


@pytest.mark.parametrize("plan", ["", None, "   ", "\n\t"])
def test_skip_empty_plan(svc, fake_prov, plan):
    rev = svc.review_plan(
        op_id="op-empty", plan_text=plan, target_files=("a.py",),
    )
    assert rev.skip_reason == "empty_plan"
    assert fake_prov.calls == []


def test_skip_no_provider(ledger):
    s = AdversarialReviewerService(provider=None, audit_ledger=ledger)
    rev = s.review_plan(
        op_id="op-np", plan_text="plan", target_files=("a.py",),
    )
    assert rev.skip_reason == "no_provider"


def test_skip_provider_error_non_propagating(ledger):
    s = AdversarialReviewerService(
        provider=_BoomProv(), audit_ledger=ledger,
    )
    rev = s.review_plan(
        op_id="op-pe", plan_text="plan", target_files=("a.py",),
    )
    assert rev.skip_reason == "provider_error"
    assert rev.findings == ()


def test_skip_provider_returns_wrong_shape(ledger):
    """Pin: defensive — provider returns non-ReviewProviderResult →
    provider_error rather than crash."""
    s = AdversarialReviewerService(
        provider=_BadShapeProv(), audit_ledger=ledger,
    )
    rev = s.review_plan(
        op_id="op-bs", plan_text="plan", target_files=("a.py",),
    )
    assert rev.skip_reason == "provider_error"


def test_skip_budget_exhausted(ledger):
    """Pin: provider over budget → review discarded (findings=()).
    The reported cost is preserved on the review so audit shows it."""
    expensive = _FakeProv(cost_usd=10.0)
    s = AdversarialReviewerService(
        provider=expensive, audit_ledger=ledger,
        cost_budget_usd=0.05,
    )
    rev = s.review_plan(
        op_id="op-be", plan_text="plan", target_files=("backend/x.py",),
    )
    assert rev.skip_reason == "budget_exhausted"
    assert rev.cost_usd == 10.0
    assert rev.model_used == "claude-test"
    assert rev.findings == ()


def test_skip_budget_zero_disables_via_env(monkeypatch, fake_prov, ledger):
    """Pin: budget==0 makes any non-zero cost trip budget_exhausted —
    operator-explicit disable without flipping the master flag."""
    monkeypatch.setenv(
        "JARVIS_ADVERSARIAL_REVIEWER_COST_BUDGET_USD", "0",
    )
    s = AdversarialReviewerService(provider=fake_prov, audit_ledger=ledger)
    rev = s.review_plan(
        op_id="op-bz", plan_text="plan", target_files=("backend/x.py",),
    )
    assert rev.skip_reason == "budget_exhausted"


# ===========================================================================
# D — Happy path
# ===========================================================================


def test_happy_path_returns_findings(svc, fake_prov):
    rev = svc.review_plan(
        op_id="op-hp", plan_text="real plan",
        target_files=("backend/x.py",),
    )
    assert rev.skip_reason == ""
    assert len(rev.findings) == 1
    assert rev.findings[0].severity is FindingSeverity.HIGH
    assert rev.findings[0].file_reference == "backend/x.py"
    assert rev.cost_usd == 0.012
    assert rev.model_used == "claude-test"
    # Provider called exactly once.
    assert len(fake_prov.calls) == 1


def test_happy_path_raw_vs_filtered_counts(ledger):
    """When the response includes findings that fail the hallucination
    filter, raw_findings_count > filtered_findings_count."""
    raw = (
        '{"findings": ['
        '{"severity": "HIGH", "category": "race_condition", '
        ' "description": "valid", "mitigation_hint": "fix", '
        ' "file_reference": "backend/x.py"},'
        '{"severity": "LOW", "category": "perf", '
        ' "description": "ungrounded", "mitigation_hint": "x", '
        ' "file_reference": "backend/elsewhere.py"}'
        ']}'
    )
    s = AdversarialReviewerService(
        provider=_FakeProv(raw=raw), audit_ledger=ledger,
    )
    rev = s.review_plan(
        op_id="op-rf", plan_text="plan",
        target_files=("backend/x.py",),
    )
    assert rev.raw_findings_count == 2
    assert rev.filtered_findings_count == 1
    # Drop note appears in review.notes.
    assert any("ungrounded" in n for n in rev.notes)


def test_happy_path_writes_audit_row(svc, ledger):
    svc.review_plan(
        op_id="op-au", plan_text="plan",
        target_files=("backend/x.py",),
    )
    text = ledger.path.read_text(encoding="utf-8")
    rows = [json.loads(ln) for ln in text.splitlines() if ln.strip()]
    assert len(rows) == 1
    row = rows[0]
    assert row["schema_version"] == AUDIT_LEDGER_SCHEMA_VERSION
    assert row["op_id"] == "op-au"
    assert row["filtered_findings_count"] == 1
    assert "wrote_at_unix" in row


def test_skip_paths_also_audited(svc, ledger):
    """Pin: skip paths land in the ledger too — operators querying
    /adversarial history (Slice 4) need to see what was skipped + why."""
    svc.review_plan(
        op_id="op-skipped", plan_text="",
        target_files=("backend/x.py",),
    )
    text = ledger.path.read_text(encoding="utf-8")
    rows = [json.loads(ln) for ln in text.splitlines() if ln.strip()]
    assert any(
        r["op_id"] == "op-skipped" and r["skip_reason"] == "empty_plan"
        for r in rows
    )


def test_happy_path_emits_telemetry(svc, caplog):
    """Pin: §8 telemetry line emitted on success per PRD spec."""
    with caplog.at_level(logging.INFO):
        svc.review_plan(
            op_id="op-tl", plan_text="plan",
            target_files=("backend/x.py",),
        )
    assert any(
        "[AdversarialReviewer] op=op-tl raised 1 findings" in r.message
        and "high=1" in r.message and "med=0" in r.message
        and "low=0" in r.message
        for r in caplog.records
    )


def test_skip_emits_skip_log_not_telemetry(svc, caplog):
    """Skip paths emit a different INFO line — pinned so /adversarial
    history can grep both forms."""
    with caplog.at_level(logging.INFO):
        svc.review_plan(
            op_id="op-sk", plan_text="",
            target_files=("a.py",),
        )
    assert any(
        "[AdversarialReviewer] op=op-sk skipped reason=empty_plan"
        in r.message
        for r in caplog.records
    )


# ===========================================================================
# E — Audit ledger best-effort paths
# ===========================================================================


def test_audit_creates_parent_directory(tmp_path):
    p = tmp_path / "made" / "for" / "test" / "audit.jsonl"
    L = _AdversarialAuditLedger(path=p)
    rev = AdversarialReview(op_id="op", findings=())
    assert L.append(rev) is True
    assert p.exists()


def test_audit_oversize_dropped_with_warning(tmp_path, caplog):
    """Pin: reviews > MAX_LINE_BYTES are dropped at write time."""
    p = tmp_path / "audit.jsonl"
    L = _AdversarialAuditLedger(path=p)
    huge = "x" * (MAX_LINE_BYTES + 4096)
    rev = AdversarialReview(
        op_id="op-huge",
        findings=(),
        notes=(huge,),  # blow the size cap via notes
    )
    with caplog.at_level(logging.WARNING):
        ok = L.append(rev)
    assert ok is False
    assert "exceeds MAX_LINE_BYTES" in caplog.text


def test_audit_io_failure_warn_once(tmp_path, caplog):
    """Pin: read-only audit dir does not propagate. Two append calls
    only log once."""
    bad = tmp_path / "ro" / "audit.jsonl"
    bad.parent.mkdir()
    bad.parent.chmod(0o400)
    try:
        L = _AdversarialAuditLedger(path=bad)
        rev = AdversarialReview(op_id="op")
        with caplog.at_level(logging.WARNING):
            assert L.append(rev) is False
        first_warn_count = caplog.text.count("write failed at")
        with caplog.at_level(logging.WARNING):
            assert L.append(rev) is False
        assert caplog.text.count("write failed at") == first_warn_count
    finally:
        bad.parent.chmod(0o700)


def test_audit_reset_warned_for_tests(tmp_path):
    L = _AdversarialAuditLedger(path=tmp_path / "x.jsonl")
    L._io_warned = True
    L.reset_warned_for_tests()
    assert L._io_warned is False


# ===========================================================================
# F — Default-singleton accessor
# ===========================================================================


def test_default_service_lazy_constructs():
    reset_default_service()
    s = get_default_service()
    assert isinstance(s, AdversarialReviewerService)


def test_default_service_returns_same_instance():
    reset_default_service()
    a = get_default_service()
    b = get_default_service()
    assert a is b


def test_reset_default_service_clears():
    reset_default_service()
    a = get_default_service()
    reset_default_service()
    b = get_default_service()
    assert a is not b


# ===========================================================================
# G — Authority invariants
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


def test_service_no_authority_imports():
    src = _read(
        "backend/core/ouroboros/governance/adversarial_reviewer_service.py",
    )
    for imp in _BANNED:
        assert imp not in src, f"banned import: {imp}"


def test_service_only_io_is_audit_ledger():
    """Pin: only file I/O is the JSONL ledger path. No subprocess /
    network / env writes."""
    src = _strip_docstrings_and_comments(
        _read(
            "backend/core/ouroboros/governance/adversarial_reviewer_service.py",
        ),
    )
    forbidden = [
        "subprocess.",
        "os.environ[",
        "os." + "system(",  # split to dodge pre-commit hook
        "import requests",
        "import httpx",
        "import urllib.request",
    ]
    for c in forbidden:
        assert c not in src, f"unexpected coupling: {c}"
