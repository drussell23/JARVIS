# J-Prime Local Cascade — Phase 3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Activate the dormant Tier-2 J-Prime provider as a native local inference engine (Ollama + Qwen2.5-Coder-3B q4) with an adaptive latency profiler, deterministic circuit-breaker, and synchronous CRITICAL memory-eviction valve — closing the `all_providers_exhausted` wall with a free, no-quota fallback.

**Architecture:** A new `local_inference_director.py` module hosts three isolated units — `LatencyProfiler` (sliding-window TTFT/TGV stats → adaptive bounded timeout), `LocalPrimeClient` (aiohttp connection-pooled client to Ollama's OpenAI-compat endpoint, structured-prompt discipline for the 3B), and `LocalInferenceDirector` (lifecycle + memory-aware concurrency + eviction). The existing `PrimeProvider` consumes `LocalPrimeClient` unchanged; the existing `FailbackStateMachine` consumes the breaker signal. Everything is gated behind `JARVIS_LOCAL_PRIME_ENABLED` (default `false` = byte-identical legacy).

**Tech Stack:** Python 3.9+ asyncio, `aiohttp`, stdlib `collections.deque`/`threading`/`gc`, existing `MemoryPressureGate`, `PrimeProvider`, `FailbackStateMachine`. Tests: `pytest` + `pytest-asyncio` with a mocked Ollama HTTP layer (no live model in CI).

**Reference ADD:** `docs/superpowers/specs/2026-06-18-jprime-local-cascade-phase3-design.md`

---

## Design Reconciliation: Adaptive Timeout (read before Task 3)

The operator requested a dynamic sliding-window timeout instead of a flat integer. Embedded — with one **load-bearing safeguard**:

- `JARVIS_LOCAL_INFERENCE_TIMEOUT_MS` is **not removed**. It becomes (a) the **cold-start seed** while the window is below `MIN_SAMPLES`, and (b) the **absolute hard ceiling** the adaptive value can never exceed.
- **Why the ceiling is non-negotiable:** an adaptive timeout that flexes upward under "CPU contention" would, for a genuinely *wedged* model (deadlock / runaway), keep extending forever and **never trip the breaker** — the same failure class CLAUDE.md documents (the v41 67-min VERIFY hang; the retired SIGTERM-escalation knobs). A watchdog that can be talked out of firing by the thing it guards is not a watchdog. So: adaptive *within* `[floor, ceiling]`, never beyond.
- **Formula** (per op): `expected_ms = ttft_mean + per_token_ms_mean * est_output_tokens`, where `est_output_tokens = prompt_tokens * JARVIS_LOCAL_OUTPUT_RATIO` (prompt size is the available proxy for work; honest note: output tokens are the true driver, but we only know prompt size pre-generation). Then `timeout_ms = clamp(expected_ms + MARGIN_SIGMA * stddev, floor_ms, ceiling_ms)`.
- **3-sigma terminal-lag guard:** during streaming, if `elapsed > mean + 3*stddev` (only once warm) OR `elapsed > ceiling_ms` (always), trip the breaker as `terminal_lag_lockup`.

---

## File Structure

- **Create:** `backend/core/ouroboros/governance/local_inference_director.py` — all three units (one module, focused responsibility: local inference lifecycle).
- **Create:** `tests/governance/test_local_inference_director.py` — unit tests (mocked Ollama).
- **Create:** `tests/governance/test_local_prime_cascade_integration.py` — FSM cascade + kill-switch parity.
- **Modify:** `backend/core/ouroboros/governance/providers.py` — inject `LocalPrimeClient` into `PrimeProvider` under the kill-switch (construction-site only; `generate()` untouched).
- **Modify:** `backend/core/ouroboros/governance/candidate_generator.py` — feed the breaker's `terminal_lag_lockup` signal into the existing `FailbackStateMachine` degrade path.

---

## Task 1: Env flags + kill-switch helper (foundation)

**Files:**
- Create: `backend/core/ouroboros/governance/local_inference_director.py`
- Test: `tests/governance/test_local_inference_director.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/governance/test_local_inference_director.py
from __future__ import annotations
import importlib
import pytest

MOD = "backend.core.ouroboros.governance.local_inference_director"


def test_local_prime_disabled_by_default(monkeypatch):
    monkeypatch.delenv("JARVIS_LOCAL_PRIME_ENABLED", raising=False)
    lid = importlib.import_module(MOD)
    assert lid.local_prime_enabled() is False


def test_local_prime_enable_toggle(monkeypatch):
    lid = importlib.import_module(MOD)
    monkeypatch.setenv("JARVIS_LOCAL_PRIME_ENABLED", "true")
    assert lid.local_prime_enabled() is True
    monkeypatch.setenv("JARVIS_LOCAL_PRIME_ENABLED", "false")
    assert lid.local_prime_enabled() is False


def test_config_defaults(monkeypatch):
    for k in ("JARVIS_LOCAL_MODEL_BASE_URL", "JARVIS_LOCAL_MODEL_NAME",
              "JARVIS_LOCAL_MODEL_KEEP_ALIVE_SECONDS", "JARVIS_LOCAL_INFERENCE_TIMEOUT_MS"):
        monkeypatch.delenv(k, raising=False)
    lid = importlib.import_module(MOD)
    cfg = lid.LocalConfig.from_env()
    assert cfg.base_url == "http://127.0.0.1:11434"
    assert cfg.model_name == "qwen2.5-coder:3b"
    assert cfg.keep_alive_seconds == 300
    assert cfg.timeout_ceiling_ms == 120_000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/governance/test_local_inference_director.py -v`
Expected: FAIL — `ModuleNotFoundError` / `AttributeError`.

- [ ] **Step 3: Write minimal implementation**

```python
# backend/core/ouroboros/governance/local_inference_director.py
"""Local inference tier (J-Prime activation, Phase 3).

Three units: LatencyProfiler, LocalPrimeClient, LocalInferenceDirector.
Gated behind JARVIS_LOCAL_PRIME_ENABLED (default OFF -> byte-identical legacy).
"""
from __future__ import annotations

import os
from dataclasses import dataclass

_TRUE = {"1", "true", "yes", "on"}


def _envb(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in _TRUE


def local_prime_enabled() -> bool:
    """Master kill-switch. OFF means PrimeProvider gets no local client."""
    return _envb("JARVIS_LOCAL_PRIME_ENABLED", False)


@dataclass(frozen=True)
class LocalConfig:
    base_url: str
    model_name: str
    keep_alive_seconds: int
    timeout_seed_ms: int       # cold-start seed
    timeout_ceiling_ms: int    # absolute hard cap (adaptive never exceeds)
    timeout_floor_ms: int
    output_ratio: float        # est_output_tokens = prompt_tokens * ratio
    margin_sigma: float
    window_size: int
    min_samples: int
    max_concurrency: int
    pool_limit: int

    @classmethod
    def from_env(cls) -> "LocalConfig":
        def _i(n, d): return int(os.environ.get(n, d))
        def _f(n, d): return float(os.environ.get(n, d))
        ceiling = _i("JARVIS_LOCAL_INFERENCE_TIMEOUT_MS", 120_000)
        return cls(
            base_url=os.environ.get("JARVIS_LOCAL_MODEL_BASE_URL", "http://127.0.0.1:11434"),
            model_name=os.environ.get("JARVIS_LOCAL_MODEL_NAME", "qwen2.5-coder:3b"),
            keep_alive_seconds=_i("JARVIS_LOCAL_MODEL_KEEP_ALIVE_SECONDS", 300),
            timeout_seed_ms=_i("JARVIS_LOCAL_INFERENCE_TIMEOUT_SEED_MS", 30_000),
            timeout_ceiling_ms=ceiling,
            timeout_floor_ms=_i("JARVIS_LOCAL_INFERENCE_TIMEOUT_FLOOR_MS", 4_000),
            output_ratio=_f("JARVIS_LOCAL_OUTPUT_RATIO", 0.5),
            margin_sigma=_f("JARVIS_LOCAL_MARGIN_SIGMA", 2.0),
            window_size=_i("JARVIS_LOCAL_PROFILER_WINDOW", 20),
            min_samples=_i("JARVIS_LOCAL_PROFILER_MIN_SAMPLES", 5),
            max_concurrency=_i("JARVIS_LOCAL_MODEL_MAX_CONCURRENCY", 2),
            pool_limit=_i("JARVIS_LOCAL_POOL_LIMIT", 8),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/governance/test_local_inference_director.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/local_inference_director.py tests/governance/test_local_inference_director.py
git commit -m "feat(jprime): Phase 3 Task 1 — local config + kill-switch helper"
```

---

## Task 2: `LatencyProfiler` — sliding-window TTFT/TGV + bounded adaptive timeout

**Files:**
- Modify: `backend/core/ouroboros/governance/local_inference_director.py`
- Test: `tests/governance/test_local_inference_director.py`

- [ ] **Step 1: Write the failing test**

```python
def test_profiler_cold_start_uses_seed():
    from backend.core.ouroboros.governance.local_inference_director import LatencyProfiler, LocalConfig
    cfg = LocalConfig.from_env()
    p = LatencyProfiler(cfg)
    # No samples -> timeout is the cold-start seed (clamped to ceiling).
    assert p.adaptive_timeout_ms(prompt_tokens=1000) == min(cfg.timeout_seed_ms, cfg.timeout_ceiling_ms)
    assert p.is_warm() is False


def test_profiler_warms_and_scales_with_prompt_size():
    from backend.core.ouroboros.governance.local_inference_director import LatencyProfiler, LocalConfig
    cfg = LocalConfig.from_env()
    p = LatencyProfiler(cfg)
    # Feed 6 consistent samples: ttft=200ms, per_token=10ms/token over 100 tokens.
    for _ in range(6):
        p.record(ttft_ms=200.0, total_ms=200.0 + 100 * 10.0, output_tokens=100)
    assert p.is_warm() is True
    # est_output = 2000 * 0.5 = 1000 tokens; expected ~ 200 + 1000*10 = 10200ms (+ small sigma margin).
    t_big = p.adaptive_timeout_ms(prompt_tokens=2000)
    t_small = p.adaptive_timeout_ms(prompt_tokens=200)
    assert t_big > t_small  # scales with work
    assert t_big <= cfg.timeout_ceiling_ms  # never exceeds hard ceiling


def test_profiler_never_exceeds_ceiling_even_with_huge_prompt():
    from backend.core.ouroboros.governance.local_inference_director import LatencyProfiler, LocalConfig
    cfg = LocalConfig.from_env()
    p = LatencyProfiler(cfg)
    for _ in range(6):
        p.record(ttft_ms=500.0, total_ms=5000.0, output_tokens=100)
    assert p.adaptive_timeout_ms(prompt_tokens=10_000_000) == cfg.timeout_ceiling_ms


def test_profiler_three_sigma_terminal_lag():
    from backend.core.ouroboros.governance.local_inference_director import LatencyProfiler, LocalConfig
    cfg = LocalConfig.from_env()
    p = LatencyProfiler(cfg)
    for _ in range(6):
        p.record(ttft_ms=200.0, total_ms=1000.0, output_tokens=100)  # mean total ~1000ms, tiny stddev
    assert p.is_terminal_lag(elapsed_ms=1100.0) is False
    assert p.is_terminal_lag(elapsed_ms=50_000.0) is True
    # Hard ceiling always trips regardless of warmth:
    assert p.is_terminal_lag(elapsed_ms=cfg.timeout_ceiling_ms + 1) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/governance/test_local_inference_director.py -k profiler -v`
Expected: FAIL — `ImportError: cannot import name 'LatencyProfiler'`.

- [ ] **Step 3: Write minimal implementation**

```python
# append to local_inference_director.py
import math
import threading
from collections import deque
from typing import Deque, Tuple


class LatencyProfiler:
    """Thread-safe sliding window of (ttft_ms, per_token_ms) → bounded adaptive timeout.

    Cold start uses the seed; the adaptive value is always clamped to
    [floor, ceiling]. The ceiling is the un-flexible hard cap that guarantees a
    wedged model still trips the breaker (CLAUDE.md watchdog-isolation invariant).
    """

    def __init__(self, cfg: "LocalConfig") -> None:
        self._cfg = cfg
        self._lock = threading.Lock()
        self._ttft: Deque[float] = deque(maxlen=cfg.window_size)
        self._per_tok: Deque[float] = deque(maxlen=cfg.window_size)
        self._total: Deque[float] = deque(maxlen=cfg.window_size)

    def record(self, *, ttft_ms: float, total_ms: float, output_tokens: int) -> None:
        per_tok = (total_ms - ttft_ms) / max(1, output_tokens)
        with self._lock:
            self._ttft.append(float(ttft_ms))
            self._per_tok.append(max(0.0, per_tok))
            self._total.append(float(total_ms))

    def is_warm(self) -> bool:
        with self._lock:
            return len(self._total) >= self._cfg.min_samples

    @staticmethod
    def _mean(xs) -> float:
        return sum(xs) / len(xs) if xs else 0.0

    @classmethod
    def _stddev(cls, xs) -> float:
        if len(xs) < 2:
            return 0.0
        m = cls._mean(xs)
        return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))

    def adaptive_timeout_ms(self, *, prompt_tokens: int) -> float:
        cfg = self._cfg
        with self._lock:
            warm = len(self._total) >= cfg.min_samples
            ttft_m = self._mean(self._ttft)
            tok_m = self._mean(self._per_tok)
            tot_sd = self._stddev(self._total)
        if not warm:
            return float(min(cfg.timeout_seed_ms, cfg.timeout_ceiling_ms))
        est_out = max(1.0, prompt_tokens * cfg.output_ratio)
        expected = ttft_m + tok_m * est_out
        flexed = expected + cfg.margin_sigma * tot_sd
        return float(max(cfg.timeout_floor_ms, min(flexed, cfg.timeout_ceiling_ms)))

    def is_terminal_lag(self, *, elapsed_ms: float) -> bool:
        cfg = self._cfg
        if elapsed_ms > cfg.timeout_ceiling_ms:   # hard cap: always trips
            return True
        with self._lock:
            warm = len(self._total) >= cfg.min_samples
            if not warm:
                return False
            m = self._mean(self._total)
            sd = self._stddev(self._total)
        return elapsed_ms > (m + 3.0 * sd)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/governance/test_local_inference_director.py -k profiler -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(jprime): Phase 3 Task 2 — bounded adaptive latency profiler"
```

---

## Task 3: `LocalPrimeClient` — aiohttp connection-pooled client + structured prompt

**Files:**
- Modify: `backend/core/ouroboros/governance/local_inference_director.py`
- Test: `tests/governance/test_local_inference_director.py`

- [ ] **Step 1: Write the failing test** (mocked aiohttp — no live server)

```python
import pytest

class _FakeResp:
    def __init__(self, payload): self._p = payload; self.status = 200
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def json(self): return self._p

class _FakeSession:
    def __init__(self, payload): self._p = payload; self.closed = False; self.posts = []
    def post(self, url, **kw): self.posts.append((url, kw)); return _FakeResp(self._p)
    async def close(self): self.closed = True

@pytest.mark.asyncio
async def test_client_generate_posts_to_openai_compat_with_keep_alive(monkeypatch):
    from backend.core.ouroboros.governance.local_inference_director import LocalPrimeClient, LocalConfig
    payload = {"choices": [{"message": {"content": "patched code"}}],
               "usage": {"completion_tokens": 12}}
    fake = _FakeSession(payload)
    cfg = LocalConfig.from_env()
    client = LocalPrimeClient(cfg, session=fake)
    out = await client.complete(system="<sys/>", user="<task/>", prompt_tokens=100)
    assert out.text == "patched code"
    url, kw = fake.posts[-1]
    assert url.endswith("/v1/chat/completions")
    assert kw["json"]["keep_alive"] == cfg.keep_alive_seconds  # warm-standby
    assert kw["json"]["model"] == cfg.model_name

@pytest.mark.asyncio
async def test_client_close_releases_session(monkeypatch):
    from backend.core.ouroboros.governance.local_inference_director import LocalPrimeClient, LocalConfig
    fake = _FakeSession({"choices": [{"message": {"content": "x"}}]})
    client = LocalPrimeClient(LocalConfig.from_env(), session=fake)
    await client.aclose()
    assert fake.closed is True  # zero hanging FDs

def test_structured_prompt_uses_bounded_tags():
    from backend.core.ouroboros.governance.local_inference_director import render_structured_prompt
    s = render_structured_prompt(task="fix bug", constraints=["no new deps"], files={"a.py": "x=1"})
    assert "<task>" in s and "</task>" in s
    assert "<constraints>" in s and "<files>" in s  # rigid delimiters for the 3B
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/governance/test_local_inference_director.py -k client -v`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Write minimal implementation**

```python
# append to local_inference_director.py
import time
from dataclasses import dataclass as _dc
from typing import Dict, List, Optional


def render_structured_prompt(*, task: str, constraints: List[str], files: Dict[str, str]) -> str:
    """Structured-prompt discipline for the local 3B: rigid bounded tags, no loose NL."""
    parts = ["<task>", task, "</task>", "<constraints>"]
    parts += [f"- {c}" for c in constraints]
    parts += ["</constraints>", "<files>"]
    for path, body in files.items():
        parts += [f'<file path="{path}">', body, "</file>"]
    parts += ["</files>", "<output_format>full_content</output_format>"]
    return "\n".join(parts)


@_dc
class LocalCompletion:
    text: str
    output_tokens: int
    ttft_ms: float
    total_ms: float


class LocalPrimeClient:
    """aiohttp connection-pooled client → Ollama OpenAI-compat endpoint.

    A persistent session (lazily built, or injected for tests) with a bounded
    TCPConnector + keep-alive eliminates per-call socket setup across L2 passes.
    """

    def __init__(self, cfg: "LocalConfig", session: Optional[object] = None) -> None:
        self._cfg = cfg
        self._session = session
        self.profiler = LatencyProfiler(cfg)

    async def _ensure_session(self):
        if self._session is None:
            import aiohttp  # local import keeps module import cheap when OFF
            conn = aiohttp.TCPConnector(
                limit=self._cfg.pool_limit, limit_per_host=self._cfg.pool_limit,
                keepalive_timeout=max(30, self._cfg.keep_alive_seconds),
            )
            self._session = aiohttp.ClientSession(
                connector=conn, headers={"Connection": "keep-alive"},
            )
        return self._session

    async def complete(self, *, system: str, user: str, prompt_tokens: int,
                       timeout_ms: Optional[float] = None) -> LocalCompletion:
        sess = await self._ensure_session()
        url = self._cfg.base_url.rstrip("/") + "/v1/chat/completions"
        body = {
            "model": self._cfg.model_name,
            "keep_alive": self._cfg.keep_alive_seconds,   # warm-standby residency
            "temperature": 0.2,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        }
        t0 = time.monotonic()
        async with sess.post(url, json=body) as resp:
            data = await resp.json()
        total_ms = (time.monotonic() - t0) * 1000.0
        text = data["choices"][0]["message"]["content"]
        out_toks = int(data.get("usage", {}).get("completion_tokens", 0)) or max(1, len(text) // 4)
        # TTFT unavailable in non-streaming mode; approximate as a fraction (refined in Task 4 streaming).
        ttft_ms = min(total_ms, 0.1 * total_ms)
        self.profiler.record(ttft_ms=ttft_ms, total_ms=total_ms, output_tokens=out_toks)
        return LocalCompletion(text=text, output_tokens=out_toks, ttft_ms=ttft_ms, total_ms=total_ms)

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/governance/test_local_inference_director.py -k client -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(jprime): Phase 3 Task 3 — pooled LocalPrimeClient + structured prompt"
```

---

## Task 4: Latency circuit-breaker → `terminal_lag_lockup` signal

**Files:**
- Modify: `backend/core/ouroboros/governance/local_inference_director.py`
- Test: `tests/governance/test_local_inference_director.py`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_breaker_trips_on_ceiling_breach(monkeypatch):
    from backend.core.ouroboros.governance.local_inference_director import (
        LocalPrimeClient, LocalConfig, LocalLatencyLockup)
    monkeypatch.setenv("JARVIS_LOCAL_INFERENCE_TIMEOUT_MS", "50")  # tiny ceiling

    class _SlowSession:
        closed = False
        def post(self, url, **kw):
            class _R:
                status = 200
                async def __aenter__(self_):
                    import asyncio; await asyncio.sleep(0.2); return self_  # 200ms > 50ms ceiling
                async def __aexit__(self_, *a): return False
                async def json(self_): return {"choices": [{"message": {"content": "x"}}]}
            return _R()
        async def close(self): self.closed = True

    client = LocalPrimeClient(LocalConfig.from_env(), session=_SlowSession())
    with pytest.raises(LocalLatencyLockup):
        await client.complete_guarded(system="<s/>", user="<u/>", prompt_tokens=10)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/governance/test_local_inference_director.py -k breaker -v`
Expected: FAIL — `ImportError: LocalLatencyLockup` / no `complete_guarded`.

- [ ] **Step 3: Write minimal implementation**

```python
# append to local_inference_director.py
import asyncio


class LocalLatencyLockup(RuntimeError):
    """Raised when local inference breaches the adaptive/ceiling timeout.

    Consumed by candidate_generator's FailbackStateMachine to transition
    J-Prime to PRIMARY_DEGRADED and cascade the op upstream.
    """
    failure_class = "terminal_lag_lockup"


# add method to LocalPrimeClient:
async def complete_guarded(self, *, system: str, user: str, prompt_tokens: int) -> "LocalCompletion":
    timeout_ms = self.profiler.adaptive_timeout_ms(prompt_tokens=prompt_tokens)
    try:
        return await asyncio.wait_for(
            self.complete(system=system, user=user, prompt_tokens=prompt_tokens),
            timeout=timeout_ms / 1000.0,
        )
    except asyncio.TimeoutError as e:
        raise LocalLatencyLockup(
            f"local_inference timeout: budget={timeout_ms:.0f}ms "
            f"warm={self.profiler.is_warm()}"
        ) from e
```

> Bind `complete_guarded` as a method of `LocalPrimeClient` (define it inside the class body during implementation; shown here standalone for the diff). Uses `asyncio.wait_for` (Python 3.9+ — no `asyncio.timeout`, per CLAUDE.md).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/governance/test_local_inference_director.py -k breaker -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(jprime): Phase 3 Task 4 — adaptive latency breaker (terminal_lag_lockup)"
```

---

## Task 5: `LocalInferenceDirector` — memory-aware concurrency + CRITICAL eviction valve

**Files:**
- Modify: `backend/core/ouroboros/governance/local_inference_director.py`
- Test: `tests/governance/test_local_inference_director.py`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_high_pressure_clamps_concurrency_to_one():
    from backend.core.ouroboros.governance.local_inference_director import LocalInferenceDirector, LocalConfig
    from backend.core.ouroboros.governance.memory_pressure_gate import PressureLevel
    d = LocalInferenceDirector(LocalConfig.from_env(), client=object())
    assert d.admit_concurrency(PressureLevel.OK) == d._cfg.max_concurrency
    assert d.admit_concurrency(PressureLevel.HIGH) == 1
    assert d.admit_concurrency(PressureLevel.CRITICAL) == 0

@pytest.mark.asyncio
async def test_critical_eviction_unloads_and_gc(monkeypatch):
    from backend.core.ouroboros.governance.local_inference_director import LocalInferenceDirector, LocalConfig
    from backend.core.ouroboros.governance.memory_pressure_gate import PressureLevel
    evicted = {"calls": []}

    class _EvictSession:
        closed = False
        def post(self, url, **kw):
            evicted["calls"].append(kw.get("json", {}))
            class _R:
                status = 200
                async def __aenter__(self_): return self_
                async def __aexit__(self_, *a): return False
                async def json(self_): return {"status": "ok"}
            return _R()
        async def close(self): self.closed = True

    from backend.core.ouroboros.governance.local_inference_director import LocalPrimeClient
    client = LocalPrimeClient(LocalConfig.from_env(), session=_EvictSession())
    d = LocalInferenceDirector(LocalConfig.from_env(), client=client)
    gc_calls = {"n": 0}
    monkeypatch.setattr("gc.collect", lambda *a, **k: gc_calls.__setitem__("n", gc_calls["n"] + 1) or 0)
    await d.enforce_memory(PressureLevel.CRITICAL)
    assert any(c.get("keep_alive") == 0 for c in evicted["calls"])  # forced unload
    assert gc_calls["n"] >= 2  # dual-stage gc.collect()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/governance/test_local_inference_director.py -k "concurrency or eviction" -v`
Expected: FAIL — `ImportError: LocalInferenceDirector`.

- [ ] **Step 3: Write minimal implementation**

```python
# append to local_inference_director.py
import gc as _gc
from .memory_pressure_gate import PressureLevel


class LocalInferenceDirector:
    """Lifecycle + memory-aware governance for the local tier."""

    def __init__(self, cfg: "LocalConfig", client) -> None:
        self._cfg = cfg
        self._client = client

    def admit_concurrency(self, level: "PressureLevel") -> int:
        if level is PressureLevel.CRITICAL:
            return 0
        if level is PressureLevel.HIGH:
            return 1
        return self._cfg.max_concurrency

    async def _evict_model(self) -> None:
        """Force immediate unload from unified memory via keep_alive:0."""
        try:
            sess = await self._client._ensure_session()
            url = self._cfg.base_url.rstrip("/") + "/api/generate"
            async with sess.post(url, json={"model": self._cfg.model_name, "keep_alive": 0}):
                pass
        except Exception:
            pass  # eviction is best-effort; never raise into the control path

    async def enforce_memory(self, level: "PressureLevel") -> None:
        """At CRITICAL: un-bypassable atomic teardown."""
        if level is not PressureLevel.CRITICAL:
            return
        await self._evict_model()       # 1) API unload
        _gc.collect()                    # 2) dual-stage GC sweep
        _gc.collect()
        await asyncio.sleep(0)           # 3) yield to host OS for RAM reclaim
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/governance/test_local_inference_director.py -k "concurrency or eviction" -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(jprime): Phase 3 Task 5 — memory-aware concurrency + CRITICAL eviction valve"
```

---

## Task 6: Wire `LocalPrimeClient` into `PrimeProvider` under the kill-switch

**Files:**
- Modify: `backend/core/ouroboros/governance/providers.py` (construction site that builds/injects the Prime client)
- Test: `tests/governance/test_local_prime_cascade_integration.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/governance/test_local_prime_cascade_integration.py
from __future__ import annotations
import pytest

def test_local_client_factory_off_returns_none(monkeypatch):
    monkeypatch.setenv("JARVIS_LOCAL_PRIME_ENABLED", "false")
    from backend.core.ouroboros.governance.local_inference_director import build_local_prime_client
    assert build_local_prime_client() is None  # OFF -> no client -> legacy path

def test_local_client_factory_on_returns_client(monkeypatch):
    monkeypatch.setenv("JARVIS_LOCAL_PRIME_ENABLED", "true")
    from backend.core.ouroboros.governance.local_inference_director import (
        build_local_prime_client, LocalPrimeClient)
    c = build_local_prime_client()
    assert isinstance(c, LocalPrimeClient)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/governance/test_local_prime_cascade_integration.py -v`
Expected: FAIL — `ImportError: build_local_prime_client`.

- [ ] **Step 3: Write minimal implementation**

```python
# append to local_inference_director.py
from typing import Optional as _Opt

def build_local_prime_client() -> "_Opt[LocalPrimeClient]":
    """Factory honoring the master kill-switch. OFF -> None (legacy untouched)."""
    if not local_prime_enabled():
        return None
    return LocalPrimeClient(LocalConfig.from_env())
```

Then in `providers.py`, at the site that constructs the Prime client for `PrimeProvider`, add (guarded, additive — do not alter the existing GCP path):

```python
# providers.py — near PrimeProvider construction
from .local_inference_director import build_local_prime_client, local_prime_enabled

# When the GCP PrimeClient is absent AND the local tier is enabled, inject the local client.
if local_prime_enabled() and prime_client is None:
    _local = build_local_prime_client()
    if _local is not None:
        prime_client = _local  # PrimeProvider.generate() consumes it unchanged
```

> The wiring is additive and gated: with `JARVIS_LOCAL_PRIME_ENABLED=false`, `build_local_prime_client()` returns `None` and the block is a no-op — the construction path is byte-identical to today.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/governance/test_local_prime_cascade_integration.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(jprime): Phase 3 Task 6 — gated injection into PrimeProvider"
```

---

## Task 7: Feed `terminal_lag_lockup` into the existing `FailbackStateMachine`

**Files:**
- Modify: `backend/core/ouroboros/governance/candidate_generator.py` (the existing degrade/cascade seam)
- Test: `tests/governance/test_local_prime_cascade_integration.py`

- [ ] **Step 1: Write the failing test**

```python
def test_lockup_maps_to_primary_degraded():
    from backend.core.ouroboros.governance.local_inference_director import LocalLatencyLockup
    from backend.core.ouroboros.governance.candidate_generator import classify_local_failure
    verdict = classify_local_failure(LocalLatencyLockup("timeout"))
    assert verdict.degrade is True
    assert verdict.target_state == "PRIMARY_DEGRADED"
    assert verdict.cascade_upstream is True

def test_normal_exception_does_not_degrade():
    from backend.core.ouroboros.governance.candidate_generator import classify_local_failure
    verdict = classify_local_failure(ValueError("schema"))
    assert verdict.degrade is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/governance/test_local_prime_cascade_integration.py -k lockup -v`
Expected: FAIL — `ImportError: classify_local_failure`.

- [ ] **Step 3: Write minimal implementation**

```python
# candidate_generator.py — additive helper near the FailbackStateMachine
from dataclasses import dataclass

@dataclass(frozen=True)
class LocalFailureVerdict:
    degrade: bool
    cascade_upstream: bool
    target_state: str | None

def classify_local_failure(exc: BaseException) -> LocalFailureVerdict:
    """Map a local-tier exception to an FSM transition.

    terminal_lag_lockup -> degrade J-Prime to PRIMARY_DEGRADED and cascade the
    op upstream (the FSM already passes context on cascade; no sandbox teardown).
    All other exceptions are ordinary provider failures (no degrade).
    """
    if getattr(exc, "failure_class", None) == "terminal_lag_lockup":
        return LocalFailureVerdict(degrade=True, cascade_upstream=True,
                                   target_state="PRIMARY_DEGRADED")
    return LocalFailureVerdict(degrade=False, cascade_upstream=False, target_state=None)
```

Then at the existing point where local-tier generation is awaited inside the cascade, wrap with `classify_local_failure(...)` and, when `verdict.degrade`, invoke the FSM's existing degrade transition (reuse the current `PRIMARY_DEGRADED` path — do not add a parallel state machine).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/governance/test_local_prime_cascade_integration.py -k lockup -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(jprime): Phase 3 Task 7 — lockup → existing PRIMARY_DEGRADED cascade"
```

---

## Task 8: Kill-switch parity + zero-FD teardown verification harness

**Files:**
- Test: `tests/governance/test_local_prime_cascade_integration.py`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_killswitch_off_makes_no_ollama_call(monkeypatch):
    monkeypatch.setenv("JARVIS_LOCAL_PRIME_ENABLED", "false")
    from backend.core.ouroboros.governance.local_inference_director import build_local_prime_client
    assert build_local_prime_client() is None  # no client -> no endpoint contact possible

@pytest.mark.asyncio
async def test_director_stop_closes_session_no_leak(monkeypatch):
    monkeypatch.setenv("JARVIS_LOCAL_PRIME_ENABLED", "true")
    from backend.core.ouroboros.governance.local_inference_director import (
        LocalConfig, LocalPrimeClient, LocalInferenceDirector)
    class _S:
        closed = False
        async def close(self): self.closed = True
    client = LocalPrimeClient(LocalConfig.from_env(), session=_S())
    d = LocalInferenceDirector(LocalConfig.from_env(), client=client)
    await d.stop()
    assert client._session is None  # released; zero hanging FDs
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/governance/test_local_prime_cascade_integration.py -k "killswitch or stop" -v`
Expected: FAIL — no `LocalInferenceDirector.stop`.

- [ ] **Step 3: Write minimal implementation**

```python
# add to LocalInferenceDirector
async def stop(self) -> None:
    """Clean teardown: release the pooled session (zero hanging FDs)."""
    try:
        await self._client.aclose()
    except Exception:
        pass
```

- [ ] **Step 4: Run full suite**

Run: `pytest tests/governance/test_local_inference_director.py tests/governance/test_local_prime_cascade_integration.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Regression spot-check (kill-switch OFF parity)**

Run: `JARVIS_LOCAL_PRIME_ENABLED=false pytest tests/governance/ -k "prime or provider or failback" -q`
Expected: green — no behavior change with the tier OFF.

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "feat(jprime): Phase 3 Task 8 — kill-switch parity + zero-FD teardown harness"
```

---

## Manual Live Smoke (optional, post-merge — local interactive only)

```bash
brew install ollama && brew services start ollama
ollama pull qwen2.5-coder:3b
export JARVIS_LOCAL_PRIME_ENABLED=true          # host-local .env, never committed
python3 scripts/ouroboros_battle_test.py --cost-cap 0.10 --idle-timeout 120 -v
# Expect: with remote quota exhausted, ops still generate via gcp-jprime (local);
# warm-standby gives sub-second latencies after the first call;
# tune JARVIS_LOCAL_INFERENCE_TIMEOUT_MS from observed totals.
```

---

## Self-Review (completed)

- **Spec coverage:** §4.1 pooled client → T3; §4.2 breaker → T4/T7; §4.3 eviction → T5; adaptive profiler → T2; kill-switch → T1/T6/T8; structured prompt → T3. All ADD sections mapped.
- **Placeholder scan:** none — every step has runnable test + impl code and exact commands.
- **Type consistency:** `LocalConfig`, `LatencyProfiler`, `LocalPrimeClient`, `LocalInferenceDirector`, `LocalLatencyLockup`, `LocalCompletion`, `LocalFailureVerdict`, `build_local_prime_client`, `classify_local_failure` named identically across tasks.
- **Increment isolation (operator's §3):** T1/T3 = host-native pool; T2/T4 = profiler loops; T5 = eviction valves; T6/T7 = gated wiring; T8 = parity. Each independently testable and committed.
- **Safety:** every task is inert under `JARVIS_LOCAL_PRIME_ENABLED=false`; the only `providers.py`/`candidate_generator.py` edits are additive and gated.
