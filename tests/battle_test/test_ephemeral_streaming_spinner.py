"""UI Slice 7 — ephemeral inline status spinner for streaming.

Pins the contract that ``show_streaming_start`` /
``show_streaming_token`` / ``show_streaming_end`` no longer use
``rich.Live(Syntax)`` for a persistent fixed-region render, but
instead drive an ephemeral ``rich.Status`` spinner that:

  * Appears with ``Streaming N tokens via <provider>`` while in flight
  * Updates the token count on each token
  * Vanishes when streaming ends (no fixed region left behind)
  * Emits a single inline ``[✓] Generated N tokens via <provider>``
    receipt line on resolution

The actual generated code surfaces later via the existing
``show_diff`` ⏺ Update path.

Authority Invariant
-------------------
Tests import only from the modules under test + stdlib + Rich.
"""
from __future__ import annotations

import io
import pathlib

import pytest


# -----------------------------------------------------------------------
# § A — Bytes pins
# -----------------------------------------------------------------------


def _serpent_flow_src() -> str:
    return pathlib.Path(
        "backend/core/ouroboros/battle_test/serpent_flow.py"
    ).read_text()


def _streaming_block() -> str:
    """Return source of show_streaming_start/token/end as one block."""
    src = _serpent_flow_src()
    fn_idx = src.find("    def show_streaming_start(")
    end_idx = src.find("    def _streaming_spinner_label(", fn_idx)
    if end_idx < 0:
        end_idx = src.find("\n    def op_started(", fn_idx)
    body_start = src[fn_idx:end_idx if end_idx > fn_idx else fn_idx + 8000]

    end2 = src.find("\n    # ══", src.find("def show_streaming_end", fn_idx))
    body_end = src[
        src.find("def show_streaming_end", fn_idx):
        end2 if end2 > 0 else end_idx + 5000
    ]
    return body_start + "\n\n" + body_end


def test_streaming_block_no_live_syntax_construction():
    """The streaming code path MUST NOT construct ``Live(Syntax(...))``
    anymore — that was the persistent fixed-region renderer Slice 7
    retires. The Live import remains in the file (used by other
    paths) but is not invoked from streaming."""
    block = _streaming_block()
    assert "Live(\n" not in block, (
        "show_streaming_* must not construct rich.Live — Slice 7 "
        "replaces the fixed region with an ephemeral Status spinner"
    )
    assert "Syntax(" not in block, (
        "show_streaming_* must not render via rich.Syntax — the diff "
        "path (show_diff / ⏺ Update) is the syntax-rendering surface"
    )


def test_streaming_block_uses_console_status():
    """The new ephemeral path uses ``self.console.status(...)`` (Rich
    ephemeral spinner primitive) and stores the handle on
    ``self._active_status``."""
    block = _streaming_block()
    assert "self.console.status(" in block
    assert "self._active_status" in block


def test_streaming_block_emits_receipt_on_end():
    """``show_streaming_end`` must emit a ``[✓] Generated N tokens``
    inline receipt line when token_count > 0."""
    block = _streaming_block()
    assert "[✓]" in block
    assert "Generated" in block
    assert "tokens" in block


def test_streaming_label_helper_exists():
    src = _serpent_flow_src()
    assert "def _streaming_spinner_label(self)" in src


# -----------------------------------------------------------------------
# § B — Behavioral
# -----------------------------------------------------------------------


def _make_flow():
    """Construct a SerpentFlow with output capture."""
    from rich.console import Console
    from backend.core.ouroboros.battle_test.serpent_flow import (
        SerpentFlow,
    )
    flow = SerpentFlow(
        session_id="bt-slice7-test",
        cost_cap_usd=2.50,
        idle_timeout_s=3600.0,
    )
    buf = io.StringIO()
    flow.console = Console(
        file=buf, force_terminal=False, width=120, color_system=None,
    )
    return flow, buf


def test_streaming_lifecycle_no_persistent_region():
    flow, buf = _make_flow()
    # Open
    flow.show_streaming_start(provider="claude", op_id="op-test")
    # Stream a few tokens
    for tok in ["Hello", " ", "world", "!"]:
        flow.show_streaming_token(tok)
    # Close
    flow.show_streaming_end()

    out = buf.getvalue()
    # Token aggregation still works
    assert flow._stream_buffer == ""  # reset on end
    # Receipt emitted
    assert "[✓]" in out
    assert "Generated" in out
    assert "4 tokens" in out
    assert "Claude" in out or "claude" in out
    # No box drawing / Panel glyphs (would indicate a fixed region)
    for glyph in ("╭", "╰", "╮", "╯", "┌", "└", "┐", "┘"):
        assert glyph not in out


def test_streaming_token_count_resets_between_calls():
    """A second start/end cycle gets a fresh token counter — state
    fully resets."""
    flow, buf = _make_flow()
    flow.show_streaming_start(provider="dw")
    flow.show_streaming_token("a")
    flow.show_streaming_token("b")
    flow.show_streaming_token("c")
    flow.show_streaming_end()
    flow.show_streaming_start(provider="dw")
    flow.show_streaming_token("x")
    flow.show_streaming_end()
    out = buf.getvalue()
    assert "3 tokens" in out
    assert "1 tokens" in out
    # No bleed
    assert "4 tokens" not in out


def test_streaming_end_no_receipt_when_zero_tokens():
    """If no tokens were streamed (e.g., immediate failure), the
    receipt line is omitted to keep the scrollback clean."""
    flow, buf = _make_flow()
    flow.show_streaming_start(provider="claude")
    flow.show_streaming_end()
    out = buf.getvalue()
    # synthesizing line printed, but no [✓] receipt for 0 tokens
    assert "[✓]" not in out
    assert "Generated" not in out


def test_active_status_handle_cleared_after_end():
    """``self._active_status`` returns to None after streaming ends —
    confirms the ephemeral spinner was properly stopped."""
    flow, buf = _make_flow()
    flow.show_streaming_start(provider="claude")
    flow.show_streaming_token("token")
    flow.show_streaming_end()
    assert flow._active_status is None


def test_streaming_token_with_no_active_stream_does_not_raise():
    """Defensive: a stray show_streaming_token call (e.g., race with
    end) must not raise."""
    flow, buf = _make_flow()
    # No start — token call should be a clean no-op
    flow.show_streaming_token("orphan")
    # No exception means pass
    assert flow._stream_token_count == 1  # accumulator still ticks


# -----------------------------------------------------------------------
# § C — Authority invariant
# -----------------------------------------------------------------------


def test_test_module_no_orchestrator_imports():
    src = pathlib.Path(__file__).read_text()
    forbidden = (
        "phase_runners", "iron_gate", "change_engine",
        "candidate_generator", "providers", "orchestrator",
    )
    for tok in forbidden:
        assert (
            f"from backend.core.ouroboros.governance.{tok}" not in src
        ), f"forbidden: {tok}"
