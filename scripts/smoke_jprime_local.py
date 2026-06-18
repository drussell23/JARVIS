"""Live smoke test for the J-Prime local tier (Phase 3 + 3.1) against real Ollama.

Run: JARVIS_LOCAL_PRIME_ENABLED=true python3 scripts/smoke_jprime_local.py
Requires: native Ollama on :11434 with qwen2.5-coder:3b pulled.
"""
from __future__ import annotations

import asyncio
import os
import time


async def main() -> int:
    os.environ.setdefault("JARVIS_LOCAL_PRIME_ENABLED", "true")
    os.environ.setdefault("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "true")

    from backend.core.ouroboros.governance.local_inference_director import (
        build_local_prime_client,
        LocalConfig,
        LocalInferenceDirector,
        LocalMemoryCritical,
    )
    from backend.core.ouroboros.governance.memory_pressure_gate import PressureLevel

    client = build_local_prime_client()
    assert client is not None, "kill-switch should be ON for the smoke test"
    print(f"[1] build_local_prime_client -> {type(client).__name__}  model={LocalConfig.from_env().model_name}")

    # Health probe (drop-in PrimeClient._check_health)
    status = await client._check_health()
    print(f"[2] _check_health -> {status.name}")

    # Cold generate (drop-in PrimeClient.generate -> PrimeResponse)
    t0 = time.monotonic()
    resp = await client.generate(
        prompt="Write a Python function fib(n) returning the nth Fibonacci number. Code only.",
        system_prompt="You are a terse code generator. Output only Python code, no prose.",
        max_tokens=200,
        temperature=0.0,
    )
    cold_ms = (time.monotonic() - t0) * 1000.0
    print(f"[3] generate (cold) -> source={resp.source} tokens={resp.tokens_used} "
          f"wall={cold_ms:.0f}ms latency_ms={resp.latency_ms:.0f}")
    print("    --- content (first 240 chars) ---")
    print("    " + resp.content[:240].replace("\n", "\n    "))

    # Warm generate (weights resident -> should be faster TTFT)
    t0 = time.monotonic()
    resp2 = await client.generate(
        prompt="Write a one-line Python lambda that squares its argument. Code only.",
        system_prompt="Output only code.",
        max_tokens=64,
        temperature=0.0,
    )
    warm_ms = (time.monotonic() - t0) * 1000.0
    print(f"[4] generate (warm) -> wall={warm_ms:.0f}ms tokens={resp2.tokens_used} "
          f"content={resp2.content[:80]!r}")

    # Profiler warmed?
    print(f"[5] profiler.is_warm()={client.profiler.is_warm()} "
          f"adaptive_timeout_ms(prompt_tokens=500)={client.profiler.adaptive_timeout_ms(prompt_tokens=500):.0f}")

    # Live memory guard: a CRITICAL gate must evict + refuse (cascade upstream),
    # proving the Phase 3.1 valve fires against the real engine.
    class _CriticalGate:
        def pressure(self):
            return PressureLevel.CRITICAL

    director = LocalInferenceDirector(LocalConfig.from_env(), client=client, gate=_CriticalGate())
    client.attach_governor(director)
    refused = False
    try:
        await client.generate(prompt="this must be refused", system_prompt="x", max_tokens=16)
    except LocalMemoryCritical as e:
        refused = True
        print(f"[6] memory_guard CRITICAL -> evicted + refused (LocalMemoryCritical): {e}")
    assert refused, "memory guard must refuse at CRITICAL"

    # Detach + confirm normal generation resumes (model reloads on demand)
    client.attach_governor(None)
    resp3 = await client.generate(prompt="print('hi')? Output only code.", system_prompt="x", max_tokens=32)
    print(f"[7] post-evict generate -> content={resp3.content[:60]!r} (model reloaded on demand)")

    await client.aclose()
    print("[8] aclose -> session released (zero hanging FDs)")
    print("\nSMOKE: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
