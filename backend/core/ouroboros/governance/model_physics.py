# backend/core/ouroboros/governance/model_physics.py
"""Per-model physics, read from the serving engine's own metadata.

WHY THIS MODULE EXISTS
----------------------
Two different numbers bound how much context an op may be given, and they are
owned by two different authorities:

  * What the DEVICE can hold -- ``(VRAM - weights - overhead) / kv_per_token``.
    Owned by the Context-Hardware Negotiator (``derive_safe_num_ctx``).
  * What the MODEL was TRAINED for. Owned by the model, and knowable only by
    asking it.

A single global ``JARVIS_NUM_CTX_CEILING`` collapses those into one constant, so
every model wears the most restrictive value any model needs. On the current
local fleet that is an 8x error: ``qwen2.5-coder`` is 32K native while
``qwen3-coder:30b`` is 262K. Capping the MoE at 32K wastes context it was built
for; raising the global to 262K would hand ``qwen2.5-coder`` a window past its
trained limit, where output degrades rather than improves. Neither number is
wrong -- they are simply not the same number.

The same argument applies to KV cost. ``JARVIS_KV_BYTES_PER_TOKEN`` is a single
pessimistic constant, but KV cost is architectural:

    kv_bytes_per_token = block_count x kv_heads x (key_len + value_len) x dtype

Across the local fleet that spans 57,344 to 262,144 -- a 4.6x spread. Computed
from metadata it reproduces the hand-derived 262,144 in
``local_inference_director`` EXACTLY for ``qwen2.5-coder:32b``, which is the
check that says the formula is right rather than merely plausible.

WHAT THIS IS NOT
----------------
Not a transport (composes ``aiohttp`` the way ``_fetch_served_model_bytes``
already does), not a cache authority beyond one memo dict, and not a policy: it
reports what the model IS. Deciding what to do about it stays with the
negotiator. Every function is fail-soft -- an unresolvable model yields ``None``
and every caller must keep its legacy path, because a guessed context window is
worse than an un-negotiated one.

Python 3.9+. Gated by ``JARVIS_MODEL_PHYSICS_AUTODETECT_ENABLED`` (default OFF
-> byte-identical legacy behaviour).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("Ouroboros.ModelPhysics")

ENABLED_ENV = "JARVIS_MODEL_PHYSICS_AUTODETECT_ENABLED"
DTYPE_BYTES_ENV = "JARVIS_KV_CACHE_DTYPE_BYTES"
TIMEOUT_ENV = "JARVIS_MODEL_PHYSICS_TIMEOUT_S"

_TRUTHY = ("1", "true", "yes", "on")

#: (endpoint, model) -> ModelPhysics. A model's architecture does not change
#: while it is loaded, so this is memoized for the process lifetime. Failures
#: are NOT cached, so a transient probe error retries on the next op.
_PHYSICS_CACHE: Dict[Tuple[str, str], "ModelPhysics"] = {}


def physics_autodetect_enabled() -> bool:
    """Master gate. Default OFF -> callers keep the global-constant path,
    byte-identical. NEVER raises."""
    try:
        return os.environ.get(ENABLED_ENV, "").strip().lower() in _TRUTHY
    except Exception:  # noqa: BLE001
        return False


def kv_cache_dtype_bytes() -> int:
    """Bytes per KV element. Default 2 (fp16), which is what llama.cpp/Ollama
    allocate unless KV quantization is explicitly enabled. An operator running a
    quantized KV cache sets this to 1. NEVER raises."""
    try:
        return max(1, int(os.environ.get(DTYPE_BYTES_ENV, "2")))
    except (TypeError, ValueError):
        return 2


def probe_timeout_s() -> float:
    """Wall-clock bound on the metadata probe. Default 8s, matching the sibling
    ``/api/tags`` fetch. NEVER raises."""
    try:
        return max(0.5, float(os.environ.get(TIMEOUT_ENV, "8")))
    except (TypeError, ValueError):
        return 8.0


@dataclass(frozen=True)
class ModelPhysics:
    """What a served model's architecture implies for context sizing."""

    #: Tokens the model was TRAINED to handle. A hard ceiling on quality, not a
    #: memory fact -- exceeding it needs rope-scaling and degrades output.
    native_context: int
    #: Bytes of KV cache one token costs on this model.
    kv_bytes_per_token: int
    architecture: str
    block_count: int
    kv_heads: int
    key_length: int
    value_length: int
    #: "metadata" when every field was read; "metadata+derived_head_dim" when
    #: key/value length were absent and head_dim was inferred from
    #: embedding_length / head_count. Provenance is reported, never assumed.
    source: str = "metadata"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "native_context": self.native_context,
            "kv_bytes_per_token": self.kv_bytes_per_token,
            "architecture": self.architecture,
            "block_count": self.block_count,
            "kv_heads": self.kv_heads,
            "key_length": self.key_length,
            "value_length": self.value_length,
            "source": self.source,
        }


def _as_int(value: Any) -> int:
    """Tolerant int coercion -- GGUF metadata arrives as int, float or str
    depending on the engine build. 0 on anything unusable. NEVER raises."""
    try:
        if value is None or isinstance(value, bool):
            return 0
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 0


def parse_model_physics(payload: Any) -> "Optional[ModelPhysics]":
    """Build :class:`ModelPhysics` from an Ollama ``/api/show`` payload.

    PURE and separately testable -- the transport is elsewhere on purpose, so
    the arithmetic can be proven against captured metadata without a network.

    Keys are architecture-prefixed (``qwen2.block_count``,
    ``qwen3moe.block_count``, ...), so the architecture is read first from
    ``general.architecture`` rather than guessed by scanning. Returns ``None``
    -- never a partial or defaulted object -- when any load-bearing field is
    missing, because a half-known model must fall back to the global constant
    rather than be sized from an assumption. NEVER raises."""
    try:
        if not isinstance(payload, dict):
            return None
        info = payload.get("model_info")
        if not isinstance(info, dict):
            return None
        arch = str(info.get("general.architecture") or "").strip()
        if not arch:
            return None

        def field(name: str) -> int:
            return _as_int(info.get(arch + "." + name))

        native_context = field("context_length")
        block_count = field("block_count")
        kv_heads = field("attention.head_count_kv")
        key_len = field("attention.key_length")
        val_len = field("attention.value_length")
        source = "metadata"

        # Older GGUF conversions omit key/value length. Derive head_dim from
        # embedding_length / head_count -- correct for classic MHA/GQA layouts
        # but NOT universally: Qwen3 sets head_dim=128 with hidden=2048 and 32
        # heads, where the division yields 64 and would understate KV by 2x.
        # So the derivation is a LAST resort and is stamped in `source` so a
        # consumer can tell a read value from an inferred one.
        if key_len <= 0 or val_len <= 0:
            head_count = field("attention.head_count")
            embedding = field("embedding_length")
            if head_count > 0 and embedding > 0:
                derived = embedding // head_count
                key_len = key_len or derived
                val_len = val_len or derived
                source = "metadata+derived_head_dim"

        if min(native_context, block_count, kv_heads, key_len, val_len) <= 0:
            return None

        kv_per_token = block_count * kv_heads * (key_len + val_len) * kv_cache_dtype_bytes()
        if kv_per_token <= 0:
            return None

        return ModelPhysics(
            native_context=native_context,
            kv_bytes_per_token=kv_per_token,
            architecture=arch,
            block_count=block_count,
            kv_heads=kv_heads,
            key_length=key_len,
            value_length=val_len,
            source=source,
        )
    except Exception:  # noqa: BLE001 -- a parse failure must never break sizing
        return None


async def _fetch_model_show(endpoint: str, model: str) -> Any:
    """POST <endpoint>/api/show -> the raw metadata payload. Fail-soft -> None.

    Mirrors ``_fetch_served_model_bytes``'s shape deliberately: same client,
    same bounded timeout, same swallow-and-return-empty contract."""
    try:
        import aiohttp  # noqa: PLC0415
        url = endpoint.rstrip("/") + "/api/show"
        timeout = aiohttp.ClientTimeout(total=probe_timeout_s())
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post(url, json={"model": model}) as resp:
                if resp.status != 200:
                    return None
                return await resp.json(content_type=None)
    except Exception:  # noqa: BLE001
        return None


async def resolve_model_physics(
    endpoint: "Optional[str]",
    model: "Optional[str]",
    *,
    fetcher: "Optional[Any]" = None,
) -> "Optional[ModelPhysics]":
    """Memoized physics for (endpoint, model). ``None`` when the gate is off or
    the model is unresolvable -- the caller then keeps the global constants.

    A successful read is cached for the process lifetime (architecture is
    immutable for a loaded model); a failure is NOT cached, so a probe against a
    still-booting engine retries rather than poisoning the entry. NEVER raises."""
    try:
        if not physics_autodetect_enabled():
            return None
        if not endpoint or not model:
            return None
        key = (endpoint, model)
        hit = _PHYSICS_CACHE.get(key)
        if hit is not None:
            return hit
        fn = fetcher or _fetch_model_show
        payload = await fn(endpoint, model)
        physics = parse_model_physics(payload)
        if physics is None:
            return None
        _PHYSICS_CACHE[key] = physics
        logger.info(
            "[ModelPhysics] %s: native_context=%d kv_per_token=%d "
            "(%s, %d layers x %d kv-heads x %d+%d, source=%s)",
            model, physics.native_context, physics.kv_bytes_per_token,
            physics.architecture, physics.block_count, physics.kv_heads,
            physics.key_length, physics.value_length, physics.source,
        )
        return physics
    except Exception:  # noqa: BLE001
        return None


def effective_ceiling(
    physics: "Optional[ModelPhysics]", configured_ceiling: int,
) -> int:
    """The context ceiling to hand the negotiator: the STRICTER of what the
    operator configured and what the model was trained for.

    Deliberately a floor-of-two rather than "model wins". The configured ceiling
    is an operator's cost/latency decision and must remain able to hold a model
    BELOW its native limit; the native limit must remain able to hold a model
    below a permissive operator setting. Neither is allowed to override the
    other upward. With no physics, the configured value passes through
    unchanged. NEVER raises."""
    try:
        cfg = max(0, int(configured_ceiling))
        if physics is None or physics.native_context <= 0:
            return cfg
        if cfg <= 0:
            return physics.native_context
        return min(cfg, physics.native_context)
    except Exception:  # noqa: BLE001
        return configured_ceiling


def reset_cache_for_tests() -> None:
    """Drop the memo. Tests only."""
    _PHYSICS_CACHE.clear()
