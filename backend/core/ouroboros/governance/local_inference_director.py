# backend/core/ouroboros/governance/local_inference_director.py
"""Local inference tier (J-Prime activation, Phase 3).

Three units (added across Phase 3 tasks): LatencyProfiler, LocalPrimeClient,
LocalInferenceDirector. Gated behind JARVIS_LOCAL_PRIME_ENABLED (default OFF ->
byte-identical legacy).
"""
from __future__ import annotations

import math
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Optional

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

    async def _ensure_session(self) -> object:
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

    async def complete(self, *, system: str, user: str, prompt_tokens: int) -> LocalCompletion:
        sess = await self._ensure_session()
        url = self._cfg.base_url.rstrip("/") + "/v1/chat/completions"
        body = {
            "model": self._cfg.model_name,
            "keep_alive": self._cfg.keep_alive_seconds,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        t0 = time.monotonic()
        async with sess.post(url, json=body) as resp:
            data = await resp.json()
        total_ms = (time.monotonic() - t0) * 1000.0
        text = data["choices"][0]["message"]["content"]
        out_toks = int(data.get("usage", {}).get("completion_tokens", 0)) or max(1, len(text) // 4)
        ttft_ms = min(total_ms, 0.1 * total_ms)
        self.profiler.record(ttft_ms=ttft_ms, total_ms=total_ms, output_tokens=out_toks)
        return LocalCompletion(text=text, output_tokens=out_toks, ttft_ms=ttft_ms, total_ms=total_ms)

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
