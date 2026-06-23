# backend/core/ouroboros/governance/local_inference_director.py
"""Local inference tier (J-Prime activation, Phase 3).

Three units (added across Phase 3 tasks): LatencyProfiler, LocalPrimeClient,
LocalInferenceDirector. Gated behind JARVIS_LOCAL_PRIME_ENABLED (default OFF ->
byte-identical legacy).
"""
from __future__ import annotations

import asyncio
import gc
import math
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Optional

from .memory_pressure_gate import PressureLevel, get_default_gate, is_enabled as memory_gate_enabled

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
        def _i(n: str, d: int) -> int: return int(os.environ.get(n, str(d)))
        def _f(n: str, d: float) -> float: return float(os.environ.get(n, str(d)))
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


class LatencyProfiler:
    """Thread-safe sliding window of (ttft_ms, per_token_ms) -> bounded adaptive timeout.

    Cold start uses the seed; the adaptive value is always clamped to
    [floor, ceiling]. The ceiling is the un-flexible hard cap that guarantees a
    wedged model still trips the breaker (watchdog-isolation invariant).
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
    def _mean(xs: Deque[float]) -> float:
        return sum(xs) / len(xs) if xs else 0.0

    @classmethod
    def _stddev(cls, xs: Deque[float]) -> float:
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
        if elapsed_ms > cfg.timeout_ceiling_ms:
            return True
        with self._lock:
            warm = len(self._total) >= cfg.min_samples
            if not warm:
                return False
            m = self._mean(self._total)
            sd = self._stddev(self._total)
        # Use a minimum stddev floor of 10% of mean so that a perfectly uniform
        # sample distribution still produces a meaningful 3-sigma band.
        sd_eff = max(sd, m * 0.1)
        return elapsed_ms > (m + 3.0 * sd_eff)


class LocalLatencyLockup(RuntimeError):
    """Raised when local inference breaches the adaptive/ceiling timeout.

    Consumed by candidate_generator's FailbackStateMachine to transition
    J-Prime to PRIMARY_DEGRADED and cascade the op upstream.
    """
    failure_class = "terminal_lag_lockup"


class LocalMemoryCritical(RuntimeError):
    """Raised when host memory is CRITICAL at local-generate admission time.

    The local tier evicts the model and refuses the op so the cascade routes
    upstream to remote providers instead of OOM-ing the host. Consumed by
    classify_local_failure -> PRIMARY_DEGRADED.
    """
    failure_class = "local_memory_critical"


def render_structured_prompt(*, task: str, constraints: List[str], files: Dict[str, str]) -> str:
    """Structured-prompt discipline for the local 3B: rigid bounded tags, no loose NL."""
    parts = ["<task>", task, "</task>", "<constraints>"]
    parts += [f"- {c}" for c in constraints]
    parts += ["</constraints>", "<files>"]
    for path, body in files.items():
        parts += [f'<file path="{path}">', body, "</file>"]
    parts += ["</files>", "<output_format>full_content</output_format>"]
    return "\n".join(parts)


@dataclass
class LocalCompletion:
    text: str
    output_tokens: int
    ttft_ms: float
    total_ms: float


class LocalPrimeClient:
    """aiohttp connection-pooled client -> Ollama OpenAI-compat endpoint.

    A persistent session (lazily built, or injected for tests) with a bounded
    TCPConnector + keep-alive eliminates per-call socket setup across L2 passes.
    """

    def __init__(self, cfg: LocalConfig, session: Optional[Any] = None) -> None:
        self._cfg = cfg
        self._session = session
        self.profiler = LatencyProfiler(cfg)
        self._governor: Any = None

    def attach_governor(self, governor: Any) -> None:
        """Attach a LocalInferenceDirector so generate() consults memory_guard()
        before each local inference (host-OOM protection). When unattached,
        behavior is byte-identical to the ungoverned path."""
        self._governor = governor

    async def _ensure_session(self) -> Any:
        if self._session is None:
            import aiohttp  # local import keeps module import cheap when OFF
            conn = aiohttp.TCPConnector(
                limit=self._cfg.pool_limit,
                limit_per_host=self._cfg.pool_limit,
                keepalive_timeout=max(30, self._cfg.keep_alive_seconds),
            )
            self._session = aiohttp.ClientSession(
                connector=conn, headers={"Connection": "keep-alive"},
            )
        return self._session

    async def complete(self, *, system: str, user: str, prompt_tokens: int,
                       temperature: float = 0.2,
                       max_tokens: "Optional[int]" = None) -> LocalCompletion:
        sess = await self._ensure_session()
        url = self._cfg.base_url.rstrip("/") + "/v1/chat/completions"
        body: Dict[str, Any] = {
            "model": self._cfg.model_name,
            "keep_alive": self._cfg.keep_alive_seconds,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        t0 = time.monotonic()
        async with sess.post(url, json=body) as resp:
            data = await resp.json()
        total_ms = (time.monotonic() - t0) * 1000.0
        text = data["choices"][0]["message"]["content"]
        out_toks = int(data.get("usage", {}).get("completion_tokens", 0)) or max(1, len(text) // 4)
        ttft_ms = min(total_ms, 0.1 * total_ms)
        self.profiler.record(ttft_ms=ttft_ms, total_ms=total_ms, output_tokens=out_toks)
        return LocalCompletion(text=text, output_tokens=out_toks, ttft_ms=ttft_ms, total_ms=total_ms)

    async def complete_guarded(self, *, system: str, user: str, prompt_tokens: int,
                               temperature: float = 0.2,
                               max_tokens: "Optional[int]" = None) -> LocalCompletion:
        timeout_ms = self.profiler.adaptive_timeout_ms(prompt_tokens=prompt_tokens)
        try:
            return await asyncio.wait_for(
                self.complete(system=system, user=user, prompt_tokens=prompt_tokens,
                              temperature=temperature, max_tokens=max_tokens),
                timeout=timeout_ms / 1000.0,
            )
        except asyncio.TimeoutError as e:
            raise LocalLatencyLockup(
                f"local_inference timeout: budget={timeout_ms:.0f}ms "
                f"warm={self.profiler.is_warm()}"
            ) from e

    async def generate(self, prompt: str, system_prompt: "Optional[str]" = None,
                       context: "Optional[Any]" = None, max_tokens: int = 4096,
                       temperature: float = 0.7, model_name: "Optional[str]" = None,
                       task_profile: "Optional[Any]" = None, **kwargs: Any) -> Any:
        """Drop-in PrimeClient.generate adapter -> PrimeResponse (source=local_prime).

        context/task_profile are accepted for interface parity; the 3B path relies
        on the structured prompt + files already in `prompt` (documented v1 limit).
        """
        if self._governor is not None:
            await self._governor.memory_guard()
        import uuid
        from backend.core.prime_client import PrimeResponse
        sys_txt = system_prompt or ""
        est_tokens = max(1, (len(prompt) + len(sys_txt)) // 4)
        lc = await self.complete_guarded(
            system=sys_txt, user=prompt, prompt_tokens=est_tokens,
            temperature=temperature, max_tokens=max_tokens,
        )
        return PrimeResponse(
            content=lc.text,
            request_id=uuid.uuid4().hex,
            model=model_name or self._cfg.model_name,
            source="local_prime",
            latency_ms=lc.total_ms,
            tokens_used=lc.output_tokens,
        )

    async def warmup(self, *, timeout_s: float) -> bool:
        """Force model weights into VRAM via a minimal 1-token generation.

        Fires a lightweight dummy generation (prompt "warmup", num_predict/
        max_tokens 1, temperature 0.0) at the configured endpoint, bounded by
        asyncio.wait_for(timeout_s). Returns True on a successful completion,
        False on timeout or any error. Fail-soft -- never raises.

        This is the cold-load forcing call: awaiting it guarantees the model is
        resident in VRAM before the first real Sovereign generation clock starts.
        Reusable by the FSM AWAKENING gate and the soak harness.
        """
        async def _do_warmup() -> bool:
            sess = await self._ensure_session()
            url = self._cfg.base_url.rstrip("/") + "/v1/chat/completions"
            body = {
                "model": self._cfg.model_name,
                "messages": [{"role": "user", "content": "warmup"}],
                "max_tokens": 1,
                "num_predict": 1,
                "temperature": 0.0,
            }
            async with sess.post(url, json=body) as resp:
                await resp.json()
            return True

        try:
            return await asyncio.wait_for(_do_warmup(), timeout=timeout_s)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001 -- fail-soft
            return False

    async def _check_health(self) -> Any:
        """Drop-in PrimeClient._check_health -> PrimeStatus (AVAILABLE iff Ollama reachable)."""
        from backend.core.prime_client import PrimeStatus
        try:
            sess = await self._ensure_session()
            url = self._cfg.base_url.rstrip("/") + "/api/tags"
            async with sess.get(url) as resp:
                return PrimeStatus.AVAILABLE if resp.status == 200 else PrimeStatus.UNAVAILABLE
        except Exception:
            return PrimeStatus.UNAVAILABLE

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None


def build_local_prime_client() -> "Optional[LocalPrimeClient]":
    """Factory honoring the master kill-switch. OFF -> None (legacy untouched)."""
    if not local_prime_enabled():
        return None
    return LocalPrimeClient(LocalConfig.from_env())


class LocalInferenceDirector:
    """Lifecycle + memory-aware governance for the local tier."""

    def __init__(self, cfg: LocalConfig, client: Any, gate: Any = None) -> None:
        self._cfg = cfg
        self._client = client
        self._gate = gate if gate is not None else get_default_gate()

    async def _evict_model(self) -> None:
        """Force immediate unload from unified memory via keep_alive:0."""
        try:
            sess = await self._client._ensure_session()
            url = self._cfg.base_url.rstrip("/") + "/api/generate"
            async with sess.post(url, json={"model": self._cfg.model_name, "keep_alive": 0}):
                pass
        except Exception:
            pass  # eviction is best-effort; never raise into the control path

    async def enforce_memory(self, level: PressureLevel) -> None:
        """At CRITICAL: un-bypassable atomic teardown."""
        if level is not PressureLevel.CRITICAL:
            return
        await self._evict_model()   # 1) API unload
        gc.collect()                # 2) dual-stage GC sweep
        gc.collect()
        await asyncio.sleep(0)      # 3) yield to host OS for RAM reclaim

    async def memory_guard(self) -> None:
        """Reuse the shared MemoryPressureGate before local inference.

        At CRITICAL host memory, evict the resident model and refuse the op by
        raising LocalMemoryCritical so the cascade routes upstream to remote
        providers instead of OOM-ing the host. Concurrency is NOT handled here
        (candidate_generator's _jprime_sem already caps local inference at 1).
        Pass-through when the gate master switch is OFF.
        """
        if not memory_gate_enabled():
            return
        level = self._gate.pressure()
        if level is PressureLevel.CRITICAL:
            await self.enforce_memory(level)  # evict + dual gc + yield (existing)
            raise LocalMemoryCritical(
                "host memory CRITICAL - local inference refused; cascading upstream"
            )

    async def stop(self) -> None:
        """Clean teardown: release the pooled session (zero hanging FDs)."""
        try:
            await self._client.aclose()
        except Exception:
            pass
