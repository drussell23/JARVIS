"""Every large-file limit derives from the served model's negotiated window.

The flat 300-line / 8000-token limits described a cloud lane, not the model
answering. These tests pin the derivation chain: negotiated window → output
reserve → fixed overhead → ingest ceiling, the floor-derived fallback when no
node has been negotiated, the process cache the synchronous readers consult,
and the absence of any literal of the module's own.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance import context_budget as cb


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    cb.reset_cache()
    for k in ("JARVIS_CONTEXT_OUTPUT_RESERVE_FRACTION", "JARVIS_CONTEXT_BUDGET_TTL_S",
              "JARVIS_CONTEXT_INGEST_CEILING_TOKENS", "JARVIS_CONTEXT_CHARS_PER_TOKEN",
              "JARVIS_NUM_CTX_FLOOR", "JARVIS_DW_MAX_CONTEXT_TOKENS", "JARVIS_DW_BIG_FILE_LINE_THRESHOLD"):
        monkeypatch.delenv(k, raising=False)
    yield
    cb.reset_cache()


async def _neg(window):
    async def _n(_endpoint):
        return window
    return _n


def test_budget_math_is_window_minus_reserve_minus_overhead(monkeypatch):
    monkeypatch.setenv("JARVIS_CONTEXT_OUTPUT_RESERVE_FRACTION", "0.25")
    b = cb._budget_from_window("ep", 40_000, negotiated=True)
    assert b.output_reserve_tokens == 10_000 and b.ingest_ceiling_tokens == 30_000
    b2 = b.with_overhead("x" * 4_000, "y" * 4_000)      # 2_000 tokens of fixed parts
    assert b2.fixed_overhead_tokens == 2_000 and b2.ingest_ceiling_tokens == 28_000
    assert b2.node_budget_tokens == b2.ingest_ceiling_tokens


def test_reserve_fraction_derives_from_the_lanes_output_ratio(monkeypatch):
    class _Cfg:
        output_ratio = 1.0          # answer as long as the prompt → half the window

    from backend.core.ouroboros.governance import local_inference_director as lid
    monkeypatch.setattr(lid.LocalConfig, "from_env", classmethod(lambda cls: _Cfg()))
    assert cb.output_reserve_fraction() == pytest.approx(0.5)
    monkeypatch.setenv("JARVIS_CONTEXT_OUTPUT_RESERVE_FRACTION", "0.1")
    assert cb.output_reserve_fraction() == pytest.approx(0.1)


def test_line_threshold_is_the_files_own_density():
    b = cb._budget_from_window("ep", 4_000, negotiated=True)   # ceiling ≈ 3000 tokens
    sparse = "x\n" * 100          # 2 chars/line  → many lines fit
    dense = ("x" * 200 + "\n") * 100
    assert b.line_threshold_for(sparse) > b.line_threshold_for(dense)


@pytest.mark.asyncio
async def test_prime_caches_and_serves_sync_readers():
    b = await cb.prime_budget("http://node", await _neg(32_768), served_model="qwen3-coder-ov:30b")
    assert b.negotiated and b.window_tokens == 32_768 and b.served_model == "qwen3-coder-ov:30b"
    assert cb.current_budget("http://node") is b and cb.current_budget() is b
    assert cb.ingest_ceiling_tokens() == b.ingest_ceiling_tokens
    assert cb.exceeds_ceiling("x" * (b.ingest_ceiling_tokens * cb.chars_per_token() + 100))
    assert not cb.exceeds_ceiling("tiny")


@pytest.mark.asyncio
async def test_prime_is_fresh_within_ttl_and_renegotiates_after(monkeypatch):
    monkeypatch.setenv("JARVIS_CONTEXT_BUDGET_TTL_S", "1")
    calls = []

    async def _n(_e):
        calls.append(1); return 8_192

    await cb.prime_budget("ep", _n); await cb.prime_budget("ep", _n)
    assert len(calls) == 1
    b = cb.current_budget("ep")
    object.__setattr__(b, "primed_at", b.primed_at - 5)
    await cb.prime_budget("ep", _n)
    assert len(calls) == 2
    await cb.prime_budget("ep", _n, force=True)
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_no_negotiation_falls_to_the_negotiators_own_floor(monkeypatch):
    monkeypatch.setenv("JARVIS_NUM_CTX_FLOOR", "2048")

    async def _none(_e):
        return None

    async def _boom(_e):
        raise RuntimeError("node down")

    b = await cb.prime_budget("ep", _none)
    assert not b.negotiated and b.window_tokens == 2048
    cb.reset_cache()
    b = await cb.prime_budget("ep", _boom)
    assert not b.negotiated and b.window_tokens == 2048
    cb.reset_cache()
    assert cb.ingest_ceiling_tokens() < 2048, "unprimed readers still get a floor-derived ceiling"


def test_explicit_operator_override_wins(monkeypatch):
    monkeypatch.setenv("JARVIS_CONTEXT_INGEST_CEILING_TOKENS", "777")
    assert cb.ingest_ceiling_tokens() == 777


def test_the_legacy_readers_now_derive():
    """The flat literals are gone from the two modules that carried them."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[2] / "backend/core/ouroboros/governance"
    ic = (root / "intelligent_chunking.py").read_text(encoding="utf-8")
    cg = (root / "chunked_generation.py").read_text(encoding="utf-8")
    assert "_DEFAULT_CEILING = 8000" not in ic and "_DEFAULT_THRESHOLD = 300" not in cg
    from backend.core.ouroboros.governance.intelligent_chunking import dynamic_token_ceiling
    from backend.core.ouroboros.governance.chunked_generation import big_file_line_threshold, is_big_file
    assert dynamic_token_ceiling() == cb.ingest_ceiling_tokens()
    assert big_file_line_threshold() == cb.ingest_ceiling_tokens()
    assert is_big_file("x" * (cb.ingest_ceiling_tokens() * cb.chars_per_token() + 400))
    assert not is_big_file("small")


def test_legacy_env_names_are_overrides_only(monkeypatch):
    from backend.core.ouroboros.governance.intelligent_chunking import dynamic_token_ceiling
    from backend.core.ouroboros.governance.chunked_generation import is_big_file
    monkeypatch.setenv("JARVIS_DW_MAX_CONTEXT_TOKENS", "123")
    assert dynamic_token_ceiling() == 123
    monkeypatch.setenv("JARVIS_DW_BIG_FILE_LINE_THRESHOLD", "3")
    assert is_big_file("a\nb\nc\nd\n") and not is_big_file("a\nb\n")



@pytest.mark.asyncio
async def test_an_explicit_ceiling_is_authority_and_never_negotiates():
    import os
    os.environ["JARVIS_CONTEXT_INGEST_CEILING_TOKENS"] = "1234"
    try:
        calls = []

        async def _n(_e):
            calls.append(1); return 99_999

        b = await cb.prime_budget("ep", _n)
        assert calls == [] and b.ingest_ceiling_tokens == 1234 and not b.negotiated
        assert cb.ingest_ceiling_tokens() == 1234
    finally:
        os.environ.pop("JARVIS_CONTEXT_INGEST_CEILING_TOKENS", None)


def test_the_suite_pins_the_historical_numbers_explicitly(monkeypatch):
    """The conftest carries the old 300/8000 as explicit overrides for the
    legacy fixtures; the production code carries no such default."""
    import os
    assert os.environ.get("JARVIS_DW_BIG_FILE_LINE_THRESHOLD") in (None, "300")
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "conftest.py").read_text(encoding="utf-8")
    assert "_isolate_context_budget" in src
