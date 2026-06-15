"""Slice 256 C.4 — livefire_validate_gate: the VALIDATE-phase decision logic
(pass / retry→GENERATE / escalate→OrangePRReviewer / cascade-suspend). All deps
injected → deterministic, in-sandbox; no orchestrator or kernel import."""
import importlib.util as _u
import sys as _sys

import pytest

_spec = _u.spec_from_file_location(
    "live_kernel_validator",
    "backend/core/ouroboros/governance/live_kernel_validator.py",
)
lkv = _u.module_from_spec(_spec)
_sys.modules["live_kernel_validator"] = lkv
_spec.loader.exec_module(lkv)

KERNEL = ["unified_supervisor.py"]


class _FakeValidator:
    def __init__(self, result):
        self._result = result
    @staticmethod
    def affects_kernel(files):
        return lkv.LiveKernelValidator.affects_kernel(files)
    async def validate_patch(self, **kw):
        return self._result


def _ok():
    return lkv.LiveFireResult(ok=True, exercised=["f"])


def _fail():
    return lkv.LiveFireResult(ok=False, exception_type="NameError",
                              traceback="NameError: name 'logger' is not defined")


@pytest.mark.asyncio
async def test_pass_proceeds():
    escalated = []
    out = await lkv.livefire_validate_gate(
        changed_files=KERNEL, affected_symbols=["f"], attempt=0,
        validator=_FakeValidator(_ok()), breaker=lkv.CascadeFailureBreaker(),
        escalate=lambda d: escalated.append(d))
    assert out["verdict"] == "pass" and escalated == []


@pytest.mark.asyncio
async def test_non_kernel_skips_validation():
    out = await lkv.livefire_validate_gate(
        changed_files=["tests/x.py"], affected_symbols=["f"], attempt=0,
        validator=_FakeValidator(_fail()),  # would fail, but skipped
        breaker=lkv.CascadeFailureBreaker(), escalate=lambda d: None)
    assert out["verdict"] == "pass" and out["reason"] == "non_kernel_skip"


@pytest.mark.asyncio
async def test_fail_within_budget_routes_to_generate(monkeypatch):
    monkeypatch.setenv("JARVIS_LIVEFIRE_RETRY_BASE", "3")
    out = await lkv.livefire_validate_gate(
        changed_files=KERNEL, affected_symbols=["x"], attempt=0,
        validator=_FakeValidator(_fail()), breaker=lkv.CascadeFailureBreaker(),
        escalate=lambda d: None)
    assert out["verdict"] == "retry"
    assert "live-fire" in out["feedback"].lower() and "NameError" in out["feedback"]


@pytest.mark.asyncio
async def test_budget_exhausted_escalates_with_sanitized_dump(monkeypatch):
    monkeypatch.setenv("JARVIS_LIVEFIRE_RETRY_BASE", "3")
    monkeypatch.setenv("JARVIS_LIVEFIRE_RETRY_PER_FILE", "1")
    captured = {}
    async def escalate(dump): captured["dump"] = dump
    out = await lkv.livefire_validate_gate(
        changed_files=KERNEL, affected_symbols=["x"], attempt=3,  # == budget(1 file)=3 → exhausted
        validator=_FakeValidator(_fail()), breaker=lkv.CascadeFailureBreaker(),
        escalate=escalate,
        generation_context={"prompt": "boot", "ANTHROPIC_API_KEY": "sk-ant-SECRET12345"})
    assert out["verdict"] == "escalated"
    # the StateDump on the PR is secret-sanitized
    assert captured["dump"]["generation_context"]["ANTHROPIC_API_KEY"] == "***REDACTED***"
    assert captured["dump"]["livefire"]["exception_type"] == "NameError"


@pytest.mark.asyncio
async def test_three_consecutive_escalations_trip_cascade(monkeypatch):
    monkeypatch.setenv("JARVIS_LIVEFIRE_RETRY_BASE", "1")
    monkeypatch.setenv("JARVIS_CASCADE_ESCALATION_THRESHOLD", "3")
    breaker = lkv.CascadeFailureBreaker()
    verdicts = []
    for _ in range(3):
        out = await lkv.livefire_validate_gate(
            changed_files=KERNEL, affected_symbols=["x"], attempt=1,  # >= budget(base 1) → escalate
            validator=_FakeValidator(_fail()), breaker=breaker, escalate=lambda d: None)
        verdicts.append(out["verdict"])
    assert verdicts == ["escalated", "escalated", "suspended"]  # 3rd trips the cascade


@pytest.mark.asyncio
async def test_escalation_failure_is_failsoft(monkeypatch):
    monkeypatch.setenv("JARVIS_LIVEFIRE_RETRY_BASE", "1")
    async def boom(d): raise RuntimeError("PR API down")
    out = await lkv.livefire_validate_gate(
        changed_files=KERNEL, affected_symbols=["x"], attempt=5,
        validator=_FakeValidator(_fail()), breaker=lkv.CascadeFailureBreaker(), escalate=boom)
    assert out["verdict"] in ("escalated", "suspended")  # escalation crash didn't crash VALIDATE
