from __future__ import annotations
import pytest
from backend.core.ouroboros.governance.fleet_repair_battery import (
    BATTERY, repair_one,
)
from backend.core.ouroboros.governance.fleet_evaluator import ProbeResult


def _defect(name):
    return next(d for d in BATTERY if d.name == name)


def _caller(text, ok=True, toks=50):
    async def call(model, messages, *, max_tokens):
        return ProbeResult(text=text, ttft_ms=100.0, total_ms=1000.0,
                           completion_tokens=toks, ok=ok, error="")
    return call


@pytest.mark.asyncio
async def test_correct_fix_reaches_state_applied():
    good = "```python\ndef add_two(a, b):\n    return a + b\n```"
    res = await repair_one(_caller(good), _defect("arithmetic"), models=("m",))
    assert res.applied is True and res.note == "state=applied"


@pytest.mark.asyncio
async def test_wrong_fix_fails_verify():
    bad = "```python\ndef add_two(a, b):\n    return a - b\n```"   # still buggy
    res = await repair_one(_caller(bad), _defect("arithmetic"), models=("m",))
    assert res.applied is False and res.note == "verify_failed"


@pytest.mark.asyncio
async def test_ast_invalid_is_rejected():
    res = await repair_one(_caller("```python\ndef add_two(((\n```"),
                           _defect("arithmetic"), models=("m",))
    assert res.applied is False and res.note == "ast_invalid"


@pytest.mark.asyncio
async def test_provider_not_ok_cascades_then_fails():
    res = await repair_one(_caller("", ok=False), _defect("arithmetic"), models=("m",))
    assert res.applied is False and res.note.startswith("provider_not_ok")
