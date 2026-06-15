"""Slice 256 C.1 — pure, deterministic pieces of the Live-Fire Validation Engine.
Standalone (no unified_supervisor import) → runs in-sandbox."""
import importlib.util as _u
import sys as _sys

import pytest

_spec = _u.spec_from_file_location(
    "live_kernel_validator",
    "backend/core/ouroboros/governance/live_kernel_validator.py",
)
lkv = _u.module_from_spec(_spec)
_sys.modules["live_kernel_validator"] = lkv  # register so @dataclass annotations resolve
_spec.loader.exec_module(lkv)


# ── deterministic retry budget ──
def test_budget_base_and_per_file(monkeypatch):
    monkeypatch.delenv("JARVIS_LIVEFIRE_RETRY_BASE", raising=False)
    monkeypatch.delenv("JARVIS_LIVEFIRE_RETRY_PER_FILE", raising=False)
    monkeypatch.delenv("JARVIS_MAX_LIVEFIRE_RETRIES", raising=False)
    assert lkv.livefire_retry_budget(1) == 3          # base
    assert lkv.livefire_retry_budget(3) == 5          # base + 2*per_file
    assert lkv.livefire_retry_budget(50) == 8         # hard cap


def test_budget_env_overrides(monkeypatch):
    monkeypatch.setenv("JARVIS_LIVEFIRE_RETRY_BASE", "1")
    monkeypatch.setenv("JARVIS_LIVEFIRE_RETRY_PER_FILE", "2")
    monkeypatch.setenv("JARVIS_MAX_LIVEFIRE_RETRIES", "100")
    assert lkv.livefire_retry_budget(1) == 1
    assert lkv.livefire_retry_budget(4) == 1 + 2 * 3  # 7


def test_budget_floor_is_one(monkeypatch):
    monkeypatch.setenv("JARVIS_LIVEFIRE_RETRY_BASE", "0")
    assert lkv.livefire_retry_budget(0) >= 1


# ── secret-sanitized state dump ──
def test_sanitize_redacts_by_key_name():
    out = lkv.sanitize_state_dump({"ANTHROPIC_API_KEY": "whatever", "ok": "v"})
    assert out["ANTHROPIC_API_KEY"] == "***REDACTED***"
    assert out["ok"] == "v"


def test_sanitize_redacts_by_value_shape():
    for secret in ("sk-ant-abcdef123456", "ghp_DEADBEEF12345678", "hf_AbCdEf123456"):
        out = lkv.sanitize_state_dump({"field": secret})
        assert out["field"] == "***REDACTED***"


def test_sanitize_collapses_home_path():
    import os
    home = os.path.expanduser("~")
    out = lkv.sanitize_state_dump({"path": f"{home}/secret/file"})
    assert home not in out["path"] and out["path"].startswith("~")


def test_sanitize_recurses_and_never_raises():
    payload = {"a": [{"token": "ghp_AAAAAAAA1111"}, "plain"], "b": ("sk-xyz12345678",)}
    out = lkv.sanitize_state_dump(payload)
    assert out["a"][0]["token"] == "***REDACTED***"
    assert out["a"][1] == "plain"
    assert out["b"][0] == "***REDACTED***"
    # malformed / odd input still returns (never raises)
    assert lkv.sanitize_state_dump(object()) is not None


# ── kernel-surface gate ──
def test_affects_kernel_gate():
    assert lkv.LiveKernelValidator.affects_kernel(["unified_supervisor.py"]) is True
    assert lkv.LiveKernelValidator.affects_kernel(["backend/core/x.py"]) is True
    assert lkv.LiveKernelValidator.affects_kernel(["tests/x.py", "docs/y.md"]) is False
    assert lkv.LiveKernelValidator.affects_kernel([]) is False


# ── cascade breaker counting ──
def test_breaker_trips_at_threshold(monkeypatch):
    monkeypatch.setenv("JARVIS_CASCADE_ESCALATION_THRESHOLD", "3")
    b = lkv.CascadeFailureBreaker()
    assert b.record_escalation() is False
    assert b.record_escalation() is False
    assert b.record_escalation() is True   # 3rd consecutive trips


def test_breaker_reset_on_clean_task(monkeypatch):
    monkeypatch.setenv("JARVIS_CASCADE_ESCALATION_THRESHOLD", "3")
    b = lkv.CascadeFailureBreaker()
    b.record_escalation(); b.record_escalation()
    b.record_clean_task()                  # resets the consecutive counter
    assert b.record_escalation() is False  # back to 1, not 3
