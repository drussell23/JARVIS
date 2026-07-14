"""Task #6 — MCP output scanner: shadow-mode graduation.

The scanner was master-default-FALSE (fully inert) — an open credential-exfil
blind spot on every tool output. It also had only two states (off / redact), so
flipping it on meant flipping straight to MUTATION, which risks false-positive
mangling of legitimate tool output mid-loop. This adds the third state (SHADOW)
and graduates OBSERVATION on by default; ENFORCEMENT (redaction) stays soak-gated.
"""
from __future__ import annotations

from backend.core.ouroboros.governance import mcp_output_scanner as m


_ENV_MASTER = "JARVIS_MCP_OUTPUT_SCANNER_ENABLED"
_ENV_SHADOW = "JARVIS_MCP_OUTPUT_SCANNER_SHADOW"


def _clear(monkeypatch):
    monkeypatch.delenv(_ENV_MASTER, raising=False)
    monkeypatch.delenv(_ENV_SHADOW, raising=False)


# ── three-state truth table ──────────────────────────────────────────

def test_default_is_observe_not_enforce(monkeypatch):
    """The graduated default: master ON (observe), shadow ON, enforce OFF —
    the scanner runs and logs, but does NOT mutate tool output."""
    _clear(monkeypatch)
    assert m.master_enabled() is True
    assert m.shadow_mode_enabled() is True
    assert m.enforce_enabled() is False


def test_un_shadow_enables_enforcement(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv(_ENV_SHADOW, "false")
    assert m.enforce_enabled() is True


def test_master_off_is_fully_inert(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv(_ENV_MASTER, "false")
    assert m.master_enabled() is False
    assert m.enforce_enabled() is False


def test_shadow_typo_stays_shadow(monkeypatch):
    """An actuator must fail SAFE: a typo'd shadow value must NOT enable
    redaction (only an explicit off un-shadows)."""
    _clear(monkeypatch)
    monkeypatch.setenv(_ENV_SHADOW, "garbage")
    assert m.shadow_mode_enabled() is True
    assert m.enforce_enabled() is False


def test_master_off_beats_un_shadow(monkeypatch):
    """Enforce requires BOTH master on and shadow off — master off wins."""
    _clear(monkeypatch)
    monkeypatch.setenv(_ENV_MASTER, "false")
    monkeypatch.setenv(_ENV_SHADOW, "false")
    assert m.enforce_enabled() is False


# ── detection works in shadow (verdict returned, mutation is the seam's job) ──

def test_scanner_detects_credential_in_shadow(monkeypatch):
    _clear(monkeypatch)  # default = shadow
    rep = m.scan_mcp_output(
        "export GH=ghp_1234567890abcdefghij1234567890abcdef", source_label="bash",
    )
    assert rep.verdict is m.McpScanVerdict.CREDENTIAL_FOUND
    assert len(rep.findings) >= 1
    assert rep.bytes_redacted > 0


# ── the seam honors enforce_enabled (redact only when enforcing) ─────

def test_seam_redacts_only_when_enforcing():
    """The tool-loop seam must consult enforce_enabled() before mutating —
    guard against a regression that redacts in shadow."""
    import inspect
    from backend.core.ouroboros.governance import tool_executor
    src = inspect.getsource(tool_executor)
    assert "enforce_enabled as _cred_enforce" in src
    # The redaction (type(tool_result)(output=_redacted...)) must be guarded by
    # the enforce predicate, and there must be a shadow-log branch.
    assert "if _cred_enforce():" in src
    assert "credential scan SHADOW" in src
