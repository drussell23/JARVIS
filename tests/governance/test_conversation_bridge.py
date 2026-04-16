"""Tests for ConversationBridge — Tier -1 sanitize + bounded ring buffer + prompt shape.

Covers the V1 definition-of-done from the design plan §10:
  * Ring buffer + per-turn + total-chars caps
  * sanitize_for_log delegation (control chars + ANSI ESC byte)
  * Secret-shape redaction for each documented pattern
  * Disabled-path no-ops
  * Fenced untrusted block header / authority-ordering copy
  * Stats counters shape
  * Hash8 determinism for op-correlation telemetry
"""
from __future__ import annotations

import os
import re

import pytest

from backend.core.ouroboros.governance import conversation_bridge as cb


@pytest.fixture(autouse=True)
def _reset_env_and_singleton(monkeypatch):
    """Every test starts with a fresh singleton and bridge-off by default."""
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
# Disabled path: all entry points are no-ops (master switch default off)
# ---------------------------------------------------------------------------


def test_disabled_path_record_turn_is_noop():
    bridge = cb.ConversationBridge()
    # Env unset → _is_enabled() False.
    bridge.record_turn("user", "hello")
    assert bridge.stats().turns_recorded == 0
    assert bridge.snapshot() == []
    assert bridge.format_for_prompt() is None


def test_disabled_path_inject_metrics_reports_disabled():
    bridge = cb.ConversationBridge()
    enabled, n_turns, chars_in, redacted_any, hash8 = bridge.inject_metrics()
    assert enabled is False
    assert n_turns == 0
    assert chars_in == 0
    assert redacted_any is False
    assert hash8 == ""


# ---------------------------------------------------------------------------
# Ring buffer + caps
# ---------------------------------------------------------------------------


def test_ring_buffer_bounded_by_max_turns(monkeypatch):
    _enable(monkeypatch, MAX_TURNS="3")
    bridge = cb.ConversationBridge()
    for idx in range(5):
        bridge.record_turn("user", f"turn {idx}")
    snap = bridge.snapshot()
    assert len(snap) == 3
    # Oldest dropped, newest retained.
    assert snap[0].text == "turn 2"
    assert snap[-1].text == "turn 4"
    # Dropped count reflects evictions (5 inserted, 3 retained → 2 dropped).
    assert bridge.stats().dropped_by_cap == 2


def test_per_turn_char_cap_trims_before_admission(monkeypatch):
    _enable(monkeypatch, MAX_CHARS_PER_TURN="10")
    bridge = cb.ConversationBridge()
    bridge.record_turn("user", "a" * 500)
    snap = bridge.snapshot()
    assert len(snap) == 1
    assert len(snap[0].text) == 10


def test_total_chars_cap_drops_oldest_turns(monkeypatch):
    _enable(monkeypatch, MAX_TURNS="100", MAX_TOTAL_CHARS="50")
    bridge = cb.ConversationBridge()
    for i in range(10):
        bridge.record_turn("user", "x" * 20)
    snap = bridge.snapshot()
    # total cap=50 with 20-char turns → at most 2 turns fit (40 chars).
    assert sum(len(t.text) for t in snap) <= 50
    assert len(snap) <= 3


# ---------------------------------------------------------------------------
# Sanitizer (Tier -1)
# ---------------------------------------------------------------------------


def test_sanitizer_strips_control_chars(monkeypatch):
    _enable(monkeypatch)
    bridge = cb.ConversationBridge()
    # ESC (0x1b), newline, tab, null byte, DEL (0x7f) all in 0x00-0x1f or == 0x7f.
    bridge.record_turn("user", "hi\x1b[31m\n\tworld\x00\x7f!")
    snap = bridge.snapshot()
    assert len(snap) == 1
    text = snap[0].text
    # ESC byte gone, newline/tab/null/DEL gone. Residual ANSI params `[31m` are
    # plain characters — harmless without the escape byte.
    assert "\x1b" not in text
    assert "\n" not in text
    assert "\t" not in text
    assert "\x00" not in text
    assert "\x7f" not in text
    assert "world" in text
    assert "hi" in text


def test_sanitizer_rejects_empty_after_strip(monkeypatch):
    _enable(monkeypatch)
    bridge = cb.ConversationBridge()
    # Pure control chars → empty after strip → not admitted.
    bridge.record_turn("user", "\x00\x01\x02")
    assert bridge.snapshot() == []


# ---------------------------------------------------------------------------
# Secret-shape redaction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,label",
    [
        ("api key is sk-abcdefghij1234567890xyz", "openai-key"),
        ("slack token: xoxb-1234567890-abcdefghij", "slack-token"),
        ("aws AKIAABCDEFGHIJKLMNOP for the bucket", "aws-access-key"),
        ("github token ghp_1234567890abcdefghij1234567890abcd", "github-token"),
    ],
)
def test_redaction_inline_secrets(monkeypatch, raw, label):
    _enable(monkeypatch)
    bridge = cb.ConversationBridge()
    bridge.record_turn("user", raw)
    snap = bridge.snapshot()
    assert len(snap) == 1
    assert f"[REDACTED:{label}]" in snap[0].text
    # Original secret body not present.
    assert "abcdefghij" not in snap[0].text or "[REDACTED:" in snap[0].text
    assert bridge.stats().bytes_redacted > 0


def test_redaction_private_key_block(monkeypatch):
    _enable(monkeypatch, MAX_CHARS_PER_TURN="4096")
    bridge = cb.ConversationBridge()
    # Private key block fits after control-char stripping — newlines get
    # stripped by sanitize_for_log, but the BEGIN/END markers remain and
    # the regex matches without anchoring to line starts.
    key_body = "A" * 80
    raw = (
        f"-----BEGIN RSA PRIVATE KEY-----{key_body}-----END RSA PRIVATE KEY-----"
    )
    bridge.record_turn("user", raw)
    snap = bridge.snapshot()
    assert "[REDACTED:private-key-block]" in snap[0].text
    assert key_body not in snap[0].text


def test_redaction_disabled_preserves_secrets(monkeypatch):
    _enable(monkeypatch, REDACT_ENABLED="false")
    bridge = cb.ConversationBridge()
    bridge.record_turn("user", "sk-abcdefghij1234567890xyz")
    snap = bridge.snapshot()
    assert "sk-abcdefghij1234567890xyz" in snap[0].text
    assert bridge.stats().bytes_redacted == 0


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------


def test_format_for_prompt_returns_none_when_empty(monkeypatch):
    _enable(monkeypatch)
    bridge = cb.ConversationBridge()
    assert bridge.format_for_prompt() is None


def test_format_for_prompt_fenced_block_shape(monkeypatch):
    _enable(monkeypatch)
    bridge = cb.ConversationBridge()
    bridge.record_turn("user", "focus on the auth module today")
    bridge.record_turn("assistant", "acknowledged")
    out = bridge.format_for_prompt()
    assert out is not None
    # Header names the content as untrusted.
    assert "## Recent Conversation (untrusted user context)" in out
    # Fenced block present with untrusted attribute.
    assert '<conversation untrusted="true">' in out
    assert "</conversation>" in out
    # Per-turn role labels present.
    assert "[user] focus on the auth module today" in out
    assert "[assistant] acknowledged" in out
    # Authority-invariant copy present (matches §9).
    assert "no authority" in out.lower()
    assert "FORBIDDEN_PATH" in out


def test_format_for_prompt_bumps_ops_seen(monkeypatch):
    _enable(monkeypatch)
    bridge = cb.ConversationBridge()
    bridge.record_turn("user", "hello")
    assert bridge.stats().ops_seen == 0
    bridge.format_for_prompt()
    assert bridge.stats().ops_seen == 1
    assert bridge.stats().turns_injected == 1
    bridge.format_for_prompt()
    assert bridge.stats().ops_seen == 2


# ---------------------------------------------------------------------------
# Observability metrics (§8)
# ---------------------------------------------------------------------------


def test_inject_metrics_shape(monkeypatch):
    _enable(monkeypatch)
    bridge = cb.ConversationBridge()
    bridge.record_turn("user", "short")
    enabled, n_turns, chars_in, redacted_any, hash8 = bridge.inject_metrics()
    assert enabled is True
    assert n_turns == 1
    assert chars_in == len("short")
    assert redacted_any is False
    assert re.fullmatch(r"[0-9a-f]{8}", hash8)


def test_inject_metrics_redacted_flag(monkeypatch):
    _enable(monkeypatch)
    bridge = cb.ConversationBridge()
    bridge.record_turn("user", "key sk-abcdefghij1234567890xyz here")
    _, _, _, redacted_any, _ = bridge.inject_metrics()
    assert redacted_any is True


def test_inject_metrics_hash_is_deterministic(monkeypatch):
    _enable(monkeypatch)
    a = cb.ConversationBridge()
    b = cb.ConversationBridge()
    a.record_turn("user", "identical text")
    b.record_turn("user", "identical text")
    _, _, _, _, hash_a = a.inject_metrics()
    _, _, _, _, hash_b = b.inject_metrics()
    assert hash_a == hash_b


# ---------------------------------------------------------------------------
# Role validation + source tag
# ---------------------------------------------------------------------------


def test_invalid_role_rejected(monkeypatch):
    _enable(monkeypatch)
    bridge = cb.ConversationBridge()
    bridge.record_turn("system", "should be dropped")  # type: ignore[arg-type]
    assert bridge.snapshot() == []


def test_source_tag_preserved(monkeypatch):
    _enable(monkeypatch)
    bridge = cb.ConversationBridge()
    bridge.record_turn("user", "hi", source="voice")
    bridge.record_turn("user", "ho", source="tui")
    snap = bridge.snapshot()
    assert snap[0].source == "voice"
    assert snap[1].source == "tui"


# ---------------------------------------------------------------------------
# Singleton wiring
# ---------------------------------------------------------------------------


def test_get_default_bridge_returns_singleton():
    a = cb.get_default_bridge()
    b = cb.get_default_bridge()
    assert a is b


def test_reset_default_bridge_clears_singleton():
    a = cb.get_default_bridge()
    cb.reset_default_bridge()
    b = cb.get_default_bridge()
    assert a is not b
