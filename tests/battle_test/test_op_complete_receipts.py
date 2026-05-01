"""UI Slice 6 — inline op-complete receipts.

Pins the contract that every terminal op (success or failure)
emits a single grep-friendly receipt line:

    [✓] op-a7f3 · cost $0.0042 · posture EXPLORE · 22.3s
    [✗] op-b8d2 · cost $0.0010 · posture HARDEN · 15.7s · failed at GENERATE: <reason>

Constraints:
  * Single line per op (not multiple).
  * ` · ` separators (no Rich Panel border glyphs / no box drawing).
  * Posture pulled best-effort from the existing observer surface;
    omitted (not crashed) when unavailable.
  * Emitted AFTER the existing ``_close_op_block`` so the receipt
    appears below the op block — operators can still see the
    expanded block AND the one-line summary.

Authority Invariant
-------------------
Tests import only from the modules under test + stdlib + Rich.
"""
from __future__ import annotations

import io
import pathlib

import pytest


# -----------------------------------------------------------------------
# § A — Bytes pins on the seam
# -----------------------------------------------------------------------


def _serpent_flow_src() -> str:
    return pathlib.Path(
        "backend/core/ouroboros/battle_test/serpent_flow.py"
    ).read_text()


def test_emit_op_receipt_method_exists():
    src = _serpent_flow_src()
    assert "def _emit_op_receipt(" in src


def test_op_completed_calls_receipt():
    """``op_completed`` must call ``_emit_op_receipt`` with kind="success"."""
    src = _serpent_flow_src()
    fn_idx = src.find("    def op_completed(")
    assert fn_idx > 0
    end_idx = src.find("    def op_failed(", fn_idx)
    assert end_idx > fn_idx
    body = src[fn_idx:end_idx]
    assert "self._emit_op_receipt(" in body
    assert 'kind="success"' in body


def test_op_failed_calls_receipt():
    """``op_failed`` must call ``_emit_op_receipt`` with kind="failure"."""
    src = _serpent_flow_src()
    fn_idx = src.find("    def op_failed(")
    assert fn_idx > 0
    end_idx = src.find("    def op_noop(", fn_idx)
    assert end_idx > fn_idx
    body = src[fn_idx:end_idx]
    assert "self._emit_op_receipt(" in body
    assert 'kind="failure"' in body
    # Must forward reason + phase
    assert "failure_reason=" in body
    assert "failure_phase=" in body


def test_receipt_emitted_after_close_op_block():
    """The receipt line is emitted AFTER ``_close_op_block`` so the
    box closes, then the receipt scrolls below it."""
    src = _serpent_flow_src()
    fn_idx = src.find("    def op_completed(")
    end_idx = src.find("    def op_failed(", fn_idx)
    body = src[fn_idx:end_idx]
    close_idx = body.find("self._close_op_block(op_id)")
    receipt_idx = body.find("self._emit_op_receipt(")
    assert 0 < close_idx < receipt_idx, (
        "receipt must come AFTER close_op_block in op_completed"
    )


def test_read_current_posture_token_is_best_effort():
    """The posture-read helper is wrapped in a try/except so receipt
    emission can't fail when the observer isn't running."""
    src = _serpent_flow_src()
    fn_idx = src.find("    def _read_current_posture_token(")
    assert fn_idx > 0
    end_idx = src.find("\n    def ", fn_idx + 1)
    body = src[fn_idx:end_idx]
    assert "except Exception" in body
    assert 'return ""' in body


# -----------------------------------------------------------------------
# § B — Behavioral: capture rendered output
# -----------------------------------------------------------------------


def _make_flow():
    """Construct a SerpentFlow with a captured Console buffer."""
    from rich.console import Console
    from backend.core.ouroboros.battle_test.serpent_flow import SerpentFlow
    flow = SerpentFlow(
        session_id="bt-slice6-test",
        cost_cap_usd=2.50,
        idle_timeout_s=3600.0,
    )
    buf = io.StringIO()
    flow.console = Console(
        file=buf, force_terminal=False, width=120, color_system=None,
    )
    # Patch the posture reader to a deterministic value so behavioral
    # assertions don't depend on the live observer state.
    flow._read_current_posture_token = lambda: "EXPLORE"
    return flow, buf


def test_success_receipt_format():
    flow, buf = _make_flow()
    flow._emit_op_receipt(
        op_id="op-019d-aaaaa-test",
        kind="success",
        cost_usd=0.0042,
        elapsed_s=22.3,
    )
    out = buf.getvalue()
    # Glyph
    assert "[✓]" in out
    # Op id (short form — uses _short_id which trims to first segment)
    assert "op-" in out
    # Cost
    assert "cost $0.0042" in out
    # Posture
    assert "posture EXPLORE" in out
    # Elapsed
    assert "22.3s" in out
    # Single line — no panel glyphs
    for glyph in ("╭", "╰", "╮", "╯", "┌", "└", "┐", "┘"):
        assert glyph not in out


def test_failure_receipt_format():
    flow, buf = _make_flow()
    flow._emit_op_receipt(
        op_id="op-019d-bbbbb-test",
        kind="failure",
        cost_usd=0.0010,
        elapsed_s=15.7,
        failure_reason="all_providers_exhausted",
        failure_phase="GENERATE",
    )
    out = buf.getvalue()
    # Glyph
    assert "[✗]" in out
    # Cost + posture + time
    assert "cost $0.0010" in out
    assert "posture EXPLORE" in out
    assert "15.7s" in out
    # Failure reason + phase surfaced
    assert "failed" in out
    assert "GENERATE" in out
    assert "all_providers_exhausted" in out
    for glyph in ("╭", "╰", "╮", "╯", "┌", "└", "┐", "┘"):
        assert glyph not in out


def test_receipt_omits_posture_when_unavailable():
    """When the posture observer hasn't run yet, the receipt skips
    the posture segment entirely (clean omission, not 'posture None')."""
    flow, buf = _make_flow()
    flow._read_current_posture_token = lambda: ""  # observer unavailable
    flow._emit_op_receipt(
        op_id="op-019d-ccccc-test",
        kind="success",
        cost_usd=0.0001,
        elapsed_s=5.0,
    )
    out = buf.getvalue()
    assert "[✓]" in out
    assert "cost $0.0001" in out
    assert "5.0s" in out
    assert "posture" not in out  # cleanly omitted
    assert "None" not in out  # not the literal string None


def test_receipt_is_single_line():
    """The receipt is one console.print call → one line of output."""
    flow, buf = _make_flow()
    flow._emit_op_receipt(
        op_id="op-019d-ddddd-test",
        kind="success",
        cost_usd=0.0042,
        elapsed_s=10.0,
    )
    out = buf.getvalue()
    # Strip the trailing newline from console.print, count remaining
    # newlines — should be exactly 0 (one line, terminated).
    inner = out.rstrip("\n")
    assert "\n" not in inner, (
        f"receipt must be single-line; got:\n{out!r}"
    )


def test_read_posture_token_returns_empty_on_observer_failure(monkeypatch):
    """The helper must NEVER raise — even when the posture observer
    chain throws."""
    from backend.core.ouroboros.battle_test.serpent_flow import SerpentFlow
    flow = SerpentFlow(session_id="bt-slice6-helper")

    import backend.core.ouroboros.governance.posture_observer as po

    def _raising(*a, **kw):
        raise RuntimeError("simulated observer failure")

    monkeypatch.setattr(po, "get_default_store", _raising)
    # Method must return "" — never raise
    assert flow._read_current_posture_token() == ""


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
