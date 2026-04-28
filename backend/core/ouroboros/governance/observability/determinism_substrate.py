"""Slice 1.2 — Determinism Substrate: canonical hashing primitives.

Per ``OUROBOROS_VENOM_PRD.md`` §24.10.1 (Priority 1):

  > Without determinism, no improvement claim is verifiable.
  > Every decisional call must be reproducible; every decision
  > event must be content-addressed.

This module ships the **canonical serialization + hashing layer** that
underpins the entire Determinism Substrate:

  1. ``CanonicalSerializer`` — strictly typed, architecture-stable JSON
     serializer that guarantees byte-identical output on any Python ≥3.9
     running on any arch (arm64 / x86_64). Uses ``json.dumps`` with
     ``sort_keys=True``, ``ensure_ascii=True``, ``separators=(",",":")``
     (no whitespace jitter), and a custom ``default`` that raises on
     unsupported types rather than silently stringifying.

  2. ``PromptHasher`` — sha256 of the prompt template + tool sequence
     + model parameters for a given operation. Produces a stable
     ``DecisionHash`` that uniquely identifies "what was asked" for
     any decisional model call.

  3. ``DecisionHash`` — frozen dataclass: content-addressed hash of
     ``(prompt_hash, model_id, temperature, tool_order)``. The Merkle
     DAG extension in Slice 1.3 stores this as metadata on each
     ``DecisionRow``.

## Cage rules (load-bearing)

  * **Stdlib-only import surface.** No governance, no provider, no
    orchestrator imports. This is a leaf module.
  * **NEVER raises into the caller** — all public functions return
    structured results. Internal serialization failures produce a
    sentinel hash (``"error:<reason>"``) rather than bubbling up.
  * **No hardcoded string concatenation for hashing** (operator
    ruling). All hash inputs pass through ``CanonicalSerializer``
    which uses ``json.dumps`` with deterministic settings.
  * **Architecture-stable** — ``ensure_ascii=True`` prevents
    locale-dependent encoding. Float serialization uses ``repr()``
    through the ``json`` module's C encoder (IEEE 754 compliant
    across arm64 / x86_64 for finite floats).

## Default-off

``JARVIS_DETERMINISM_SUBSTRATE_ENABLED`` (default false until
Phase 1 graduation).
"""
from __future__ import annotations

import enum
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


_TRUTHY = ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Master flag
# ---------------------------------------------------------------------------


def is_substrate_enabled() -> bool:
    """Master flag — ``JARVIS_DETERMINISM_SUBSTRATE_ENABLED``
    (default false until Phase 1 graduation)."""
    return os.environ.get(
        "JARVIS_DETERMINISM_SUBSTRATE_ENABLED", "",
    ).strip().lower() in _TRUTHY


# ---------------------------------------------------------------------------
# Canonical Serializer
# ---------------------------------------------------------------------------
#
# Operator ruling: "do not use hardcoded string concatenation. Use a
# strictly typed, canonical JSON serializer to ensure cryptographic
# hash stability across different architectures."
#
# This serializer is the ONLY path through which hash inputs flow.
# It enforces:
#   * sort_keys=True — key order is alphabetical, not insertion-order
#   * ensure_ascii=True — no locale-dependent encoding
#   * separators=(",", ":") — no whitespace jitter
#   * Strict default — unsupported types raise TypeError (caught
#     by callers) rather than silently stringifying via str()
#
# The explicit REJECTION of ``default=str`` is load-bearing:
# ``str(datetime.now())`` is locale-dependent; ``str(Path(...))``
# varies by OS. If a caller passes an unsupported type, the hash
# must FAIL rather than silently produce a non-deterministic digest.


# Types the canonical serializer accepts. Everything else is rejected.
_CANONICAL_TYPES: Tuple[type, ...] = (
    str, int, float, bool, type(None), list, tuple, dict,
)


def _canonical_default(obj: Any) -> Any:
    """JSON default handler that rejects non-canonical types.

    Tuples are converted to lists (JSON has no tuple type).
    Enums are converted to their ``.value``.
    Frozensets are converted to sorted lists.
    Everything else raises TypeError — the caller gets a sentinel hash.
    """
    if isinstance(obj, tuple):
        return list(obj)
    if isinstance(obj, enum.Enum):
        return obj.value
    if isinstance(obj, frozenset):
        return sorted(obj)
    if isinstance(obj, set):
        return sorted(obj)
    if isinstance(obj, bytes):
        return obj.hex()
    raise TypeError(
        f"canonical_serialize: unsupported type {type(obj).__name__} "
        f"for value {repr(obj)[:100]}"
    )


def canonical_serialize(obj: Any) -> str:
    """Serialize ``obj`` to a canonical JSON string.

    Returns a byte-stable string suitable for sha256 hashing.
    The output is identical across Python ≥3.9 on arm64 / x86_64
    for the same input (assuming finite floats — Inf/NaN are
    rejected by the JSON spec and will raise).

    Raises
    ------
    TypeError
        If ``obj`` contains unsupported types (see ``_canonical_default``).
    ValueError
        If ``obj`` contains non-finite floats (Inf, NaN).
    """
    return json.dumps(
        obj,
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
        default=_canonical_default,
        allow_nan=False,  # reject Inf/NaN — non-deterministic repr
    )


def canonical_hash(obj: Any) -> str:
    """Compute sha256 hex digest of the canonical JSON of ``obj``.

    Returns a 64-character lowercase hex string on success, or
    ``"error:<reason>"`` if serialization fails. NEVER raises.
    """
    try:
        serialized = canonical_serialize(obj)
    except (TypeError, ValueError, OverflowError) as exc:
        return f"error:{type(exc).__name__}:{str(exc)[:200]}"
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Decision Hash
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DecisionHash:
    """Content-addressed hash of a decisional model call.

    Uniquely identifies "what was asked" — the combination of prompt
    content, model identity, temperature, and tool ordering. Two calls
    with identical ``DecisionHash`` values asked the same question to
    the same model with the same parameters.

    Fields
    ------
    digest : str
        64-char lowercase hex sha256 digest. ``"error:..."`` on
        serialization failure.
    prompt_hash : str
        sha256 of the prompt content alone (for prompt-level dedup).
    model_id : str
        Provider/model identifier (e.g. ``"claude-sonnet-4-20250514"``).
    temperature : float
        Temperature used for this call.
    tool_order_hash : str
        sha256 of the sorted tool names available to this call.
    """

    digest: str
    prompt_hash: str
    model_id: str
    temperature: float
    tool_order_hash: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "digest": self.digest,
            "prompt_hash": self.prompt_hash,
            "model_id": self.model_id,
            "temperature": self.temperature,
            "tool_order_hash": self.tool_order_hash,
        }


# ---------------------------------------------------------------------------
# Prompt Hasher
# ---------------------------------------------------------------------------


class PromptHasher:
    """Compute stable hashes of prompt + tool + model parameter tuples.

    Usage::

        hasher = PromptHasher()
        dh = hasher.hash_decision(
            prompt="Generate code for ...",
            model_id="claude-sonnet-4-20250514",
            temperature=0.0,
            tool_names=("read_file", "search_code"),
        )
        assert len(dh.digest) == 64  # sha256 hex

    All inputs pass through ``canonical_serialize`` — no string
    concatenation. The hash is stable across architectures.
    """

    @staticmethod
    def hash_prompt(prompt: str) -> str:
        """sha256 hex of the prompt string. NEVER raises."""
        try:
            return hashlib.sha256(
                prompt.encode("utf-8", errors="replace"),
            ).hexdigest()
        except Exception:  # noqa: BLE001 — defensive
            return "error:hash_prompt_failed"

    @staticmethod
    def hash_tool_order(tool_names: Sequence[str]) -> str:
        """sha256 hex of the sorted tool names. NEVER raises.

        Sorting ensures the hash is order-independent — the SAME
        set of tools always produces the SAME hash.
        """
        try:
            canonical = canonical_serialize(sorted(tool_names))
            return hashlib.sha256(
                canonical.encode("utf-8"),
            ).hexdigest()
        except Exception:  # noqa: BLE001 — defensive
            return "error:hash_tool_order_failed"

    def hash_decision(
        self,
        *,
        prompt: str,
        model_id: str,
        temperature: float,
        tool_names: Sequence[str] = (),
        extra_context: Optional[Dict[str, Any]] = None,
    ) -> DecisionHash:
        """Compute a ``DecisionHash`` for a decisional model call.

        Parameters
        ----------
        prompt:
            The full prompt string sent to the model.
        model_id:
            Provider/model identifier.
        temperature:
            Temperature parameter for this call.
        tool_names:
            Names of tools available to this call (order-independent).
        extra_context:
            Additional context dict to include in the hash (e.g.
            ``{"op_id": "...", "phase": "VALIDATE"}``). Must be
            JSON-serializable via ``canonical_serialize``.

        Returns a ``DecisionHash``. NEVER raises.
        """
        prompt_h = self.hash_prompt(prompt)
        tool_h = self.hash_tool_order(tool_names)

        # Build the canonical input for the overall digest.
        # Structure is deterministic: sorted keys, canonical JSON.
        hash_input: Dict[str, Any] = {
            "model_id": model_id,
            "prompt_hash": prompt_h,
            "temperature": temperature,
            "tool_order_hash": tool_h,
        }
        if extra_context:
            hash_input["extra_context"] = extra_context

        digest = canonical_hash(hash_input)

        return DecisionHash(
            digest=digest,
            prompt_hash=prompt_h,
            model_id=model_id,
            temperature=temperature,
            tool_order_hash=tool_h,
        )


# ---------------------------------------------------------------------------
# Temperature policy (§24.10.1 — ALL decisional calls pinned to 0)
# ---------------------------------------------------------------------------
#
# Operator ruling:
#   "Pin temperature=0 for ALL decisional calls, including ROUTE
#    classification, semantic triage, and risk tiering. Only GENERATE
#    (the creative mutation step) is permitted to run with stochastic
#    jitter."
#
# This module provides the policy; providers.py and candidate_generator.py
# read it to override their temperature at call time.


class CallCategory(str, enum.Enum):
    """Classification of a model call for temperature policy.

    ``DECISIONAL`` calls are deterministic (temperature=0).
    ``CREATIVE`` calls permit stochastic jitter (temperature>0).
    """

    DECISIONAL = "decisional"
    CREATIVE = "creative"


# Phases classified as DECISIONAL (temperature pinned to 0).
# Every phase not in CREATIVE_PHASES is DECISIONAL by default.
CREATIVE_PHASES: FrozenSet[str] = frozenset({
    "GENERATE",
    "GENERATE_RETRY",
})


# Env-overridable max temperature for decisional calls. Default 0.
# Clamped to [0, 0.3] — values above 0.3 defeat determinism.
_DECISIONAL_TEMP_CAP = 0.3


def get_decisional_temperature() -> float:
    """Temperature for DECISIONAL model calls.

    Default ``0.0``. Env-overridable via
    ``JARVIS_DECISIONAL_TEMPERATURE`` (clamped to ``[0, 0.3]``).
    """
    raw = os.environ.get("JARVIS_DECISIONAL_TEMPERATURE")
    if raw is None:
        return 0.0
    try:
        v = float(raw)
        return max(0.0, min(_DECISIONAL_TEMP_CAP, v))
    except (TypeError, ValueError):
        return 0.0


def resolve_temperature(
    *,
    phase: str,
    requested_temperature: float,
) -> Tuple[float, CallCategory]:
    """Resolve the effective temperature for a model call.

    Parameters
    ----------
    phase:
        Current pipeline phase name (e.g. ``"VALIDATE"``,
        ``"GENERATE"``).
    requested_temperature:
        The temperature the caller originally wanted.

    Returns
    -------
    (effective_temperature, call_category):
        For CREATIVE phases, returns ``(requested_temperature,
        CREATIVE)``. For DECISIONAL phases, returns
        ``(decisional_temperature, DECISIONAL)``.
    """
    phase_upper = (phase or "").strip().upper()
    if phase_upper in CREATIVE_PHASES:
        return (requested_temperature, CallCategory.CREATIVE)
    return (get_decisional_temperature(), CallCategory.DECISIONAL)


# ---------------------------------------------------------------------------
# Singleton hasher
# ---------------------------------------------------------------------------


_DEFAULT_HASHER: Optional[PromptHasher] = None


def get_default_hasher() -> PromptHasher:
    global _DEFAULT_HASHER
    if _DEFAULT_HASHER is None:
        _DEFAULT_HASHER = PromptHasher()
    return _DEFAULT_HASHER


def reset_default_hasher() -> None:
    global _DEFAULT_HASHER
    _DEFAULT_HASHER = None


__all__ = [
    "CallCategory",
    "CanonicalSerializer",
    "CREATIVE_PHASES",
    "DecisionHash",
    "PromptHasher",
    "canonical_hash",
    "canonical_serialize",
    "get_decisional_temperature",
    "get_default_hasher",
    "is_substrate_enabled",
    "reset_default_hasher",
    "resolve_temperature",
]
"""
CanonicalSerializer is the module-level namespace (not a class) — the
public API is ``canonical_serialize()`` and ``canonical_hash()``.
"""
