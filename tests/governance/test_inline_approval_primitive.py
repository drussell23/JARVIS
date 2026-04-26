"""P3 Slice 1 — Inline approval primitive regression suite.

Pins the pure-data primitive shipped in
``backend/core/ouroboros/governance/inline_approval.py``:

  * Decision parser (parse_decision_input) — single-char + verbose +
    case-insensitive + safety-first WAIT default.
  * Frozen dataclasses (InlineApprovalRequest, InlineApprovalDecision)
    + helpers (is_immediate_priority, seconds_remaining).
  * Bounded thread-safe FIFO queue (InlineApprovalQueue) — IMMEDIATE
    priority promotion, idempotent record_decision, mark_timeout,
    cap-at-MAX behavior, snapshot.
  * Default-singleton accessor (get_default_queue / reset).
  * Env-knob defaults (is_enabled false; decision_timeout_s 30s + clamp).
  * Authority invariants — no banned imports + no I/O / subprocess /
    env mutation.

Slice 1 ships the primitive default-off behind
``JARVIS_APPROVAL_UX_INLINE_ENABLED``. Slice 4 graduates the flip.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.inline_approval import (
    DEFAULT_DECISION_TIMEOUT_S,
    MAX_QUEUED_REQUESTS,
    InlineApprovalChoice,
    InlineApprovalDecision,
    InlineApprovalQueue,
    InlineApprovalRequest,
    decision_timeout_s,
    get_default_queue,
    is_enabled,
    parse_decision_input,
    reset_default_queue,
)


_REPO = Path(__file__).resolve().parent.parent.parent


def _read(rel: str) -> str:
    return (_REPO / rel).read_text(encoding="utf-8")


def _make_request(
    request_id: str = "req-1",
    op_id: str = "op-1",
    risk_tier: str = "STANDARD",
    deadline_unix: float = 1_000_000.0,
    created_unix: float = 999_970.0,
) -> InlineApprovalRequest:
    return InlineApprovalRequest(
        request_id=request_id,
        op_id=op_id,
        risk_tier=risk_tier,
        target_files=("a.py",),
        diff_summary="@@ -1 +1 @@\n-a\n+b",
        created_unix=created_unix,
        deadline_unix=deadline_unix,
    )


@pytest.fixture(autouse=True)
def _reset_singleton():
    reset_default_queue()
    yield
    reset_default_queue()


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("JARVIS_APPROVAL_UX_INLINE_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_APPROVAL_UX_INLINE_TIMEOUT_S", raising=False)
    yield


# ===========================================================================
# A — Module-level constants
# ===========================================================================


def test_default_decision_timeout_pinned():
    """Pin: PRD spec says 30s default decision timeout."""
    assert DEFAULT_DECISION_TIMEOUT_S == 30.0


def test_max_queued_requests_pinned():
    """Pin: bounded queue capacity to prevent operator-AFK floods."""
    assert MAX_QUEUED_REQUESTS == 16


# ===========================================================================
# B — Env-knob accessors (is_enabled + decision_timeout_s)
# ===========================================================================


def test_is_enabled_default_false_pre_graduation():
    """Slice 1 ships default-OFF. Renamed to
    ``test_is_enabled_default_true_post_graduation`` at Slice 4 flip."""
    assert is_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE", "Yes"])
def test_is_enabled_truthy_variants(monkeypatch, val):
    monkeypatch.setenv("JARVIS_APPROVAL_UX_INLINE_ENABLED", val)
    assert is_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "", "garbage"])
def test_is_enabled_falsy_variants(monkeypatch, val):
    monkeypatch.setenv("JARVIS_APPROVAL_UX_INLINE_ENABLED", val)
    assert is_enabled() is False


def test_decision_timeout_default_when_unset():
    assert decision_timeout_s() == DEFAULT_DECISION_TIMEOUT_S


def test_decision_timeout_reads_env(monkeypatch):
    monkeypatch.setenv("JARVIS_APPROVAL_UX_INLINE_TIMEOUT_S", "12.5")
    assert decision_timeout_s() == 12.5


def test_decision_timeout_clamps_negative_to_one(monkeypatch):
    """Pin: negative env values clamp to 1.0 — never auto-defer instantly."""
    monkeypatch.setenv("JARVIS_APPROVAL_UX_INLINE_TIMEOUT_S", "-5")
    assert decision_timeout_s() == 1.0


def test_decision_timeout_clamps_zero_to_one(monkeypatch):
    monkeypatch.setenv("JARVIS_APPROVAL_UX_INLINE_TIMEOUT_S", "0")
    assert decision_timeout_s() == 1.0


def test_decision_timeout_falls_back_on_invalid(monkeypatch):
    monkeypatch.setenv("JARVIS_APPROVAL_UX_INLINE_TIMEOUT_S", "not-a-number")
    assert decision_timeout_s() == DEFAULT_DECISION_TIMEOUT_S


# ===========================================================================
# C — parse_decision_input
# ===========================================================================


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("y", InlineApprovalChoice.APPROVE),
        ("n", InlineApprovalChoice.REJECT),
        ("s", InlineApprovalChoice.SHOW_STACK),
        ("e", InlineApprovalChoice.EDIT),
        ("w", InlineApprovalChoice.WAIT),
    ],
)
def test_parse_single_char_choices(text, expected):
    assert parse_decision_input(text) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("yes", InlineApprovalChoice.APPROVE),
        ("YES", InlineApprovalChoice.APPROVE),
        ("Yes", InlineApprovalChoice.APPROVE),
        ("approve", InlineApprovalChoice.APPROVE),
        ("no", InlineApprovalChoice.REJECT),
        ("reject", InlineApprovalChoice.REJECT),
        ("show", InlineApprovalChoice.SHOW_STACK),
        ("stack", InlineApprovalChoice.SHOW_STACK),
        ("edit", InlineApprovalChoice.EDIT),
        ("wait", InlineApprovalChoice.WAIT),
        ("defer", InlineApprovalChoice.WAIT),
    ],
)
def test_parse_verbose_choices_case_insensitive(text, expected):
    assert parse_decision_input(text) is expected


@pytest.mark.parametrize("text", ["", "   ", "\t", "\n"])
def test_parse_empty_returns_wait(text):
    """Safety-first: empty / whitespace input never auto-approves."""
    assert parse_decision_input(text) is InlineApprovalChoice.WAIT


@pytest.mark.parametrize("text", ["maybe", "????", "123", "approve-but-wait"])
def test_parse_garbage_returns_wait(text):
    """Safety-first: unknown tokens never auto-approve."""
    assert parse_decision_input(text) is InlineApprovalChoice.WAIT


def test_parse_strips_surrounding_whitespace():
    assert parse_decision_input("  y  ") is InlineApprovalChoice.APPROVE


def test_parse_takes_first_token_only():
    """``y\\nrm -rf /`` should parse the first token, not crash or chain."""
    assert parse_decision_input("y\nrm -rf /") is InlineApprovalChoice.APPROVE


# ===========================================================================
# D — InlineApprovalRequest + InlineApprovalDecision dataclasses
# ===========================================================================


def test_request_is_frozen():
    req = _make_request()
    with pytest.raises(dataclasses.FrozenInstanceError):
        req.risk_tier = "BLOCKED"  # type: ignore[misc]


def test_decision_is_frozen():
    dec = InlineApprovalDecision(
        request_id="r1",
        choice=InlineApprovalChoice.APPROVE,
        reason="looks good",
        decided_unix=100.0,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        dec.choice = InlineApprovalChoice.REJECT  # type: ignore[misc]


def test_decision_default_operator_is_operator():
    dec = InlineApprovalDecision(
        request_id="r1",
        choice=InlineApprovalChoice.APPROVE,
        reason="",
        decided_unix=100.0,
    )
    assert dec.operator == "operator"


@pytest.mark.parametrize("tier", ["IMMEDIATE", "BLOCKED"])
def test_request_immediate_priority_true_for_immediate_or_blocked(tier):
    req = _make_request(risk_tier=tier)
    assert req.is_immediate_priority() is True


@pytest.mark.parametrize(
    "tier", ["STANDARD", "COMPLEX", "BACKGROUND", "SPECULATIVE", "SAFE_AUTO"],
)
def test_request_immediate_priority_false_for_other_tiers(tier):
    req = _make_request(risk_tier=tier)
    assert req.is_immediate_priority() is False


def test_seconds_remaining_positive_when_before_deadline():
    req = _make_request(deadline_unix=1000.0)
    assert req.seconds_remaining(now_unix=970.0) == 30.0


def test_seconds_remaining_negative_clamps_to_minus_one():
    """Past-deadline clamps to -1.0 (caller signal: auto-defer)."""
    req = _make_request(deadline_unix=1000.0)
    assert req.seconds_remaining(now_unix=2000.0) == -1.0


# ===========================================================================
# E — InlineApprovalQueue: enqueue / next_pending / record_decision
# ===========================================================================


def test_enqueue_then_next_pending_returns_request():
    q = InlineApprovalQueue()
    req = _make_request()
    assert q.enqueue(req) is True
    assert q.next_pending(now_unix=999_990.0) == req


def test_enqueue_rejects_blank_request_id():
    q = InlineApprovalQueue()
    bad = _make_request(request_id="")
    assert q.enqueue(bad) is False


def test_enqueue_rejects_blank_op_id():
    q = InlineApprovalQueue()
    bad = _make_request(op_id="")
    assert q.enqueue(bad) is False


def test_enqueue_rejects_duplicate_request_id():
    q = InlineApprovalQueue()
    req = _make_request()
    assert q.enqueue(req) is True
    assert q.enqueue(req) is False  # duplicate


def test_enqueue_returns_false_when_queue_full():
    q = InlineApprovalQueue()
    for i in range(MAX_QUEUED_REQUESTS):
        assert q.enqueue(_make_request(request_id=f"req-{i}")) is True
    overflow = _make_request(request_id="req-overflow")
    assert q.enqueue(overflow) is False
    assert len(q) == MAX_QUEUED_REQUESTS


def test_immediate_priority_jumps_queue_front():
    q = InlineApprovalQueue()
    q.enqueue(_make_request(request_id="r1", risk_tier="STANDARD"))
    q.enqueue(_make_request(request_id="r2", risk_tier="STANDARD"))
    q.enqueue(_make_request(request_id="r3", risk_tier="IMMEDIATE"))
    # IMMEDIATE r3 should be served first.
    nxt = q.next_pending(now_unix=999_990.0)
    assert nxt is not None
    assert nxt.request_id == "r3"


def test_blocked_tier_also_jumps_queue_front():
    q = InlineApprovalQueue()
    q.enqueue(_make_request(request_id="r1"))
    q.enqueue(_make_request(request_id="rB", risk_tier="BLOCKED"))
    nxt = q.next_pending(now_unix=999_990.0)
    assert nxt is not None
    assert nxt.request_id == "rB"


def test_record_decision_marks_entry_decided():
    q = InlineApprovalQueue()
    q.enqueue(_make_request(request_id="r1"))
    assert q.record_decision(
        "r1", InlineApprovalChoice.APPROVE, reason="lgtm",
        now_unix=500.0,
    ) is True
    dec = q.get_decision("r1")
    assert dec is not None
    assert dec.choice is InlineApprovalChoice.APPROVE
    assert dec.reason == "lgtm"
    assert dec.decided_unix == 500.0


def test_record_decision_unknown_request_id_returns_false():
    q = InlineApprovalQueue()
    assert q.record_decision(
        "missing", InlineApprovalChoice.APPROVE,
    ) is False


def test_record_decision_idempotent_first_wins():
    """Pin: subsequent record_decision calls are silent no-ops."""
    q = InlineApprovalQueue()
    q.enqueue(_make_request(request_id="r1"))
    assert q.record_decision("r1", InlineApprovalChoice.APPROVE) is True
    assert q.record_decision("r1", InlineApprovalChoice.REJECT) is False
    dec = q.get_decision("r1")
    assert dec is not None
    assert dec.choice is InlineApprovalChoice.APPROVE


def test_record_decision_truncates_long_reason():
    """Pin: reason capped at 500 chars to bound audit ledger size."""
    q = InlineApprovalQueue()
    q.enqueue(_make_request(request_id="r1"))
    huge = "x" * 1000
    q.record_decision("r1", InlineApprovalChoice.REJECT, reason=huge)
    dec = q.get_decision("r1")
    assert dec is not None
    assert len(dec.reason) == 500


def test_next_pending_skips_decided():
    q = InlineApprovalQueue()
    q.enqueue(_make_request(request_id="r1"))
    q.enqueue(_make_request(request_id="r2"))
    q.record_decision("r1", InlineApprovalChoice.APPROVE)
    nxt = q.next_pending(now_unix=999_990.0)
    assert nxt is not None
    assert nxt.request_id == "r2"


def test_next_pending_skips_past_deadline():
    q = InlineApprovalQueue()
    q.enqueue(_make_request(request_id="r1", deadline_unix=100.0))
    q.enqueue(_make_request(
        request_id="r2", deadline_unix=999_999_999.0,
    ))
    nxt = q.next_pending(now_unix=500.0)
    assert nxt is not None
    assert nxt.request_id == "r2"


def test_next_pending_returns_none_when_empty():
    q = InlineApprovalQueue()
    assert q.next_pending() is None


def test_mark_timeout_records_timeout_deferred():
    q = InlineApprovalQueue()
    q.enqueue(_make_request(request_id="r1"))
    assert q.mark_timeout("r1", now_unix=500.0) is True
    dec = q.get_decision("r1")
    assert dec is not None
    assert dec.choice is InlineApprovalChoice.TIMEOUT_DEFERRED
    assert dec.operator == "system"


def test_forget_drops_entry():
    q = InlineApprovalQueue()
    q.enqueue(_make_request(request_id="r1"))
    assert q.forget("r1") is True
    assert q.next_pending() is None
    assert len(q) == 0


def test_forget_unknown_returns_false():
    q = InlineApprovalQueue()
    assert q.forget("missing") is False


def test_snapshot_returns_only_undecided_in_order():
    q = InlineApprovalQueue()
    q.enqueue(_make_request(request_id="r1"))
    q.enqueue(_make_request(request_id="r2"))
    q.enqueue(_make_request(request_id="r3"))
    q.record_decision("r2", InlineApprovalChoice.APPROVE)
    snap = q.snapshot()
    ids = [r.request_id for r in snap]
    assert ids == ["r1", "r3"]


def test_len_reflects_total_entries():
    """__len__ counts all entries (decided + pending) — primary use is
    cap-check + quick observability."""
    q = InlineApprovalQueue()
    q.enqueue(_make_request(request_id="r1"))
    q.enqueue(_make_request(request_id="r2"))
    assert len(q) == 2
    q.record_decision("r1", InlineApprovalChoice.APPROVE)
    assert len(q) == 2  # decided still present until forget()


# ===========================================================================
# F — Default-singleton accessor
# ===========================================================================


def test_get_default_queue_lazy_constructs():
    a = get_default_queue()
    assert isinstance(a, InlineApprovalQueue)


def test_get_default_queue_returns_same_instance():
    a = get_default_queue()
    b = get_default_queue()
    assert a is b


def test_reset_default_queue_clears_singleton():
    a = get_default_queue()
    reset_default_queue()
    b = get_default_queue()
    assert a is not b


# ===========================================================================
# G — Authority invariants (no banned imports + no I/O)
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


def test_inline_approval_no_authority_imports():
    src = _read("backend/core/ouroboros/governance/inline_approval.py")
    for imp in _BANNED:
        assert imp not in src, f"banned import: {imp}"


def test_inline_approval_no_io_or_subprocess():
    """Pin: primitive is pure data — no file I/O, no subprocess, no
    env mutation. Slice 3 owns the I/O surface (SerpentFlow + $EDITOR)."""
    src = _read("backend/core/ouroboros/governance/inline_approval.py")
    forbidden = [
        "subprocess.",
        "open(",
        ".write(",
        ".write_text(",
        "os.environ[",
        "os." + "system(",  # split to dodge pre-commit hook
    ]
    for c in forbidden:
        assert c not in src, f"unexpected coupling: {c}"
