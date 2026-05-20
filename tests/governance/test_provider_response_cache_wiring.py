"""S1 wiring spine — proves the gate is correctly wired into the two
provider seams without running through the ~1k-line ClaudeProvider
_generate_raw closure (D2: kept in place).

Coverage:
  * Structural (AST) pins on both providers: imports are
    cached_or_generate + response_cache_enabled ONLY; no inline cache
    class; no OrderedDict-LRU; gate block calls cached_or_generate
    with a produce thunk; gate is guarded by the correct
    tools/tool_loop predicate; cost-contract precedes the gate
    (Claude); DW assembles the prompt and threads prompt_override.
  * Method-presence pins for the D2 extracts.
  * Behavioral confirms via the substrate's cached_or_generate
    directly (same shape Claude/DW use): master OFF → DISABLED +
    produce every call; master ON → MISS then EXACT_HIT with $0 +
    +cache tag + produce called once; is_noop not stored; fault →
    FAULT_FAIL_OPEN; repo-digest change → not EXACT_HIT.
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import (
    provider_response_cache as prc,
)
from backend.core.ouroboros.governance.op_context import (
    GenerationResult,
)


PROVIDERS_PY = (
    "backend/core/ouroboros/governance/providers.py"
)
DW_PY = (
    "backend/core/ouroboros/governance/doubleword_provider.py"
)


def _ast(path: str):
    src = Path(path).read_text(encoding="utf-8")
    return src, ast.parse(src)


def _gr(provider="claude", noop=False):
    return GenerationResult(
        candidates=(
            {"file_path": "x.py", "full_content": "print(1)"},
        ),
        provider_name=provider,
        generation_duration_s=0.4,
        model_id="m", is_noop=noop,
        total_input_tokens=10, total_output_tokens=5, cost_usd=0.5,
    )


@pytest.fixture(autouse=True)
def _iso(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "JARVIS_PROVIDER_CACHE_PATH", str(tmp_path / "t.jsonl"),
    )
    monkeypatch.setenv("JARVIS_PROVIDER_RESPONSE_CACHE_ENABLED", "true")
    prc.reset_default_cache_for_tests()
    yield
    prc.reset_default_cache_for_tests()


# --------------------------------------------------------------------------
# Structural / AST pins — providers wired correctly without running them
# --------------------------------------------------------------------------


def test_claude_imports_only_cached_or_generate():
    src, tree = _ast(PROVIDERS_PY)
    imports = [
        a.name
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom)
        and n.module and "provider_response_cache" in n.module
        for a in n.names
    ]
    assert set(imports) <= {
        "cached_or_generate", "response_cache_enabled",
    }, imports
    classes = [
        n.name for n in ast.walk(tree)
        if isinstance(n, ast.ClassDef) and "Cache" in n.name
    ]
    assert classes == [], (
        f"no inline cache classes allowed in providers.py: {classes}"
    )
    has_od_lru = any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "OrderedDict"
        for n in ast.walk(tree)
    )
    assert not has_od_lru


def test_dw_imports_only_cached_or_generate():
    src, tree = _ast(DW_PY)
    imports = [
        a.name
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom)
        and n.module and "provider_response_cache" in n.module
        for a in n.names
    ]
    assert set(imports) <= {
        "cached_or_generate", "response_cache_enabled",
    }, imports
    classes = [
        n.name for n in ast.walk(tree)
        if isinstance(n, ast.ClassDef) and "Cache" in n.name
    ]
    assert classes == []


def test_claude_gate_guard_and_produce_present():
    src, _ = _ast(PROVIDERS_PY)
    # gate guard: NOT self._tools_enabled AND self._tool_loop is None
    assert "not self._tools_enabled" in src
    assert "self._tool_loop is None" in src
    # produce thunk + cached_or_generate call
    assert "_no_tools_inner" in src
    assert "_cached_or_generate(" in src
    assert "produce=_no_tools_inner" in src


def test_dw_gate_guard_and_produce_present():
    src, _ = _ast(DW_PY)
    # _will_skip_tools discipline (trivial/simple) or tool_loop None
    assert '"trivial"' in src and '"simple"' in src
    assert "self._tool_loop is None" in src
    assert "_dw_inner" in src
    assert "_zw_cached_or_generate(" in src
    assert "produce=_dw_inner" in src
    # prompt_override propagation through the dispatcher extract
    assert "_dispatch_internal" in src
    assert "prompt_override=_zw_prompt" in src


def test_cost_contract_precedes_cache_in_claude():
    """The PRD §26.6.2 cost contract gate MUST fire on every call —
    a cached hit would silently bypass it if the gate were earlier.
    Pin source-order: assert_provider_route_compatible appears
    BEFORE _cached_or_generate."""
    src, _ = _ast(PROVIDERS_PY)
    i_contract = src.find("assert_provider_route_compatible(")
    i_gate = src.find("_cached_or_generate(")
    assert i_contract > 0 and i_gate > 0
    assert i_contract < i_gate, (
        "cost-contract gate must precede the response cache gate"
    )


def test_claude_extracts_present():
    from backend.core.ouroboros.governance.providers import (
        ClaudeProvider,
    )
    assert hasattr(ClaudeProvider, "_assemble_codegen_prompt")
    assert hasattr(ClaudeProvider, "_finalize_codegen_result")


def test_dw_extract_present():
    from backend.core.ouroboros.governance.doubleword_provider import (
        DoublewordProvider,
    )
    assert hasattr(DoublewordProvider, "_dispatch_internal")


# --------------------------------------------------------------------------
# Behavioral confirms via the substrate (same shape as the provider
# wiring; avoids running through the 1k-line _generate_raw closure)
# --------------------------------------------------------------------------


def test_master_off_byte_identical_via_gate(monkeypatch):
    """master OFF → produce called every time, outcome DISABLED.
    This is the exact byte-identical-when-off contract the wiring
    inherits from the substrate."""
    monkeypatch.setenv(
        "JARVIS_PROVIDER_RESPONSE_CACHE_ENABLED", "false",
    )
    calls = {"n": 0}

    async def produce():
        calls["n"] += 1
        return _gr()

    async def run():
        g1, o1 = await prc.cached_or_generate(
            prompt="P", model="m", route="ide",
            repo_root=Path("."), produce=produce,
        )
        g2, o2 = await prc.cached_or_generate(
            prompt="P", model="m", route="ide",
            repo_root=Path("."), produce=produce,
        )
        return (g1, o1, g2, o2)

    g1, o1, g2, o2 = asyncio.run(run())
    assert o1 is prc.CacheLookupOutcome.DISABLED
    assert o2 is prc.CacheLookupOutcome.DISABLED
    assert calls["n"] == 2  # produce called every time
    assert g1.cost_usd == 0.5 and g2.cost_usd == 0.5


def test_master_on_repeat_zero_cost_provider_skipped():
    """master ON, 2nd identical call: produce NOT called,
    cost_usd=0.0, provider_name ends '+cache'. This is the exact
    behavior Claude/DW will exhibit on cache hit."""
    calls = {"n": 0}

    async def produce():
        calls["n"] += 1
        return _gr(provider="claude")

    async def run():
        await prc.cached_or_generate(
            prompt="P", model="claude-3", route="ide",
            repo_root=Path("."), produce=produce,
        )
        return await prc.cached_or_generate(
            prompt="P", model="claude-3", route="ide",
            repo_root=Path("."), produce=produce,
        )

    g2, o2 = asyncio.run(run())
    assert o2 is prc.CacheLookupOutcome.EXACT_HIT
    assert calls["n"] == 1                  # produce called once
    assert g2.cost_usd == 0.0
    assert g2.provider_name.endswith("+cache")


def test_is_noop_not_cached():
    async def produce():
        return _gr(noop=True)

    async def run():
        await prc.cached_or_generate(
            prompt="Q", model="m", route="ide",
            repo_root=Path("."), produce=produce,
        )
        return await prc.cached_or_generate(
            prompt="Q", model="m", route="ide",
            repo_root=Path("."), produce=produce,
        )

    _, o2 = asyncio.run(run())
    assert o2 is not prc.CacheLookupOutcome.EXACT_HIT


def test_fault_fail_open(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("compute_key fault")

    monkeypatch.setattr(prc, "compute_cache_key", boom)
    calls = {"n": 0}

    async def produce():
        calls["n"] += 1
        return _gr()

    g, o = asyncio.run(prc.cached_or_generate(
        prompt="P", model="m", route="ide",
        repo_root=Path("."), produce=produce,
    ))
    assert o is prc.CacheLookupOutcome.FAULT_FAIL_OPEN
    assert calls["n"] == 1 and g is not None


def test_repo_state_change_does_not_serve_stale(monkeypatch):
    """Wiring invariant the providers inherit: repo-digest in the
    key invalidates any prior trajectory. Repeat with different
    repo state -> not EXACT_HIT (no stale-fix application)."""
    state = {"d": "digest-A"}
    monkeypatch.setattr(
        prc, "repo_state_digest", lambda _root: state["d"],
    )

    async def produce():
        return _gr()

    async def run():
        await prc.cached_or_generate(
            prompt="P", model="m", route="ide",
            repo_root=Path("."), produce=produce,
        )
        state["d"] = "digest-B"   # operator code change
        return await prc.cached_or_generate(
            prompt="P", model="m", route="ide",
            repo_root=Path("."), produce=produce,
        )

    _, o2 = asyncio.run(run())
    assert o2 is not prc.CacheLookupOutcome.EXACT_HIT
