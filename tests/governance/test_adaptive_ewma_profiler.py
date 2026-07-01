"""Adaptive EWMA Profiler -- cure the EWMA starvation (chicken-and-egg).

The 32B multi-round generation exceeded the 150s cold seed, timed out, recorded
NO sample (record fires only on success), so the persistent profiler stayed cold
forever. Three structural cures:

  1. Context-Aware Dynamic Seed: the cold seed is derived from
     JARVIS_JPRIME_HEAVY_COLDSTART_MULT * (num_ctx / baseline) -- a 16k window
     inherently needs a bigger first budget than 8k. No arbitrary base bump.
  2. Asymmetric EWMA Escalation: a TimeoutException injects a PENALTY sample
     (timeout * escalation_factor) that jumps the EWMA UP, so the next dispatch
     aggressively expands the window and rapidly finds the true ceiling.
  3. Absolute Global Circuit Breaker: if the EWMA breaches an absolute ceiling
     (default 20min), adaptive_timeout raises UnrecoverableInferenceLatency to
     kill the loop -- no infinite inflation / endless billing.
"""
from __future__ import annotations

import dataclasses

import pytest

import backend.core.ouroboros.governance.local_inference_director as lid


def _cfg(**over):
    return dataclasses.replace(lid.LocalConfig.from_env(), **over)


# --- 1. Context-Aware Dynamic Seed ------------------------------------------

def test_dynamic_seed_scales_with_ctx_and_heavy(monkeypatch):
    monkeypatch.setenv("JARVIS_JPRIME_HEAVY_COLDSTART_MULT", "4.0")
    monkeypatch.setenv("JARVIS_LOCAL_SEED_CTX_BASELINE", "8192")
    base = _cfg().timeout_seed_ms
    prof = lid.LatencyProfiler(_cfg(num_ctx=16640))
    seed = prof._cold_seed_ms()
    # base * 4 * (16640/8192 ~= 2.03) -> ~8x base, and strictly > base.
    assert seed > base * 4                # heavy + ctx both applied
    assert seed < lid._absolute_ceiling_ms()  # never above the breaker


def test_dynamic_seed_survival_path_byte_identical(monkeypatch):
    # No num_ctx (survival/CPU) -> plain base seed, no heavy/ctx scaling.
    prof = lid.LatencyProfiler(_cfg(num_ctx=None))
    assert prof._cold_seed_ms() == float(_cfg().timeout_seed_ms)


def test_dynamic_seed_capped_below_absolute():
    prof = lid.LatencyProfiler(_cfg(num_ctx=10_000_000))  # absurd ctx
    assert prof._cold_seed_ms() <= lid._absolute_ceiling_ms() * 0.5 + 1


# --- 2. Asymmetric EWMA Escalation (penalty injection) ----------------------

def test_timeout_penalty_escalates_next_budget(monkeypatch):
    monkeypatch.setenv("JARVIS_LOCAL_TIMEOUT_ESCALATION_FACTOR", "1.5")
    prof = lid.LatencyProfiler(_cfg(num_ctx=8192))
    prof.record_timeout_penalty(150_000)
    nxt = prof.adaptive_timeout_ms(prompt_tokens=100)
    assert nxt >= 150_000 * 1.5   # the next dispatch gets an escalated budget


def test_penalty_escalates_geometrically(monkeypatch):
    monkeypatch.setenv("JARVIS_LOCAL_TIMEOUT_ESCALATION_FACTOR", "1.5")
    prof = lid.LatencyProfiler(_cfg(num_ctx=8192))
    prof.record_timeout_penalty(150_000)
    a = prof.adaptive_timeout_ms(prompt_tokens=100)
    prof.record_timeout_penalty(a)   # timed out again at the escalated budget
    b = prof.adaptive_timeout_ms(prompt_tokens=100)
    assert b > a                     # rapidly climbs toward the true ceiling


def test_success_blends_ewma_down(monkeypatch):
    monkeypatch.setenv("JARVIS_LOCAL_EWMA_ALPHA", "0.5")
    prof = lid.LatencyProfiler(_cfg(num_ctx=8192))
    prof.record_timeout_penalty(400_000)          # ewma jumps to 600k
    high = prof.adaptive_timeout_ms(prompt_tokens=100)
    for _ in range(_cfg().min_samples + 3):        # real fast successes
        prof.record(ttft_ms=1_000.0, total_ms=120_000.0, output_tokens=300)
    low = prof.adaptive_timeout_ms(prompt_tokens=100)
    assert low < high                              # decays toward real latency


# --- 3. Absolute Global Circuit Breaker -------------------------------------

def test_absolute_breaker_raises(monkeypatch):
    monkeypatch.setenv("JARVIS_LOCAL_INFERENCE_ABSOLUTE_CEILING_MS", "1200000")
    prof = lid.LatencyProfiler(_cfg(num_ctx=8192))
    prof.record_timeout_penalty(1_000_000)   # penalty -> ewma = 1.5M >= 1.2M absolute
    with pytest.raises(lid.UnrecoverableInferenceLatency):
        prof.adaptive_timeout_ms(prompt_tokens=100)


def test_below_absolute_does_not_raise(monkeypatch):
    monkeypatch.setenv("JARVIS_LOCAL_INFERENCE_ABSOLUTE_CEILING_MS", "1200000")
    prof = lid.LatencyProfiler(_cfg(num_ctx=8192))
    prof.record_timeout_penalty(300_000)     # ewma 450k < 1.2M
    assert prof.adaptive_timeout_ms(prompt_tokens=100) < 1_200_000


def test_unrecoverable_is_not_l7_recoverable():
    import backend.core.ouroboros.governance.candidate_generator as cg
    exc = lid.UnrecoverableInferenceLatency("boom")
    assert cg._is_l7_recoverable(exc) is False   # halts the op, no infinite retry


# --- integration: complete_guarded injects the penalty on timeout -----------

def test_complete_guarded_injects_penalty_on_timeout():
    # The total-duration timeout + penalty lives on the NON-streaming (survival)
    # path now; the heavy num_ctx path streams (inter-token watchdog, no total cap).
    import asyncio

    prof = lid.LatencyProfiler(_cfg(num_ctx=None))
    client = lid.LocalPrimeClient(_cfg(num_ctx=None), session=object(), profiler=prof)

    async def _timeout_complete(**kw):
        raise asyncio.TimeoutError()

    client.complete = _timeout_complete  # type: ignore[assignment]

    before = prof._ewma_ms
    with pytest.raises(lid.LocalLatencyLockup):
        asyncio.run(client.complete_guarded(system="s", user="u", prompt_tokens=10))
    assert prof._ewma_ms > before   # a penalty sample was injected on the timeout
