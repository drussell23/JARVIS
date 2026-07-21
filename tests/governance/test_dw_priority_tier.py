"""Priority-Tier Injection — the missing ``service_tier`` selector.

Root cause of the 66s-TTFT soak deaths: every "realtime" GENERATE posted to
``/v1/chat/completions`` WITHOUT ``service_tier`` — DW served the default
async tier. These tests pin: (1) the ONE decision helper's env contract,
(2) the adaptive per-model rejection learner, (3) AST wiring — every
realtime-plane composer calls the helper, NO batch composer does, and
(4) the wire truth: a real ``complete_sync`` POST carries the selector.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from backend.core.ouroboros.governance import doubleword_provider as dwp

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_DW_SRC = (_ROOT / "backend" / "core" / "ouroboros" / "governance"
           / "doubleword_provider.py").read_text()


@pytest.fixture(autouse=True)
def _clean_tier_state(monkeypatch):
    """Process-global caches must not leak between tests (or into the
    real ledger of any other suite)."""
    monkeypatch.delenv("JARVIS_DW_SERVICE_TIER_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_DW_RT_SERVICE_TIER", raising=False)
    saved_rejected = set(dwp._DW_TIER_PARAM_REJECTED)
    saved_logged = list(dwp._DW_TIER_FIRST_INJECT_LOGGED)
    dwp._DW_TIER_PARAM_REJECTED.clear()
    dwp._DW_TIER_FIRST_INJECT_LOGGED.clear()
    yield
    dwp._DW_TIER_PARAM_REJECTED.clear()
    dwp._DW_TIER_PARAM_REJECTED.update(saved_rejected)
    dwp._DW_TIER_FIRST_INJECT_LOGGED.clear()
    dwp._DW_TIER_FIRST_INJECT_LOGGED.extend(saved_logged)


# ---------------------------------------------------------------------------
# The decision helper — env contract
# ---------------------------------------------------------------------------

def test_default_injects_priority():
    body = dwp.apply_rt_service_tier({"model": "m"}, "qwen-397b")
    assert body["service_tier"] == "priority"


def test_master_off_no_injection(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_SERVICE_TIER_ENABLED", "false")
    body = dwp.apply_rt_service_tier({"model": "m"}, "qwen-397b")
    assert "service_tier" not in body


def test_env_tier_override(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_RT_SERVICE_TIER", "express")
    body = dwp.apply_rt_service_tier({"model": "m"}, "qwen-397b")
    assert body["service_tier"] == "express"


def test_empty_tier_disables(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_RT_SERVICE_TIER", "")
    body = dwp.apply_rt_service_tier({"model": "m"}, "qwen-397b")
    assert "service_tier" not in body


def test_caller_override_preserved():
    """A body that already carries a tier is NEVER overwritten."""
    body = dwp.apply_rt_service_tier(
        {"model": "m", "service_tier": "custom"}, "qwen-397b")
    assert body["service_tier"] == "custom"


def test_rejected_model_omits():
    dwp._DW_TIER_PARAM_REJECTED.add("qwen-397b")
    body = dwp.apply_rt_service_tier({"model": "m"}, "qwen-397b")
    assert "service_tier" not in body
    # ...but OTHER models keep the tier (per-model cache, not global off)
    other = dwp.apply_rt_service_tier({"model": "m"}, "gemma-31b")
    assert other["service_tier"] == "priority"


# ---------------------------------------------------------------------------
# The adaptive rejection learner
# ---------------------------------------------------------------------------

def test_learner_caches_unknown_param_400():
    learned = dwp._dw_note_tier_param_rejection(
        "qwen-397b", 400, '{"error": "unknown parameter: service_tier"}')
    assert learned is True
    assert dwp._dw_tier_param_known_unsupported("qwen-397b")


def test_learner_ignores_unrelated_400():
    """A 400 that does NOT name service_tier (schema, quota text) is
    someone else's fact — never cached here."""
    learned = dwp._dw_note_tier_param_rejection(
        "qwen-397b", 400, '{"error": "credit balance too low"}')
    assert learned is False
    assert not dwp._dw_tier_param_known_unsupported("qwen-397b")


def test_learner_status_gated():
    """A 429/403 mentioning service_tier is NOT a param rejection —
    entitlement/quota seams own those statuses."""
    for status in (403, 429, 503):
        assert dwp._dw_note_tier_param_rejection(
            "qwen-397b", status, "service_tier denied") is False
    assert not dwp._dw_tier_param_known_unsupported("qwen-397b")


def test_learner_then_helper_omits():
    """The full adaptive loop: rejection → cache → next body build omits."""
    dwp._dw_note_tier_param_rejection(
        "m1", 422, "invalid parameter service_tier for this deployment")
    body = dwp.apply_rt_service_tier({"model": "m1"}, "m1")
    assert "service_tier" not in body


# ---------------------------------------------------------------------------
# AST wiring pins — realtime plane composes the helper, batch plane NEVER
# ---------------------------------------------------------------------------

def _calls_in_function(func_name: str, callee: str) -> int:
    """Count ``callee(...)`` call sites inside the named function/method."""
    tree = ast.parse(_DW_SRC)
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == func_name:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    f = sub.func
                    name = getattr(f, "id", None) or getattr(f, "attr", None)
                    if name == callee:
                        count += 1
    return count


def test_realtime_plane_is_wired():
    # main GENERATE (SSE + non-streaming variants share the one body build)
    assert _calls_in_function("_generate_realtime", "apply_rt_service_tier") >= 1
    # heavy fn / Functions lane
    assert _calls_in_function("complete_sync", "apply_rt_service_tier") >= 1
    # the stability probe measures the tier we actually generate on
    assert _calls_in_function("stream_health_probe", "apply_rt_service_tier") >= 1


def test_batch_plane_is_not_wired():
    """The 2× batch discount is the point of that plane — the tier selector
    must never leak into a batch JSONL body."""
    for batch_fn in ("submit_batch", "prompt_only", "_generate_via_batch",
                     "poll_and_retrieve"):
        assert _calls_in_function(batch_fn, "apply_rt_service_tier") == 0, batch_fn


def test_rejection_learner_covers_every_rt_error_seam():
    # both _generate_realtime error branches (streaming + non-streaming)
    assert _calls_in_function(
        "_generate_realtime", "_dw_note_tier_param_rejection") >= 2
    # the complete_sync failure composition seam
    assert _calls_in_function(
        "_handle_rt_http_failure", "_dw_note_tier_param_rejection") >= 1


def test_cognition_lanes_delegates_to_canonical_helper():
    """DRY: the council/cognition RT lane composes the SAME decision seam
    (master flag + env tier + rejection cache honored everywhere)."""
    src = (_ROOT / "backend" / "core" / "ouroboros" / "governance"
           / "cognition_lanes.py").read_text()
    assert "apply_rt_service_tier" in src


# ---------------------------------------------------------------------------
# Wire truth — a REAL complete_sync POST carries the selector
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_complete_sync_sends_priority_on_the_wire(monkeypatch):
    import aiohttp
    from aiohttp import web

    seen = {}

    async def _serve(request):
        seen.update(await request.json())
        return web.json_response({
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        })

    app = web.Application()
    app.router.add_post("/v1/chat/completions", _serve)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        dw = dwp.DoublewordProvider(
            api_key="test-key",
            base_url=f"http://127.0.0.1:{port}/v1",
            model="test-model",
        )
        result = await dw.complete_sync(
            "ping", system_prompt="you are a test", caller_id="tier_test",
            max_tokens=8, timeout_s=10.0)
        assert result.content == "ok"
        assert seen.get("service_tier") == "priority"
        assert seen.get("stream") is False
    finally:
        await runner.cleanup()
