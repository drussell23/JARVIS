"""S1 spine — ProviderResponseCache (Zero-Waste arc).

Pins every load-bearing PRD constraint: exact-hit->$0, repo-state
invalidation (no stale-fix), byte-budget LRU (16GB-M1), TTL,
persistence roundtrip, fail-open, fail-closed-on-correctness,
master-off byte-identical, composes-prompt_cache AST pin.
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import (
    provider_response_cache as prc,
)
from backend.core.ouroboros.governance.op_context import (
    GenerationResult,
)


@pytest.fixture(autouse=True)
def _iso(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "JARVIS_PROVIDER_CACHE_PATH", str(tmp_path / "t.jsonl"),
    )
    monkeypatch.setenv("JARVIS_PROVIDER_RESPONSE_CACHE_ENABLED", "true")
    for k in ("JARVIS_PROVIDER_CACHE_MAX_BYTES",
              "JARVIS_PROVIDER_CACHE_TTL_S"):
        monkeypatch.delenv(k, raising=False)
    prc.reset_default_cache_for_tests()
    yield
    prc.reset_default_cache_for_tests()


def _gr(content="print(1)", noop=False):
    return GenerationResult(
        candidates=({"file_path": "x.py", "full_content": content},),
        provider_name="dw", generation_duration_s=1.0,
        model_id="m", is_noop=noop,
        total_input_tokens=10, total_output_tokens=5, cost_usd=0.5,
    )


# -- flags / knobs ---------------------------------------------------------


# Slice 131 P1 GRADUATED this master to default-TRUE (the cache is
# correctness-fail-closed: any git diff re-keys, so default-on can only
# eliminate redundant identical-context calls). Only explicit off-values
# disable; unset/empty/garbage now resolve TRUE.
@pytest.mark.parametrize("raw,exp", [
    (None, True), ("", True), ("1", True), ("true", True),
    ("YES", True), ("0", False), ("false", False), ("off", False),
    ("garbage", True)])
def test_master_default_true_asymmetric_slice131(monkeypatch, raw, exp):
    if raw is None:
        monkeypatch.delenv(
            "JARVIS_PROVIDER_RESPONSE_CACHE_ENABLED", raising=False)
    else:
        monkeypatch.setenv(
            "JARVIS_PROVIDER_RESPONSE_CACHE_ENABLED", raw)
    assert prc.response_cache_enabled() is exp


@pytest.mark.parametrize("raw,exp", [
    (None, 268_435_456), ("", 268_435_456), ("x", 268_435_456),
    ("100", 1_048_576), ("999999999999", 4_294_967_296),
    ("268435456", 268_435_456)])
def test_max_bytes_clamp(monkeypatch, raw, exp):
    if raw is None:
        monkeypatch.delenv(
            "JARVIS_PROVIDER_CACHE_MAX_BYTES", raising=False)
    else:
        monkeypatch.setenv("JARVIS_PROVIDER_CACHE_MAX_BYTES", raw)
    assert prc.cache_max_bytes() == exp


def test_ttl_clamp(monkeypatch):
    monkeypatch.setenv("JARVIS_PROVIDER_CACHE_TTL_S", "0")
    assert prc.cache_ttl_s() == 1.0
    monkeypatch.setenv("JARVIS_PROVIDER_CACHE_TTL_S", "junk")
    assert prc.cache_ttl_s() == 86_400.0


# -- repo digest / key (correctness) ---------------------------------------


def test_repo_digest_stable_and_failclosed():
    d = prc.repo_state_digest(Path("."))
    assert d and not d.startswith("UNDETERMINED-")
    assert prc.repo_state_digest(Path(".")) == d  # stable, same state
    bad1 = prc.repo_state_digest(Path("/nonexistent/zz"))
    bad2 = prc.repo_state_digest(Path("/nonexistent/zz"))
    assert bad1.startswith("UNDETERMINED-")
    assert bad1 != bad2  # unique nonce each call -> guaranteed miss


def test_key_stable_full_differs_prefix():
    a = prc.compute_cache_key("P", "m", "ide", Path("."))
    b = prc.compute_cache_key("P", "m", "ide", Path("."))
    assert a == b and a[0] != a[1]
    c = prc.compute_cache_key("P", "m2", "ide", Path("."))
    assert c[1] != a[1]  # model change -> different prefix


# -- trajectory dto --------------------------------------------------------


def test_trajectory_roundtrip_and_baddict():
    t = prc._trajectory_from_generation_result("F", "P", _gr())
    assert t is not None
    d = t.to_dict()
    t2 = prc.CachedTrajectory.from_dict(d)
    assert t2 is not None and t2.full_key == "F" and t2.candidates == t.candidates
    assert prc.CachedTrajectory.from_dict({"bogus": 1}) is None
    with pytest.raises(Exception):
        t.full_key = "z"  # frozen


def test_unserializable_candidates_not_cached():
    class Weird:  # not JSON-serializable
        pass
    gr = GenerationResult(
        candidates=({"obj": Weird()},), provider_name="dw",
        generation_duration_s=1.0, model_id="m")
    assert prc._trajectory_from_generation_result("F", "P", gr) is None


def test_reconstruct_zero_cost_cache_tag():
    t = prc._trajectory_from_generation_result("F", "P", _gr())
    gr = prc.reconstruct_generation_result(t)
    assert gr.cost_usd == 0.0
    assert gr.provider_name.endswith("+cache")
    assert gr.generation_duration_s == 0.0


# -- ring: hit / miss / invalidate / ttl / byte-LRU ------------------------


def test_store_then_exact_hit():
    c = prc.ProviderResponseCache()
    t = prc._trajectory_from_generation_result("F1", "P1", _gr())
    assert c.store(t)
    o, got = c.lookup("F1", "P1")
    assert o is prc.CacheLookupOutcome.EXACT_HIT and got.full_key == "F1"


def test_cold_miss_vs_invalidated_repo_change():
    c = prc.ProviderResponseCache()
    c.store(prc._trajectory_from_generation_result("F1", "PFX", _gr()))
    # different full key, SAME prefix => repo changed, not cold miss
    o, _ = c.lookup("F2", "PFX")
    assert o is prc.CacheLookupOutcome.INVALIDATED_REPO_CHANGE
    o2, _ = c.lookup("F9", "OTHER")
    assert o2 is prc.CacheLookupOutcome.MISS


def test_ttl_expiry(monkeypatch):
    monkeypatch.setenv("JARVIS_PROVIDER_CACHE_TTL_S", "1")
    c = prc.ProviderResponseCache()
    t = prc._trajectory_from_generation_result("F", "P", _gr())
    # force created_at into the past
    object.__setattr__(t, "created_at", t.created_at - 100.0)
    c.store(t)
    o, _ = c.lookup("F", "P")
    assert o in (prc.CacheLookupOutcome.MISS,
                 prc.CacheLookupOutcome.INVALIDATED_REPO_CHANGE)


def test_byte_lru_drop_oldest_within_budget():
    c = prc.ProviderResponseCache(max_bytes=120, ttl_s=9999)
    for i in range(6):
        c.store(prc._trajectory_from_generation_result(
            f"F{i}", f"P{i}", _gr(content="z" * 30)))
    st = c.stats()
    assert st["bytes"] <= 120  # never exceeds byte budget
    assert c.lookup("F0", "P0")[0] is not prc.CacheLookupOutcome.EXACT_HIT
    assert c.lookup("F5", "P5")[0] is prc.CacheLookupOutcome.EXACT_HIT


def test_lru_bump_on_hit():
    # Size the budget from the ACTUAL entry bytes (non-brittle):
    # a budget that holds exactly 2 entries.
    sample = prc._trajectory_from_generation_result(
        "S", "S", _gr(content="z" * 40))
    c = prc.ProviderResponseCache(
        max_bytes=2 * sample.n_bytes, ttl_s=9999)
    c.store(prc._trajectory_from_generation_result(
        "F0", "P0", _gr(content="z" * 40)))
    c.store(prc._trajectory_from_generation_result(
        "F1", "P1", _gr(content="z" * 40)))  # cache full (2 entries)
    c.lookup("F0", "P0")                      # bump F0 to MRU
    c.store(prc._trajectory_from_generation_result(
        "F2", "P2", _gr(content="z" * 40)))   # evicts LRU == F1
    assert c.lookup("F0", "P0")[0] is prc.CacheLookupOutcome.EXACT_HIT
    assert c.lookup("F1", "P1")[0] is not prc.CacheLookupOutcome.EXACT_HIT


# -- persistence (cross-session) -------------------------------------------


def test_persistence_replay_new_instance(tmp_path):
    c1 = prc.ProviderResponseCache()
    c1.store(prc._trajectory_from_generation_result("F", "P", _gr()))
    assert Path(tmp_path / "t.jsonl").exists()
    c2 = prc.ProviderResponseCache()  # fresh -> replays from disk
    o, got = c2.lookup("F", "P")
    assert o is prc.CacheLookupOutcome.EXACT_HIT and got is not None


# -- gate (the seam) -------------------------------------------------------


def test_gate_disabled_byte_identical(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PROVIDER_RESPONSE_CACHE_ENABLED", "false")
    n = {"c": 0}

    async def produce():
        n["c"] += 1
        return _gr()

    gr, o = asyncio.run(prc.cached_or_generate(
        prompt="P", model="m", route="ide",
        repo_root=Path("."), produce=produce))
    assert o is prc.CacheLookupOutcome.DISABLED and n["c"] == 1
    assert gr.cost_usd == 0.5  # untouched real result


def test_gate_miss_then_exact_hit_zero_cost():
    n = {"c": 0}

    async def produce():
        n["c"] += 1
        return _gr()

    async def run():
        g1, o1 = await prc.cached_or_generate(
            prompt="P", model="m", route="ide",
            repo_root=Path("."), produce=produce)
        g2, o2 = await prc.cached_or_generate(
            prompt="P", model="m", route="ide",
            repo_root=Path("."), produce=produce)
        return (o1, g1, o2, g2)

    o1, g1, o2, g2 = asyncio.run(run())
    assert o1 is prc.CacheLookupOutcome.MISS and g1.cost_usd == 0.5
    assert o2 is prc.CacheLookupOutcome.EXACT_HIT
    assert g2.cost_usd == 0.0 and g2.provider_name.endswith("+cache")
    assert n["c"] == 1  # provider skipped on the 2nd call


def test_gate_noop_not_stored():
    async def produce():
        return _gr(noop=True)

    async def run():
        await prc.cached_or_generate(
            prompt="Q", model="m", route="ide",
            repo_root=Path("."), produce=produce)
        # 2nd call must still miss (noop never cached)
        return await prc.cached_or_generate(
            prompt="Q", model="m", route="ide",
            repo_root=Path("."), produce=produce)

    _, o = asyncio.run(run())
    assert o is not prc.CacheLookupOutcome.EXACT_HIT


def test_gate_fail_open_on_lookup_fault(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("cache exploded")

    monkeypatch.setattr(prc, "compute_cache_key", boom)
    n = {"c": 0}

    async def produce():
        n["c"] += 1
        return _gr()

    gr, o = asyncio.run(prc.cached_or_generate(
        prompt="P", model="m", route="ide",
        repo_root=Path("."), produce=produce))
    assert o is prc.CacheLookupOutcome.FAULT_FAIL_OPEN
    assert n["c"] == 1 and gr is not None  # real path still ran


def test_store_never_raises():
    c = prc.ProviderResponseCache()
    assert c.store(None) is False  # no raise


# -- registration contract -------------------------------------------------


def test_register_flags_four():
    """Seed count pin. Master + shadow + max_bytes + ttl_s = 4.
    PRD §10.10: shadow added as the 4th seed."""
    seen = []

    class _R:
        def register(self, s):
            seen.append(s.name)

    assert prc.register_flags(_R()) == 4
    assert "JARVIS_PROVIDER_RESPONSE_CACHE_ENABLED" in seen
    assert "JARVIS_PROVIDER_RESPONSE_CACHE_SHADOW" in seen


def test_ast_pin_self_validates_green():
    invs = prc.register_shipped_invariants()
    assert len(invs) == 1
    src = Path(prc.__file__).read_text(encoding="utf-8")
    assert invs[0].validate(ast.parse(src), src) == ()


# -- shadow mode (PRD §10.10) ----------------------------------------------


def _gr_with_cost(cost_usd=0.5, content="print(1)"):
    """Builds a GenerationResult with a specific cost_usd, used to
    verify ``original_cost_usd`` is captured at store time and surfaced
    as the shadow-mode ``cost_would_have_saved`` telemetry value."""
    return GenerationResult(
        candidates=({"file_path": "x.py", "full_content": content},),
        provider_name="dw", generation_duration_s=1.0,
        model_id="m", is_noop=False,
        total_input_tokens=10, total_output_tokens=5,
        cost_usd=cost_usd,
    )


def test_shadow_mode_default_false(monkeypatch):
    """Independent of master; default-FALSE; case-insensitive parser
    matches master's discipline."""
    monkeypatch.delenv("JARVIS_PROVIDER_RESPONSE_CACHE_SHADOW",
                       raising=False)
    assert prc.shadow_mode_enabled() is False
    for val, exp in [("true", True), ("1", True), ("YES", True),
                     ("on", True), ("false", False), ("", False),
                     ("garbage", False)]:
        monkeypatch.setenv("JARVIS_PROVIDER_RESPONSE_CACHE_SHADOW", val)
        assert prc.shadow_mode_enabled() is exp, val


def test_shadow_enum_value_exists_and_distinct():
    """SHADOW_HIT_PASSTHROUGH is in the closed taxonomy and is
    distinct from every other outcome."""
    assert hasattr(prc.CacheLookupOutcome, "SHADOW_HIT_PASSTHROUGH")
    s = prc.CacheLookupOutcome.SHADOW_HIT_PASSTHROUGH
    for other in (
        prc.CacheLookupOutcome.EXACT_HIT,
        prc.CacheLookupOutcome.SEMANTIC_HIT,
        prc.CacheLookupOutcome.MISS,
        prc.CacheLookupOutcome.DISABLED,
        prc.CacheLookupOutcome.INVALIDATED_REPO_CHANGE,
        prc.CacheLookupOutcome.FAULT_FAIL_OPEN,
    ):
        assert s is not other


def test_trajectory_carries_original_cost(monkeypatch, tmp_path):
    """``CachedTrajectory.original_cost_usd`` captures
    ``gr.cost_usd`` at store time. Roundtrip preserves it."""
    full, pref = prc.compute_cache_key(
        "P", "m", "ide", Path(tmp_path),
    )
    gr = _gr_with_cost(cost_usd=0.123456)
    t = prc._trajectory_from_generation_result(full, pref, gr)
    assert t is not None
    assert t.original_cost_usd == pytest.approx(0.123456)
    # Roundtrip preserves the field
    t2 = prc.CachedTrajectory.from_dict(t.to_dict())
    assert t2 is not None
    assert t2.original_cost_usd == pytest.approx(0.123456)


def test_legacy_trajectory_dict_defaults_zero():
    """An older serialized entry without ``original_cost_usd`` must
    deserialize cleanly with 0.0 (means 'unknown would-save')."""
    d = {
        "full_key": "k", "prefix_key": "p", "candidates": [],
        "provider_name": "dw", "model_id": "m", "is_noop": False,
        "prompt_preloaded_files": [], "total_input_tokens": 0,
        "total_output_tokens": 0, "n_bytes": 0,
        # original_cost_usd intentionally absent
        "created_at": 0.0,
        "schema_version": prc.PROVIDER_RESPONSE_CACHE_SCHEMA_VERSION,
    }
    t = prc.CachedTrajectory.from_dict(d)
    assert t is not None
    assert t.original_cost_usd == 0.0


def test_shadow_hit_does_not_short_circuit_provider(monkeypatch):
    """Master ON + shadow ON: identical 2nd call must run produce()
    (no cache short-circuit), return the fresh GR, and the outcome
    must be SHADOW_HIT_PASSTHROUGH (not EXACT_HIT)."""
    monkeypatch.setenv("JARVIS_PROVIDER_RESPONSE_CACHE_SHADOW", "true")
    calls = {"n": 0}

    async def produce():
        calls["n"] += 1
        return _gr_with_cost(cost_usd=0.42)

    async def run():
        g1, o1 = await prc.cached_or_generate(
            prompt="P", model="m", route="ide",
            repo_root=Path("."), produce=produce,
        )
        g2, o2 = await prc.cached_or_generate(
            prompt="P", model="m", route="ide",
            repo_root=Path("."), produce=produce,
        )
        return g1, o1, g2, o2

    g1, o1, g2, o2 = asyncio.run(run())
    assert o1 is prc.CacheLookupOutcome.MISS
    # Hit at lookup time, but flow goes through produce — outcome
    # signals "observed but not acted upon".
    assert o2 is prc.CacheLookupOutcome.SHADOW_HIT_PASSTHROUGH
    # produce was called BOTH times (no short-circuit).
    assert calls["n"] == 2
    # Both responses come from produce (fresh provider call), so the
    # 2nd response has the full cost_usd, NOT 0.0.
    assert g1.cost_usd == pytest.approx(0.42)
    assert g2.cost_usd == pytest.approx(0.42)
    # provider_name is NOT tagged "+cache" — upstream was hit.
    assert not g2.provider_name.endswith("+cache")


def test_shadow_passthrough_does_not_overwrite_store(monkeypatch):
    """On SHADOW_HIT_PASSTHROUGH the key is already present; the post-
    produce store path is skipped to avoid LRU re-write thrash."""
    monkeypatch.setenv("JARVIS_PROVIDER_RESPONSE_CACHE_SHADOW", "true")
    call_log = []
    real_store = prc.ProviderResponseCache.store

    def counting_store(self, t):
        call_log.append(t.full_key if t is not None else None)
        return real_store(self, t)

    monkeypatch.setattr(prc.ProviderResponseCache, "store", counting_store)

    async def produce():
        return _gr_with_cost(cost_usd=0.42)

    async def run():
        await prc.cached_or_generate(
            prompt="P", model="m", route="ide",
            repo_root=Path("."), produce=produce,
        )
        return await prc.cached_or_generate(
            prompt="P", model="m", route="ide",
            repo_root=Path("."), produce=produce,
        )

    g2, o2 = asyncio.run(run())
    assert o2 is prc.CacheLookupOutcome.SHADOW_HIT_PASSTHROUGH
    # Exactly ONE store call — the cold MISS path. The shadow-HIT
    # call#2 must NOT trigger a second store.
    assert len(call_log) == 1


def test_shadow_off_master_on_normal_hit():
    """Sanity: with shadow OFF (default), master-ON behavior is the
    canonical EXACT_HIT path — no shadow code interferes."""
    calls = {"n": 0}

    async def produce():
        calls["n"] += 1
        return _gr_with_cost(cost_usd=0.42)

    async def run():
        await prc.cached_or_generate(
            prompt="P", model="m", route="ide",
            repo_root=Path("."), produce=produce,
        )
        return await prc.cached_or_generate(
            prompt="P", model="m", route="ide",
            repo_root=Path("."), produce=produce,
        )

    g2, o2 = asyncio.run(run())
    assert o2 is prc.CacheLookupOutcome.EXACT_HIT
    assert calls["n"] == 1                 # produce called once
    assert g2.cost_usd == 0.0
    assert g2.provider_name.endswith("+cache")


def test_shadow_on_master_off_is_disabled(monkeypatch):
    """Master takes precedence: shadow-mode is meaningless without
    master ON. When master OFF, outcome is DISABLED regardless of
    shadow."""
    monkeypatch.setenv("JARVIS_PROVIDER_RESPONSE_CACHE_ENABLED", "false")
    monkeypatch.setenv("JARVIS_PROVIDER_RESPONSE_CACHE_SHADOW", "true")

    async def produce():
        return _gr_with_cost(cost_usd=0.42)

    g, o = asyncio.run(prc.cached_or_generate(
        prompt="P", model="m", route="ide",
        repo_root=Path("."), produce=produce,
    ))
    assert o is prc.CacheLookupOutcome.DISABLED
    assert g.cost_usd == pytest.approx(0.42)


def test_shadow_hit_logs_cost_would_have_saved(caplog, monkeypatch):
    """The `[PRC] SHADOW_HIT cost_would_have_saved=$X` log line is
    emitted on the 2nd identical call with shadow ON, and X equals
    the stored ``original_cost_usd`` from call #1."""
    import logging as _logging
    monkeypatch.setenv("JARVIS_PROVIDER_RESPONSE_CACHE_SHADOW", "true")

    async def produce():
        return _gr_with_cost(cost_usd=0.071234)

    with caplog.at_level(_logging.INFO,
                         logger="Ouroboros.ProviderResponseCache"):
        async def run():
            await prc.cached_or_generate(
                prompt="P", model="m", route="ide",
                repo_root=Path("."), produce=produce,
            )
            return await prc.cached_or_generate(
                prompt="P", model="m", route="ide",
                repo_root=Path("."), produce=produce,
            )

        asyncio.run(run())

    shadow_lines = [
        r.getMessage() for r in caplog.records
        if "SHADOW_HIT" in r.getMessage()
    ]
    assert len(shadow_lines) == 1, shadow_lines
    # Cost rendered with 6-decimal precision (PRD §10.10 format).
    assert "cost_would_have_saved=$0.071234" in shadow_lines[0]
    assert "model=m" in shadow_lines[0]
    assert "route=ide" in shadow_lines[0]


def test_shadow_negative_cost_clamped_to_zero(monkeypatch):
    """Defensive: a (hypothetical) negative provider cost is clamped
    to 0.0 at store time so the would-save log is never misleading."""
    full, pref = prc.compute_cache_key(
        "P", "m", "ide", Path("."),
    )
    bad = _gr_with_cost(cost_usd=-1.0)
    t = prc._trajectory_from_generation_result(full, pref, bad)
    assert t is not None
    assert t.original_cost_usd == 0.0
