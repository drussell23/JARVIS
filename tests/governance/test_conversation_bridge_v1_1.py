"""v1.1 tests — assistant signals, sub-env gates, cross-op semantics, error isolation.

Definition-of-done from v1.1 plan §11. Each test maps to a specific
contract bullet the plan promises:

  (1) ask_human Q+A pair shape + op_id correlation
  (2) POSTMORTEM ingested with deterministic one-liner
  (3) Cross-op visibility (op1 postmortem → op2 snapshot)
  (4) Postmortem K-cap
  (5) Postmortem TTL
  (6) Sub-gate CAPTURE_ASK_HUMAN=false
  (7) Sub-gate CAPTURE_POSTMORTEM=false
  (8) by_source counters
  (9) Subheaders render only when populated
 (10) record_turn error isolation → dropped_errors
 (11) format_postmortem_payload skips empty root_cause
"""
from __future__ import annotations

import os
import time

import pytest

from backend.core.ouroboros.governance import conversation_bridge as cb


@pytest.fixture(autouse=True)
def _reset_env_and_singleton(monkeypatch):
    for key in list(os.environ.keys()):
        if key.startswith("JARVIS_CONVERSATION_BRIDGE_"):
            monkeypatch.delenv(key, raising=False)
    cb.reset_default_bridge()
    yield
    cb.reset_default_bridge()


def _enable(monkeypatch, **overrides):
    monkeypatch.setenv("JARVIS_CONVERSATION_BRIDGE_ENABLED", "true")
    for k, v in overrides.items():
        monkeypatch.setenv(f"JARVIS_CONVERSATION_BRIDGE_{k}", str(v))


# ---------------------------------------------------------------------------
# (1) ask_human Q+A pair with op_id correlation
# ---------------------------------------------------------------------------


def test_ask_human_pair_captured_with_op_id(monkeypatch):
    _enable(monkeypatch)
    bridge = cb.ConversationBridge()
    bridge.record_turn(
        "assistant", "which auth provider should I use?",
        source="ask_human_q", op_id="op-abc",
    )
    bridge.record_turn(
        "user", "OAuth2 only",
        source="ask_human_a", op_id="op-abc",
    )
    snap = bridge.snapshot()
    assert len(snap) == 2
    q, a = snap
    assert q.role == "assistant" and q.source == "ask_human_q"
    assert a.role == "user" and a.source == "ask_human_a"
    assert q.op_id == "op-abc" == a.op_id
    assert "which auth provider" in q.text
    assert "OAuth2" in a.text


# ---------------------------------------------------------------------------
# (2) POSTMORTEM ingested via deterministic payload helper
# ---------------------------------------------------------------------------


def test_postmortem_ingested_one_liner(monkeypatch):
    _enable(monkeypatch)
    bridge = cb.ConversationBridge()
    payload = cb.format_postmortem_payload(
        op_id="op-xyz",
        terminal_reason_code="VERIFY",
        root_cause="scoped test failed: missing fixture",
    )
    assert payload is not None
    assert payload.startswith("postmortem op=op-xyz ")
    assert "outcome=VERIFY" in payload
    assert "root_cause=scoped test failed" in payload

    bridge.record_turn("assistant", payload, source="postmortem", op_id="op-xyz")
    snap = bridge.snapshot()
    assert len(snap) == 1
    t = snap[0]
    assert t.role == "assistant"
    assert t.source == "postmortem"
    assert t.op_id == "op-xyz"


def test_postmortem_payload_caps_at_256_chars():
    """v1.1 §13.1: hard per-line cap prevents pathological root_cause bloat."""
    rc = "x" * 500
    payload = cb.format_postmortem_payload(
        op_id="op-1", terminal_reason_code="PHASE", root_cause=rc,
    )
    assert payload is not None
    assert len(payload) <= 256
    assert payload.endswith("...")


# ---------------------------------------------------------------------------
# (3) Cross-op: op1 postmortem visible in op2's snapshot/prompt
# ---------------------------------------------------------------------------


def test_cross_op_postmortem_appears_in_next_op_prompt(monkeypatch):
    _enable(monkeypatch)
    bridge = cb.ConversationBridge()

    # op1 ends, POSTMORTEM captured.
    pm1 = cb.format_postmortem_payload(
        op_id="op-1", terminal_reason_code="VERIFY",
        root_cause="regressed after refactor",
    )
    bridge.record_turn("assistant", pm1 or "", source="postmortem", op_id="op-1")

    # op2 begins — CONTEXT_EXPANSION calls format_for_prompt.
    prompt = bridge.format_for_prompt()
    assert prompt is not None
    assert "### Prior op closure (postmortem)" in prompt
    assert "[postmortem op=op-1]" in prompt
    assert "regressed after refactor" in prompt


# ---------------------------------------------------------------------------
# (4) Postmortem K-cap — oldest evicted under pressure
# ---------------------------------------------------------------------------


def test_postmortem_k_cap_retains_last_n(monkeypatch):
    _enable(monkeypatch, MAX_TURNS="20", MAX_POSTMORTEMS="3")
    bridge = cb.ConversationBridge()
    for i in range(5):
        payload = cb.format_postmortem_payload(
            op_id=f"op-{i}", terminal_reason_code="PHASE",
            root_cause=f"reason {i}",
        )
        bridge.record_turn(
            "assistant", payload or "", source="postmortem", op_id=f"op-{i}",
        )

    snap = bridge.snapshot()
    pm_ids = [t.op_id for t in snap if t.source == "postmortem"]
    assert pm_ids == ["op-2", "op-3", "op-4"]


# ---------------------------------------------------------------------------
# (5) Postmortem TTL — aged entries drop silently
# ---------------------------------------------------------------------------


def test_postmortem_ttl_drops_aged(monkeypatch):
    _enable(monkeypatch, POSTMORTEM_TTL_S="1", MAX_POSTMORTEMS="10")
    bridge = cb.ConversationBridge()

    # Inject a postmortem with a ts deep in the past — bypass record_turn's
    # "ts = now" stamping by directly appending to the buffer.
    old_turn = cb.ConversationTurn(
        role="assistant",
        text="postmortem op=old outcome=X root_cause=stale",
        ts=time.time() - 3600.0,  # 1 hour old
        source="postmortem",
        op_id="op-old",
    )
    with bridge._lock:  # type: ignore[attr-defined]
        bridge._buf.append(old_turn)  # type: ignore[attr-defined]

    # Fresh postmortem for the same bridge.
    bridge.record_turn(
        "assistant",
        "postmortem op=new outcome=Y root_cause=fresh",
        source="postmortem", op_id="op-new",
    )

    snap = bridge.snapshot()
    pm_ids = {t.op_id for t in snap if t.source == "postmortem"}
    assert "op-old" not in pm_ids  # TTL-aged out
    assert "op-new" in pm_ids


# ---------------------------------------------------------------------------
# (6) Sub-gate: CAPTURE_ASK_HUMAN=false
# ---------------------------------------------------------------------------


def test_subgate_ask_human_off_drops_qa(monkeypatch):
    _enable(monkeypatch, CAPTURE_ASK_HUMAN="false")
    bridge = cb.ConversationBridge()
    bridge.record_turn(
        "assistant", "q?", source="ask_human_q", op_id="op-1",
    )
    bridge.record_turn(
        "user", "a", source="ask_human_a", op_id="op-1",
    )
    # TUI user should still be accepted.
    bridge.record_turn("user", "focus on X")

    snap = bridge.snapshot()
    sources = {t.source for t in snap}
    assert "ask_human_q" not in sources
    assert "ask_human_a" not in sources
    assert "tui_user" in sources


# ---------------------------------------------------------------------------
# (7) Sub-gate: CAPTURE_POSTMORTEM=false
# ---------------------------------------------------------------------------


def test_subgate_postmortem_off_drops_pm(monkeypatch):
    _enable(monkeypatch, CAPTURE_POSTMORTEM="false")
    bridge = cb.ConversationBridge()
    bridge.record_turn(
        "assistant", "postmortem op=op-1 outcome=X root_cause=y",
        source="postmortem", op_id="op-1",
    )
    bridge.record_turn("user", "hello")  # tui_user still accepted

    snap = bridge.snapshot()
    sources = {t.source for t in snap}
    assert "postmortem" not in sources
    assert "tui_user" in sources


# ---------------------------------------------------------------------------
# (8) by_source counters populate correctly
# ---------------------------------------------------------------------------


def test_by_source_counters(monkeypatch):
    _enable(monkeypatch)
    bridge = cb.ConversationBridge()
    bridge.record_turn("user", "hello")  # tui_user
    bridge.record_turn(
        "assistant", "q", source="ask_human_q", op_id="op-1",
    )
    bridge.record_turn(
        "user", "a", source="ask_human_a", op_id="op-1",
    )
    bridge.record_turn(
        "assistant", "postmortem op=op-1 outcome=X root_cause=y",
        source="postmortem", op_id="op-1",
    )

    stats = bridge.stats()
    assert stats.by_source.get("tui_user") == 1
    assert stats.by_source.get("ask_human_q") == 1
    assert stats.by_source.get("ask_human_a") == 1
    assert stats.by_source.get("postmortem") == 1


# ---------------------------------------------------------------------------
# (9) Subheaders render only for populated categories
# ---------------------------------------------------------------------------


def test_subheaders_omitted_when_empty(monkeypatch):
    _enable(monkeypatch)
    bridge = cb.ConversationBridge()
    bridge.record_turn("user", "only user intent here")

    prompt = bridge.format_for_prompt()
    assert prompt is not None
    assert "### TUI user intent" in prompt
    assert "### Clarifications (recent)" not in prompt
    assert "### Prior op closure (postmortem)" not in prompt


def test_all_subheaders_present_when_all_populated(monkeypatch):
    _enable(monkeypatch)
    bridge = cb.ConversationBridge()
    bridge.record_turn("user", "user intent")
    bridge.record_turn("assistant", "q?", source="ask_human_q", op_id="op-1")
    bridge.record_turn("user", "a", source="ask_human_a", op_id="op-1")
    bridge.record_turn(
        "assistant", "postmortem op=op-0 outcome=VERIFY root_cause=x",
        source="postmortem", op_id="op-0",
    )

    prompt = bridge.format_for_prompt()
    assert prompt is not None
    assert "### TUI user intent" in prompt
    assert "### Clarifications (recent)" in prompt
    assert "### Prior op closure (postmortem)" in prompt


# ---------------------------------------------------------------------------
# (10) Error isolation — record_turn never raises
# ---------------------------------------------------------------------------


def test_record_turn_error_isolation(monkeypatch):
    """Force a crash inside record_turn body → dropped_errors bumps, no raise."""
    _enable(monkeypatch)
    bridge = cb.ConversationBridge()

    # Monkeypatch sanitize_for_log inside the bridge module to raise.
    def _boom(*_a, **_k):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(cb, "sanitize_for_log", _boom)

    # Should not raise.
    bridge.record_turn("user", "text that would trigger sanitize")
    assert bridge.stats().dropped_errors >= 1
    # Ring stays empty (turn never admitted).
    assert bridge.snapshot() == []


# ---------------------------------------------------------------------------
# (11) format_postmortem_payload skips empty/none root_cause
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rc", ["", "   ", "none", "None", "NONE"])
def test_format_postmortem_payload_skips_trivial(rc):
    assert cb.format_postmortem_payload(
        op_id="op-1", terminal_reason_code="PHASE", root_cause=rc,
    ) is None


# ---------------------------------------------------------------------------
# Legacy alias regression check
# ---------------------------------------------------------------------------


def test_legacy_tui_alias_remaps(monkeypatch):
    """V1 callers using source='tui' must still land their turns."""
    _enable(monkeypatch)
    bridge = cb.ConversationBridge()
    bridge.record_turn("user", "legacy caller", source="tui")
    snap = bridge.snapshot()
    assert len(snap) == 1
    assert snap[0].source == "tui_user"


def test_unknown_source_dropped(monkeypatch):
    """Fail-closed against future adapters pre-dating a schema update."""
    _enable(monkeypatch)
    bridge = cb.ConversationBridge()
    bridge.record_turn("user", "from mars", source="mars_adapter")  # unknown
    assert bridge.snapshot() == []
