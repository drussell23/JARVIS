# backend/core/ouroboros/governance/local_inference_director.py
"""Local inference tier (J-Prime activation, Phase 3).

Three units (added across Phase 3 tasks): LatencyProfiler, LocalPrimeClient,
LocalInferenceDirector. Gated behind JARVIS_LOCAL_PRIME_ENABLED (default OFF ->
byte-identical legacy).
"""
from __future__ import annotations

import asyncio
import gc
import logging
import math
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Optional, Tuple

from .memory_pressure_gate import PressureLevel, get_default_gate, is_enabled as memory_gate_enabled

logger = logging.getLogger(__name__)

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
    # Autonomous Context-Hardware Negotiator output: the VRAM-safe context window
    # injected as ollama ``options.num_ctx`` + used as the Cognitive Compression
    # budget. None -> legacy (no injection, no compression) = byte-identical.
    num_ctx: Optional[int] = None

    @classmethod
    def from_env(cls) -> "LocalConfig":
        def _i(n: str, d: int) -> int: return int(os.environ.get(n, str(d)))
        def _f(n: str, d: float) -> float: return float(os.environ.get(n, str(d)))
        ceiling = _i("JARVIS_LOCAL_INFERENCE_TIMEOUT_MS", 120_000)
        _nc = os.environ.get("JARVIS_LOCAL_NUM_CTX", "").strip()
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
            num_ctx=int(_nc) if _nc.isdigit() else None,
        )


# ---------------------------------------------------------------------------
# Autonomous Context-Hardware Negotiator + Dynamic Cognitive Compression
# ---------------------------------------------------------------------------
#
# The warm 32B ServerDisconnects on an L4 because the KV cache for a large prompt
# overflows the VRAM left after the ~20GB model weights. We solve this in software:
# derive the max SAFE num_ctx from the MEASURED VRAM buffer (no static cap), then
# compress the payload to fit it (preserve the system rules + recent tool outputs).

# Accurate KV cache per token for a 32B GQA fp16 model: 2(K+V) * 64 layers *
# (8 kv_heads * 128 head_dim = 1024 kv_dim) * 2 bytes = 262144 = 256KB. (The prior
# 512KB was 2x too conservative -- it double-counted the kv_dim -- which crushed
# num_ctx and over-compressed the payload into empty responses.) Env-tunable.
_KV_BYTES_PER_TOKEN_DEFAULT = 262144
_CTX_OVERHEAD_BYTES_DEFAULT = 1_500_000_000  # CUDA/runtime/activation headroom
_NUM_CTX_FLOOR_DEFAULT = 2048
_NUM_CTX_CEILING_DEFAULT = 32768
# Tokens reserved for the model's OUTPUT inside num_ctx. The generation cap
# (max_tokens) is often 4096, but a patch is ~1-2K tokens; reserving the full cap
# halved the INPUT budget and over-compressed. Reserve a bounded, env-tunable
# output slice so the input window stays wide. Default 2048.
_OUTPUT_RESERVE_TOKENS_DEFAULT = 2048


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _f_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def estimate_tokens(text: Any) -> int:
    """Cheap deterministic token estimate (~4 chars/token). NEVER raises."""
    try:
        return max(0, len(text or "") // 4)
    except Exception:  # noqa: BLE001
        return 0


def derive_safe_num_ctx(
    *,
    vram_bytes: int,
    model_bytes: int,
    kv_bytes_per_token: "Optional[int]" = None,
    overhead_bytes: "Optional[int]" = None,
    floor: "Optional[int]" = None,
    ceiling: "Optional[int]" = None,
) -> int:
    """Mathematically derive the max SAFE context window from the MEASURED VRAM
    buffer: ``(vram - model - overhead) / kv_bytes_per_token``, floored to a 256
    multiple and clamped to [floor, ceiling]. NOT a static cap -- a bigger GPU or a
    smaller model widens the window automatically. Fail-soft -> floor on any
    non-positive buffer / bad input."""
    kvbpt = kv_bytes_per_token if kv_bytes_per_token is not None else _int_env(
        "JARVIS_KV_BYTES_PER_TOKEN", _KV_BYTES_PER_TOKEN_DEFAULT)
    ovh = overhead_bytes if overhead_bytes is not None else _int_env(
        "JARVIS_CTX_OVERHEAD_BYTES", _CTX_OVERHEAD_BYTES_DEFAULT)
    flr = floor if floor is not None else _int_env("JARVIS_NUM_CTX_FLOOR", _NUM_CTX_FLOOR_DEFAULT)
    ceil = ceiling if ceiling is not None else _int_env("JARVIS_NUM_CTX_CEILING", _NUM_CTX_CEILING_DEFAULT)
    try:
        if vram_bytes <= 0 or model_bytes <= 0 or kvbpt <= 0:
            return flr
        kv_buffer = int(vram_bytes) - int(model_bytes) - int(ovh)
        if kv_buffer <= 0:
            return flr
        nctx = (int(kv_buffer // kvbpt) // 256) * 256  # 256-multiple for engine friendliness
        return max(flr, min(ceil, nctx))
    except Exception:  # noqa: BLE001
        return flr


def fit_prompt_to_window(
    system: str,
    user: str,
    *,
    max_tokens: int,
    head_frac: float = 0.35,
    tail_frac: float = 0.5,
) -> "Tuple[str, str, bool]":
    """Dynamic Cognitive Compression (sliding window). Preserve the SYSTEM prompt
    (Iron Gate rules) IN FULL + a HEAD (task/plan) and TAIL (most recent tool
    outputs) of the user payload; compress the older intermediate middle into a
    deterministic marker. GUARANTEES ``estimate_tokens(system)+estimate_tokens(user)
    <= max_tokens`` (best-effort; the system is never cut, so if it alone exceeds
    the window the user is reduced to a stub). Returns (system, user, compressed?)."""
    system = system or ""
    user = user or ""
    if estimate_tokens(system) + estimate_tokens(user) <= max_tokens:
        return system, user, False
    user_budget_toks = max_tokens - estimate_tokens(system)
    if user_budget_toks <= 0:
        return system, "[context omitted: system prompt already fills the VRAM-safe window]", True
    char_budget = user_budget_toks * 4
    head_chars = max(0, int(char_budget * head_frac))
    tail_chars = max(0, int(char_budget * tail_frac))
    if len(user) <= head_chars + tail_chars or head_chars + tail_chars == 0:
        return system, user[:char_budget], True
    head = user[:head_chars]
    tail = user[-tail_chars:]
    dropped = len(user) - head_chars - tail_chars
    marker = (
        "\n\n[...cognitive compression: %d chars of older intermediate history "
        "elided to fit the %d-token VRAM-safe window; system rules + recent tool "
        "outputs preserved...]\n\n" % (dropped, max_tokens)
    )
    return system, head + marker + tail, True


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
        # Asymmetric EWMA (ms): timeouts jump it UP (penalty), successes blend it
        # down toward the real latency. Acts as an escalating floor on the adaptive
        # timeout so a starved cold profiler still expands the window. 0 = no data.
        self._ewma_ms: float = 0.0
        # Async Calibration Mutex (Scout Lock): the FIRST cold coroutine calibrates
        # (Scout) while the concurrent herd waits on the lock; once calibrated, the
        # gate is open and dispatches run fully concurrently on the escalated EWMA.
        # Lazily bound to the running loop on first use.
        self._calibrated: bool = False
        self._scout_lock: "Optional[asyncio.Lock]" = None

    def is_calibrated(self) -> bool:
        return self._calibrated

    def mark_calibrated(self) -> None:
        self._calibrated = True

    def _get_scout_lock(self) -> "asyncio.Lock":
        if self._scout_lock is None:
            self._scout_lock = asyncio.Lock()
        return self._scout_lock

    async def run_calibrated(self, coro_factory: "Any") -> Any:
        """Async Calibration Mutex. If already calibrated -> run ``coro_factory()``
        immediately (no lock, full concurrency). Otherwise the FIRST caller acquires
        the scout lock and runs as the Scout; the concurrent herd awaits the lock.
        The Scout marks the profiler calibrated when it finishes (success OR
        timeout+escalate -- in ``finally``, so the herd is never stuck) and releases;
        the herd then runs CONCURRENTLY, reading the newly escalated EWMA seed.
        ``coro_factory`` is a zero-arg async callable (a fresh coroutine per call)."""
        if self.is_calibrated():
            return await coro_factory()
        lock = self._get_scout_lock()
        async with lock:
            if not self.is_calibrated():
                try:
                    return await coro_factory()          # Scout
                finally:
                    self.mark_calibrated()
        return await coro_factory()                       # herd (calibrated)

    def _cold_seed_ms(self) -> float:
        """Context-Aware Dynamic Seed. Survival/CPU (no num_ctx) -> plain base seed
        (byte-identical legacy). Heavy/GPU (negotiated num_ctx) -> the base seed
        scaled by JARVIS_JPRIME_HEAVY_COLDSTART_MULT AND the token payload
        (num_ctx / baseline) -- a 16k window inherently needs a longer first budget
        than 8k. Capped at half the absolute ceiling so escalation has room before
        the breaker. NEVER raises."""
        base = float(self._cfg.timeout_seed_ms)
        if not self._cfg.num_ctx:
            return base
        try:
            heavy_mult = max(1.0, _f_env("JARVIS_JPRIME_HEAVY_COLDSTART_MULT", 4.0))
            baseline = max(1, _int_env("JARVIS_LOCAL_SEED_CTX_BASELINE", 8192))
            ctx_factor = max(1.0, float(self._cfg.num_ctx) / baseline)
            seed = base * heavy_mult * ctx_factor
            return min(seed, _absolute_ceiling_ms() * 0.5)
        except Exception:  # noqa: BLE001
            return base

    def record(self, *, ttft_ms: float, total_ms: float, output_tokens: int) -> None:
        per_tok = (total_ms - ttft_ms) / max(1, output_tokens)
        with self._lock:
            self._ttft.append(float(ttft_ms))
            self._per_tok.append(max(0.0, per_tok))
            self._total.append(float(total_ms))
            # SUCCESS blends the EWMA DOWN toward the observed latency (asymmetric).
            if self._ewma_ms <= 0.0:
                self._ewma_ms = float(total_ms)
            else:
                a = _ewma_alpha()
                self._ewma_ms = a * float(total_ms) + (1.0 - a) * self._ewma_ms

    def record_timeout_penalty(self, timeout_ms: float) -> None:
        """Asymmetric penalty injection: a TIMEOUT jumps the EWMA UP to
        ``timeout_ms * escalation_factor`` so the very next dispatch expands the
        window aggressively (breaks the cold-profiler starvation). NEVER raises."""
        try:
            penalty = float(timeout_ms) * _timeout_escalation_factor()
            with self._lock:
                self._ewma_ms = max(self._ewma_ms, penalty)
        except Exception:  # noqa: BLE001
            pass

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
            ewma = self._ewma_ms

        # SURVIVAL / CPU path (no negotiated num_ctx): BYTE-IDENTICAL legacy -- no
        # EWMA escalation, no absolute breaker, soft ceiling is the cap.
        if not cfg.num_ctx:
            if not warm:
                return float(min(cfg.timeout_seed_ms, cfg.timeout_ceiling_ms))
            est_out = max(1.0, prompt_tokens * cfg.output_ratio)
            flexed = ttft_m + tok_m * est_out + cfg.margin_sigma * tot_sd
            return float(max(cfg.timeout_floor_ms, min(flexed, cfg.timeout_ceiling_ms)))

        # HEAVY / GPU path: Context-Aware Dynamic Seed + asymmetric EWMA escalation
        # + Absolute Global Circuit Breaker.
        absolute = _absolute_ceiling_ms()
        if warm:
            est_out = max(1.0, prompt_tokens * cfg.output_ratio)
            value = ttft_m + tok_m * est_out + cfg.margin_sigma * tot_sd
        else:
            value = self._cold_seed_ms()
        # Never below the (timeout-escalated) EWMA -- a starved cold profiler still
        # expands the window on the next dispatch.
        if ewma > 0.0:
            value = max(value, ewma)
        # Runaway EWMA past the absolute ceiling kills the loop (no infinite
        # inflation / endless billing on a genuinely wedged model).
        if value >= absolute:
            raise UnrecoverableInferenceLatency(
                "adaptive inference timeout %.0fms >= absolute ceiling %.0fms "
                "(EWMA=%.0fms) -- wedged model, halting to prevent endless billing"
                % (value, absolute, ewma)
            )
        # The absolute ceiling is the cap so the dynamic seed / escalation is not
        # crushed by the (survival-sized) soft ceiling.
        return float(max(cfg.timeout_floor_ms, min(absolute, value)))

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


class UnrecoverableInferenceLatency(RuntimeError):
    """Absolute Global Circuit Breaker: the adaptive/EWMA timeout inflated past the
    absolute ceiling (default 20min). Raised to KILL the loop -- prevents infinite
    EWMA inflation + endless billing on a genuinely wedged model. Non-recoverable:
    the L7 auto-heal treats it as terminal (seal/halt), never retries."""
    failure_class = "unrecoverable_inference_latency"


def _absolute_ceiling_ms() -> float:
    """Hard absolute inference-timeout ceiling (ms). The EWMA escalation can grow
    the budget on timeouts; this is the un-inflatable kill line. Default 20min."""
    return max(1000.0, _int_env("JARVIS_LOCAL_INFERENCE_ABSOLUTE_CEILING_MS", 1_200_000))


def _timeout_escalation_factor() -> float:
    """Asymmetric penalty multiplier: a timeout injects timeout*factor into the
    EWMA so the next dispatch aggressively expands the window. Default 1.5."""
    try:
        f = float(os.environ.get("JARVIS_LOCAL_TIMEOUT_ESCALATION_FACTOR", "1.5"))
        return f if f > 1.0 else 1.5
    except (TypeError, ValueError):
        return 1.5


def _ewma_alpha() -> float:
    """EWMA blend weight for SUCCESS samples (decays an escalated budget back
    toward the real latency). Default 0.3. Timeouts jump UP (asymmetric max)."""
    try:
        a = float(os.environ.get("JARVIS_LOCAL_EWMA_ALPHA", "0.3"))
        return a if 0.0 < a <= 1.0 else 0.3
    except (TypeError, ValueError):
        return 0.3


class InterTokenStall(RuntimeError):
    """Asynchronous Inter-Token Watchdog trip: the streamed generation went silent
    (no token chunk within the inter-token timeout). A stalled stream = a wedged
    worker; NON-recoverable (the L7 auto-heal seals/halts, never retries). A stream
    that keeps emitting is allowed to run indefinitely -- total duration is NOT a
    kill condition on the streaming (heavy) path."""
    failure_class = "inter_token_stall"


def _streaming_enabled() -> bool:
    """Master switch for the streaming inter-token watchdog on the heavy (num_ctx)
    generation path. Default TRUE. OFF -> legacy total-duration adaptive timeout."""
    return _envb("JARVIS_LOCAL_STREAMING_ENABLED", True) if os.environ.get(
        "JARVIS_LOCAL_STREAMING_ENABLED") is not None else True


def _inter_token_timeout_s() -> float:
    """Max wall-time between streamed token chunks before the Stream Breaker trips.
    The model may run indefinitely as long as it emits within this gap. Default 30s."""
    return max(1.0, _f_env("JARVIS_LOCAL_INTER_TOKEN_TIMEOUT_S", 30.0))


_SSE_DONE = object()  # sentinel: the [DONE] terminator of an OpenAI-compat SSE stream


def _parse_sse_delta(line: bytes) -> "Any":
    """Parse ONE line of an ollama /v1/chat/completions SSE stream. Returns the
    incremental content string, the ``_SSE_DONE`` sentinel on ``data: [DONE]``, or
    None for keep-alives / non-data / parse errors. Pure + fail-soft."""
    try:
        s = line.decode("utf-8", "ignore").strip() if isinstance(line, (bytes, bytearray)) else str(line).strip()
        if not s or not s.startswith("data:"):
            return None
        payload = s[len("data:"):].strip()
        if payload == "[DONE]":
            return _SSE_DONE
        import json as _json  # noqa: PLC0415
        obj = _json.loads(payload)
        choices = obj.get("choices") or []
        if not choices:
            return None
        delta = (choices[0] or {}).get("delta") or {}
        return delta.get("content") or None
    except Exception:  # noqa: BLE001
        return None


def _emit_stream_token(text: str) -> None:
    """Yield a streamed chunk to stdout for real-time observability (constraint 2).
    Best-effort -- observability never breaks the generation. The 'wall yields to an
    active stream' behavior (constraint 3) is achieved structurally at the dispatch
    layer: the streaming path drops the outer op-deadline wait_for, so an
    actively-emitting call is bounded ONLY by the per-chunk inter-token watchdog +
    the STATIC hard wall-clock cap (kept blind per the Slice-47 Watchdog Isolation
    Invariant -- never coupled to stream state)."""
    try:
        import sys  # noqa: PLC0415
        sys.stdout.write(text)
        sys.stdout.flush()
    except Exception:  # noqa: BLE001
        pass
    # Feed the streaming liveness heartbeat -> the IDLE/staleness watchdog stays
    # fresh while tokens flow (so a streaming op is never idle-killed). Does NOT
    # touch the wall-clock cap (Slice-47: that stays blind). Best-effort.
    try:
        from backend.core.ouroboros.governance import stream_heartbeat as _hb  # noqa: PLC0415
        _hb.pulse()
    except Exception:  # noqa: BLE001
        pass


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

    def __init__(self, cfg: LocalConfig, session: Optional[Any] = None,
                 profiler: "Optional[LatencyProfiler]" = None) -> None:
        self._cfg = cfg
        self._session = session
        # Stateful Latency Profiler: when injected (a session-scoped singleton kept
        # per-endpoint by the dispatcher), the EWMA/sample window SURVIVES across
        # ops + L7 retries -- so the client learns the 32B's real latency (incl. the
        # one-time ~109s load) and adapts its timeout up instead of resetting to the
        # cold seed on every fresh client (the "profiler amnesia").
        self.profiler = profiler if profiler is not None else LatencyProfiler(cfg)
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
                       max_tokens: "Optional[int]" = None,
                       stream: "Optional[bool]" = None) -> LocalCompletion:
        sess = await self._ensure_session()
        url = self._cfg.base_url.rstrip("/") + "/v1/chat/completions"
        # Dynamic Cognitive Compression + num_ctx injection (Context-Hardware
        # Negotiator). When a VRAM-safe num_ctx is configured, fit the payload to
        # the INPUT budget (num_ctx minus reserved output) so the KV cache can never
        # overflow VRAM -> no ServerDisconnect. The system prompt (Iron Gate rules)
        # is preserved in full; older intermediate history is compressed. num_ctx is
        # also declared to the engine so it never pre-allocates a fatal KV cache.
        if self._cfg.num_ctx:
            # Reserve only a BOUNDED output slice (not the full max_tokens cap) so
            # the input window stays wide -> less compression -> fewer empty results.
            _reserve_out = min(
                max_tokens or 0,
                _int_env("JARVIS_FAILOVER_OUTPUT_RESERVE_TOKENS", _OUTPUT_RESERVE_TOKENS_DEFAULT),
            ) if max_tokens else _int_env(
                "JARVIS_FAILOVER_OUTPUT_RESERVE_TOKENS", _OUTPUT_RESERVE_TOKENS_DEFAULT)
            _in_budget = max(256, int(self._cfg.num_ctx) - _reserve_out)
            system, user, _compressed = fit_prompt_to_window(
                system, user, max_tokens=_in_budget,
            )
            if _compressed:
                logger.info(
                    "[LocalPrimeClient] Cognitive Compression applied: fit payload "
                    "to %d-token input budget (num_ctx=%d, reserved_out=%d)",
                    _in_budget, int(self._cfg.num_ctx), _reserve_out,
                )
        body: Dict[str, Any] = {
            "model": self._cfg.model_name,
            "keep_alive": self._cfg.keep_alive_seconds,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if self._cfg.num_ctx:
            # ollama-native option; harmless if a given engine ignores it (the
            # compression above is the hard guarantee, this is the declarative one).
            body["options"] = {"num_ctx": int(self._cfg.num_ctx)}
        if max_tokens is not None:
            body["max_tokens"] = max_tokens

        _use_stream = stream if stream is not None else (
            bool(self._cfg.num_ctx) and _streaming_enabled())
        if _use_stream:
            return await self._complete_streaming(sess, url, body)

        t0 = time.monotonic()
        async with sess.post(url, json=body) as resp:
            data = await resp.json()
        total_ms = (time.monotonic() - t0) * 1000.0
        text = data["choices"][0]["message"]["content"]
        out_toks = int(data.get("usage", {}).get("completion_tokens", 0)) or max(1, len(text) // 4)
        ttft_ms = min(total_ms, 0.1 * total_ms)
        self.profiler.record(ttft_ms=ttft_ms, total_ms=total_ms, output_tokens=out_toks)
        return LocalCompletion(text=text, output_tokens=out_toks, ttft_ms=ttft_ms, total_ms=total_ms)

    async def _complete_streaming(self, sess: Any, url: str, body: Dict[str, Any]) -> LocalCompletion:
        """Streaming generation with the Asynchronous Inter-Token Watchdog. Reads the
        SSE stream chunk-by-chunk; each ``readline`` is bounded by the inter-token
        timeout (NOT the total duration), so a model that keeps emitting runs
        indefinitely and only a genuine STALL (silence > timeout) trips the breaker.
        Buffers the deltas (constraint 2) while yielding them to stdout, and records
        the REAL end-to-end latency as a profiler sample on success."""
        body = dict(body)
        body["stream"] = True
        inter_token_s = _inter_token_timeout_s()
        parts: List[str] = []
        ttft_ms = 0.0
        t0 = time.monotonic()
        first = True
        async with sess.post(url, json=body) as resp:
            reader = resp.content  # aiohttp StreamReader (line-iterable)
            while True:
                try:
                    line = await asyncio.wait_for(reader.readline(), timeout=inter_token_s)
                except asyncio.TimeoutError as e:
                    raise InterTokenStall(
                        "inter-token stall: no chunk within %.0fs (stream wedged)"
                        % inter_token_s
                    ) from e
                if not line:
                    break  # EOF -> stream complete
                delta = _parse_sse_delta(line)
                if delta is _SSE_DONE:
                    break
                if delta:
                    if first:
                        ttft_ms = (time.monotonic() - t0) * 1000.0
                        first = False
                    parts.append(delta)
                    _emit_stream_token(delta)
        total_ms = (time.monotonic() - t0) * 1000.0
        text = "".join(parts)
        out_toks = max(1, len(text) // 4)
        # A completed stream is a REAL latency sample -> the EWMA learns + decays.
        self.profiler.record(ttft_ms=ttft_ms or min(total_ms, 0.1 * total_ms),
                             total_ms=total_ms, output_tokens=out_toks)
        return LocalCompletion(text=text, output_tokens=out_toks, ttft_ms=ttft_ms, total_ms=total_ms)

    async def complete_guarded(self, *, system: str, user: str, prompt_tokens: int,
                               temperature: float = 0.2,
                               max_tokens: "Optional[int]" = None) -> LocalCompletion:
        # HEAVY (num_ctx) STREAMING path: deprecate the total-duration timeout. The
        # Inter-Token Watchdog inside _complete_streaming is the sole guard -- a
        # model that keeps emitting tokens runs indefinitely; only a STALL trips it.
        # This is the mathematically-robust replacement for guessing total latency.
        if self._cfg.num_ctx and _streaming_enabled():
            logger.info(
                "[LocalPrimeClient] streaming generation (inter-token watchdog=%.0fs, "
                "no total-duration cap) num_ctx=%d",
                _inter_token_timeout_s(), int(self._cfg.num_ctx),
            )
            return await self.complete(
                system=system, user=user, prompt_tokens=prompt_tokens,
                temperature=temperature, max_tokens=max_tokens, stream=True,
            )

        # SURVIVAL / non-streaming path: legacy total-duration adaptive timeout.
        # May raise UnrecoverableInferenceLatency (absolute breaker) -> terminal.
        timeout_ms = self.profiler.adaptive_timeout_ms(prompt_tokens=prompt_tokens)
        try:
            return await asyncio.wait_for(
                self.complete(system=system, user=user, prompt_tokens=prompt_tokens,
                              temperature=temperature, max_tokens=max_tokens),
                timeout=timeout_ms / 1000.0,
            )
        except asyncio.TimeoutError as e:
            self.profiler.record_timeout_penalty(timeout_ms)
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
            # Dedicated Cold-Start HTTP Context: give the warmup POST its OWN total
            # timeout == the (heavy-mult-scaled) warmup budget, overriding aiohttp's
            # 300s session default. A 32B is ~20GB; the PCIe->VRAM cold-load can
            # exceed 300s, and without this the socket would be dropped mid-transfer
            # (min(720s wait_for, 300s default) = 300s). Fail-soft if aiohttp is
            # unavailable -- fall back to the session default.
            try:
                import aiohttp  # noqa: PLC0415
                _post_kw = {"timeout": aiohttp.ClientTimeout(total=max(1.0, timeout_s))}
            except Exception:  # noqa: BLE001
                _post_kw = {}
            async with sess.post(url, json=body, **_post_kw) as resp:
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


async def flush_vram(endpoint: str, model_name: str, *, timeout_s: float = 10.0) -> bool:
    """Deterministic VRAM flush: POST ``keep_alive:0`` to the node so ollama
    immediately unloads the model from VRAM. Fired synchronously by the FSM's
    ``_reap_gpu_node`` BEFORE the GCP delete -- a violent, safe-termination flush so
    the node never lingers holding VRAM. Best-effort -> bool; NEVER raises."""
    if not endpoint or not model_name:
        return False
    try:
        import aiohttp  # noqa: PLC0415
        url = endpoint.rstrip("/") + "/api/generate"
        timeout = aiohttp.ClientTimeout(total=max(1.0, timeout_s))
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post(url, json={"model": model_name, "keep_alive": 0}) as resp:
                await resp.read()
        return True
    except Exception:  # noqa: BLE001 -- flush is best-effort; teardown proceeds regardless
        return False


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
