"""Phase 2A-ii — dormant per-dispatch state substrate for the
upcoming ClaudeProvider._generate_raw decomposition.

This module introduces two pure-data primitives that will replace
the closure's implicit state cells when Phase 2A-iii through 2C-ii
land their per-helper extractions:

  * :class:`_ClaudeDispatchState` — the 8-field mutable record
    that, in the refactored code path, carries the per-dispatch
    state currently held in the closure's 5 nonlocals
    (``raw_content`` / ``input_tokens`` / ``output_tokens`` /
    ``_cached_input``) plus 4 of the 5 outer ``generate()``-scope
    captures (``_first_token_ms`` / ``_last_msg`` /
    ``_thinking_reason_out`` / ``_token_usage``).

    Reset semantic: every ``_dispatch_raw`` invocation gets a fresh
    instance (today this is implicit via closure-cell re-allocation;
    the refactor makes it explicit via construction).

  * :class:`_CumulativeCost` — a small accumulator that carries the
    fifth nonlocal (``total_cost``). Unlike the other state, this
    one survives multiple ``_dispatch_raw`` calls within a single
    ``generate()`` (e.g. the ``tool_loop.run`` multi-round path).
    The refactor models this by constructing one instance per
    ``generate()`` call and threading it explicitly into each
    dispatch.

DORMANCY INVARIANT — Slice 2A-ii ships this module **completely
unused** by ``providers.py``. AST pin
:func:`test_ast_pin_providers_does_not_import_dispatch_state_yet`
enforces that ``providers.py`` is byte-identical to main: the
substrate exists, the green-bar test surface for it exists, but
no caller imports it yet. Phase 2A-iii (extracting
``_boundary_audit_sampler`` as the proof-of-concept first
extraction) is the first PR allowed to flip that pin.

Architectural invariants (AST-pinned):

  * No import of ``providers.py``, the orchestrator, the iron gate,
    the candidate generator, or any authority surface. This module
    is pure data, no dependency cycles, no behavior.

  * No import of any newly-deployed surface
    (evaluator_trace_observer, session_budget_authority,
    provider_response_cache, s2_predictive_budget, swe_bench_pro/*,
    commit_authority).

  * Closed 8-field shape on the dataclass; closed 3-method interface
    on the cost accumulator. Adding a field requires bumping
    :data:`CLAUDE_DISPATCH_STATE_SCHEMA_VERSION` and a paired AST
    pin update.

  * Mutable dataclass (NOT frozen) — the refactor mutates these in
    place across the helper extractions, mirroring exactly what the
    closure's nonlocals do today. ``to_dict()`` / ``from_dict()``
    provide debug snapshots without compromising mutability.

  * ``_CumulativeCost.add(...)`` clamps non-positive inputs to a
    no-op. This matches the closure's defensive accounting (a
    negative cost is the SDK reporting unusable usage; the closure
    today doesn't subtract from ``total_cost``).
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Mapping, Optional


CLAUDE_DISPATCH_STATE_SCHEMA_VERSION: str = (
    "claude_dispatch_state.v1"
)
CLAUDE_STREAM_CONTEXT_SCHEMA_VERSION: str = (
    "claude_stream_context.v1"
)


# Closed field set for the dispatch state — adding requires schema
# bump + paired AST pin update.
_CLAUDE_DISPATCH_STATE_FIELD_NAMES: tuple = (
    "raw_content",
    "input_tokens",
    "output_tokens",
    "cached_input",
    "first_token_ms",
    "last_msg",
    "thinking_reason_out",
    "token_usage",
)


@dataclass
class _ClaudeDispatchState:
    """Per-dispatch mutable state for ClaudeProvider._generate_raw.

    Replaces (when the extraction lands) the 5 closure nonlocals
    + 4 of the 5 outer-``generate()`` captures with explicit
    fields. The fifth outer capture (``total_cost``) is carried
    separately by :class:`_CumulativeCost` because its lifetime
    spans multiple dispatches.

    Mutable by design — the refactored helper methods will mutate
    fields in place exactly as the current nested closures mutate
    their nonlocals. Tests rely on the mutability to characterize
    per-helper state transitions.

    Field semantics (one-to-one with the closure's current cells):

      * ``raw_content`` — accumulated text content across stream
        deltas or the final non-streaming message body.
      * ``input_tokens`` — Claude server-side tokenizer input count
        (from ``message.usage.input_tokens``).
      * ``output_tokens`` — Claude server-side tokenizer output
        count (from ``message_delta.usage.output_tokens``).
      * ``cached_input`` — cache-read input tokens
        (``message.usage.cache_read_input_tokens``).
      * ``first_token_ms`` — monotonic ms timestamp of the first
        text-delta event (None until first delta lands).
      * ``last_msg`` — the final ``Message`` object (or its
        equivalent on the create path) returned by the SDK.
      * ``thinking_reason_out`` — extracted reason text from
        thinking blocks (None when thinking is disabled).
      * ``token_usage`` — open dict of additional per-dispatch
        token telemetry the closure's outer scope reads after
        return (kept as a dict to mirror the closure's open
        shape; the refactor may close this in a later phase).
    """

    raw_content: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input: int = 0
    first_token_ms: Optional[float] = None
    last_msg: Optional[Any] = None
    thinking_reason_out: Optional[str] = None
    token_usage: Dict[str, int] = field(default_factory=dict)

    def reset_for_next_dispatch(self) -> None:
        """Restore every field to its default. Mirrors what the
        closure achieves implicitly today via closure-cell
        re-allocation on each ``_dispatch_raw`` invocation.

        Used by call sites that want to reuse a state instance
        across two dispatches without re-allocating; the canonical
        path is to construct a fresh state. NEVER raises."""
        self.raw_content = ""
        self.input_tokens = 0
        self.output_tokens = 0
        self.cached_input = 0
        self.first_token_ms = None
        self.last_msg = None
        self.thinking_reason_out = None
        self.token_usage = {}

    def to_dict(self) -> Mapping[str, Any]:
        """Snapshot the current state as a plain-dict for logging /
        debugging / SSE pretty-print. ``last_msg`` is coerced to a
        repr-string when present (the raw Message object is not
        JSON-friendly). NEVER raises."""
        try:
            last_msg_repr = (
                repr(self.last_msg) if self.last_msg is not None else None
            )
        except Exception:  # noqa: BLE001 — defensive
            last_msg_repr = "<unrenderable>"
        return {
            "schema_version": CLAUDE_DISPATCH_STATE_SCHEMA_VERSION,
            "raw_content_len": len(self.raw_content),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_input": self.cached_input,
            "first_token_ms": self.first_token_ms,
            "last_msg_repr": last_msg_repr,
            "thinking_reason_out": self.thinking_reason_out,
            "token_usage": dict(self.token_usage),
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any],
    ) -> "_ClaudeDispatchState":
        """Reconstruct from a ``to_dict()`` snapshot. Note: the
        round-trip is LOSSY on two fields:

          * ``raw_content`` cannot be recovered from
            ``raw_content_len``; it is restored to ``""``.
          * ``last_msg`` cannot be recovered from its repr;
            it is restored to ``None``.

        Other fields round-trip exactly. The asymmetry is
        intentional — :meth:`to_dict` is a debug snapshot, not a
        persistence format. NEVER raises."""
        try:
            return cls(
                raw_content="",  # lossy; see docstring
                input_tokens=int(payload.get("input_tokens", 0) or 0),
                output_tokens=int(payload.get("output_tokens", 0) or 0),
                cached_input=int(payload.get("cached_input", 0) or 0),
                first_token_ms=(
                    float(payload["first_token_ms"])
                    if payload.get("first_token_ms") is not None else None
                ),
                last_msg=None,  # lossy; see docstring
                thinking_reason_out=(
                    str(payload["thinking_reason_out"])
                    if payload.get("thinking_reason_out") is not None
                    else None
                ),
                token_usage=dict(payload.get("token_usage") or {}),
            )
        except (TypeError, ValueError, KeyError):
            return cls()


class _CumulativeCost:
    """Per-``generate()`` cumulative cost accumulator.

    Replaces (when the extraction lands) the ``total_cost``
    nonlocal in the closure. One instance per ``generate()`` call;
    survives multiple ``_dispatch_raw`` invocations within that
    call (the ``tool_loop.run`` multi-round path); discarded when
    ``generate()`` returns.

    Thread-safety: this primitive uses an internal lock because
    the refactored helper methods will be class methods that can
    in principle be called concurrently in tests or future
    parallel-fanout paths. The closure today is single-threaded
    async (no lock needed); we add the lock as cheap insurance
    for the refactor's wider call surface — the per-add overhead
    is sub-microsecond. NEVER raises."""

    def __init__(self) -> None:
        self._total: float = 0.0
        self._lock: threading.Lock = threading.Lock()

    @property
    def total(self) -> float:
        """Current cumulative total. Race-tolerant read (no lock —
        a stale snapshot is acceptable for telemetry; the lock is
        held only by :meth:`add`)."""
        return self._total

    def add(self, cost: float) -> None:
        """Add ``cost`` to the cumulative total. Non-positive
        inputs (zero or negative) are silently dropped — matches
        the closure's defensive accounting where a negative
        SDK-reported cost cannot subtract from
        ``provider._daily_spend``. NEVER raises."""
        try:
            value = float(cost)
        except (TypeError, ValueError):
            return  # defensive; ignore non-numeric input
        if value <= 0.0:
            return
        with self._lock:
            self._total += value

    def reset(self) -> None:
        """Zero the accumulator. Used between ``generate()`` calls
        in test paths that reuse one accumulator across multiple
        characterization runs. NEVER raises."""
        with self._lock:
            self._total = 0.0


# Closed field set for the stream-context dataclass — adding requires
# schema bump + paired AST pin update. Frozen by design (read-only
# call context passed to the heavy stream-extraction helper).
_CLAUDE_STREAM_CONTEXT_FIELD_NAMES: tuple = (
    "context",
    "deadline",
    "timeout_s",
    "effective_max_tokens",
    "temperature",
    "thinking_param",
    "system_with_cache",
    "messages",
    "is_tool_round",
    "prompt_chars",
    "call_start",
    "stream_callback",
)


@dataclass(frozen=True)
class _ClaudeStreamContext:
    """Frozen read-only call context for ClaudeProvider's
    _claude_do_stream extraction (Slice 2C-i).

    Bundles the 14 read-only captures the heavy stream helper
    needs from its outer ``_generate_raw`` scope, leaving the
    mutable per-dispatch state to :class:`_ClaudeDispatchState`.
    The frozen-vs-mutable split is the load-bearing design
    invariant — the helper RECEIVES this context as immutable
    config + RECEIVES a separate mutable state to write into.

    Field semantics (one-to-one with the closure's pre-extraction
    captures, with the leading underscore stripped where the
    closure used a hungarian-style name):

      * ``context`` — :class:`OperationContext` from the caller;
        the helper derives the truncated op_id from
        ``getattr(context, "op_id", "?")[:24]``.
      * ``deadline`` — :class:`datetime` UTC deadline for the
        per-attempt budget; the helper computes the per-request
        HTTPX timeout via the module-level
        ``_remaining_utc_budget_s(deadline, floor_s=1.0)``.
      * ``timeout_s`` — float fallback when the live budget
        helper returns ``None``.
      * ``effective_max_tokens`` — int passed to the SDK as
        ``max_tokens``.
      * ``temperature`` — float passed to the SDK.
      * ``thinking_param`` — opaque (``Optional[Mapping[str, Any]]``)
        thinking config; passed through to the SDK unchanged when
        non-None.
      * ``system_with_cache`` — the cached-system-blocks payload
        passed to the SDK as ``system``.
      * ``messages`` — the messages list passed to the SDK.
        Mutated EXTERNALLY by the prefill-fallback wrapper (Slice
        2B-iii) via ``.pop()``; the helper itself reads it but
        does not write. The list IDENTITY is preserved across
        prefill retries — the same list reference is passed in
        twice with the trailing assistant turn popped.
      * ``is_tool_round`` — bool, used in log lines only.
      * ``prompt_chars`` — int, used in log lines only.
      * ``call_start`` — float, monotonic timestamp of the call
        beginning; the helper computes first-token latency as
        ``(time.monotonic() - call_start) * 1000.0``.
      * ``stream_callback`` — ``Optional[Callable[[str], None]]``
        invoked per text-delta. The caller resolves this to one
        of: tool-loop callback, render callback, fanout (both),
        or None (headless). Swallow-on-callback-raise is the
        helper's responsibility.

    (The Phase-1 TTFT + Phase-2 inter-chunk rupture timeouts
    are module-level imported functions
    ``stream_rupture_timeout_s`` / ``stream_inter_chunk_timeout_s``
    in ``providers.py`` — NOT in this ctx; the helper calls them
    directly.)

    Architectural notes:

      * ``@dataclass(frozen=True)`` — attribute assignment after
        construction raises ``FrozenInstanceError``. The CONTAINED
        objects (``messages`` list, ``system_with_cache`` blocks,
        ``thinking_param`` dict) are NOT deep-frozen — Python's
        frozen-dataclass only freezes attribute REBINDING.
        Mutation of the contained collections by other layers
        (the prefill wrapper mutates ``messages.pop()`` from
        outside) is by design.

      * NO ``from_dict()``. This dataclass contains callables
        (``stream_callback``), opaque SDK objects
        (``thinking_param``), and a foreign-typed ``context``.
        Faithful reconstruction from a dict is impossible; we do
        not pretend otherwise. The :meth:`to_debug_dict` helper
        below repr-stringifies non-JSON-friendly fields for
        snapshot debugging only.

      * NO defaults on any field. Every value must be supplied
        explicitly at construction time. This prevents accidental
        hardcoding of default ``temperature=0.0`` /
        ``max_tokens=1024`` / ``deadline=...`` and matches the
        operator-mandated "no defaults for context fields unless
        the current code already has a real default" discipline.
        Currently the closure has NO real defaults for any of
        these — every cell is set explicitly before _do_stream
        opens.
    """

    context: Any
    deadline: datetime
    timeout_s: float
    effective_max_tokens: int
    temperature: float
    thinking_param: Optional[Mapping[str, Any]]
    system_with_cache: Any
    messages: List[Dict[str, Any]]
    is_tool_round: bool
    prompt_chars: int
    call_start: float
    stream_callback: Optional[Callable[[str], None]]

    def to_debug_dict(self) -> Mapping[str, Any]:
        """Snapshot the context as a plain-dict for logging /
        debugging. Non-JSON-friendly fields are repr-stringified
        (callables, foreign types). NEVER raises.

        Note: this is the ONLY serialization surface. No
        ``from_dict`` companion — callables cannot be
        round-tripped, and we do not pretend otherwise."""
        def _repr_safe(v: Any) -> Any:
            try:
                if v is None:
                    return None
                if isinstance(v, (str, int, float, bool)):
                    return v
                if isinstance(v, (list, tuple)):
                    return [_repr_safe(x) for x in v]
                if isinstance(v, dict):
                    return {
                        str(k): _repr_safe(x) for k, x in v.items()
                    }
                # Foreign object / callable — repr-stringify with
                # length cap to avoid log bloat.
                return repr(v)[:200]
            except Exception:  # noqa: BLE001 — defensive
                return "<unrenderable>"

        try:
            deadline_iso = self.deadline.isoformat()
        except Exception:  # noqa: BLE001
            deadline_iso = repr(self.deadline)[:64]
        try:
            messages_len = len(self.messages)
        except Exception:  # noqa: BLE001
            messages_len = -1
        return {
            "schema_version": CLAUDE_STREAM_CONTEXT_SCHEMA_VERSION,
            "context_repr": _repr_safe(self.context),
            "deadline_iso": deadline_iso,
            "timeout_s": float(self.timeout_s),
            "effective_max_tokens": int(self.effective_max_tokens),
            "temperature": float(self.temperature),
            "thinking_param": _repr_safe(self.thinking_param),
            "system_with_cache_repr": _repr_safe(
                self.system_with_cache,
            ),
            "messages_len": messages_len,
            "is_tool_round": bool(self.is_tool_round),
            "prompt_chars": int(self.prompt_chars),
            "call_start": float(self.call_start),
            "stream_callback_repr": _repr_safe(self.stream_callback),
        }


__all__ = [
    "CLAUDE_DISPATCH_STATE_SCHEMA_VERSION",
    "CLAUDE_STREAM_CONTEXT_SCHEMA_VERSION",
    "_CLAUDE_DISPATCH_STATE_FIELD_NAMES",
    "_CLAUDE_STREAM_CONTEXT_FIELD_NAMES",
    "_ClaudeDispatchState",
    "_ClaudeStreamContext",
    "_CumulativeCost",
]
