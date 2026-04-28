"""Phase 1 Slice 1.3 — Phase capture integration helper.

The PRODUCTION-CALLSITE adapter for the Determinism Substrate.

Phase 1 layering recap:

  * Slice 1.1 — entropy + clock primitives
  * Slice 1.2 — DecisionRuntime + ``decide(...)`` integration runtime
  * Slice 1.3 (THIS module) — phase-runner-shaped wrapper that
    decorates production decision sites with capture semantics.

Slice 1.2 ships ``decide(...)`` as the universal record/replay/
verify integration. Slice 1.3 ships ``capture_phase_decision(...)`` —
a phase-runner-shaped adapter that:

  1. Adds the master-flag short-circuit for cheap PASSTHROUGH.
  2. Builds a canonical ``inputs`` dict from common phase ctx fields
     so every wired phase emits a consistent shape.
  3. Coerces non-JSON-serializable outputs (Enum, dataclass, tuple)
     into canonical-friendly representations via the registered
     adapter.
  4. Provides a dynamic registry so each phase + kind has explicit
     adapters (not hardcoded enum). New decision kinds register at
     module load.

The wiring discipline at production callsites:

    from backend.core.ouroboros.governance.determinism.phase_capture import (
        capture_phase_decision,
    )

    # Existing code:
    route, reason = router.classify(ctx)

    # Slice 1.3 wiring (one async call):
    captured = await capture_phase_decision(
        op_id=ctx.op_id,
        phase="ROUTE",
        kind="route_assignment",
        ctx=ctx,
        compute=lambda: router.classify(ctx),
        output_adapter=route_adapter,
    )
    route, reason = captured

Master flag ``JARVIS_DETERMINISM_PHASE_CAPTURE_ENABLED`` (default
false until Phase 1 graduation). When off, ``capture_phase_decision``
short-circuits to a pure call of ``compute()`` — bit-for-bit legacy.

NEVER imports orchestrator / phase_runner / candidate_generator.
NEVER raises out of any public method.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional, Tuple

from backend.core.ouroboros.governance.determinism.decision_runtime import (
    LedgerMode,
    decide,
    ledger_enabled as _ledger_enabled,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Master flag — independent of the underlying ledger flag
# ---------------------------------------------------------------------------


def phase_capture_enabled() -> bool:
    """``JARVIS_DETERMINISM_PHASE_CAPTURE_ENABLED`` (default ``true`` —
    graduated in Phase 1 Slice 1.5).

    Independent from ``JARVIS_DETERMINISM_LEDGER_ENABLED`` (Slice 1.2)
    so operators can record decisions WITHOUT firing the phase capture
    wrappers (shadow recording on the runtime layer only) OR enable
    phase capture WITHOUT fully enabling the ledger (PASSTHROUGH mode
    for the wrappers themselves — no-op everywhere).

    Both flags must be ``true`` for capture to actually record; if
    either is off, ``capture_phase_decision`` is a pure passthrough.

    Re-read at call time so monkeypatch works in tests + operators
    can flip live without re-init. Hot-revert path: ``export
    JARVIS_DETERMINISM_PHASE_CAPTURE_ENABLED=false`` returns the
    wrappers to pure passthrough — production callsites continue to
    work with bit-for-bit legacy behavior."""
    raw = os.environ.get(
        "JARVIS_DETERMINISM_PHASE_CAPTURE_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated default
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Output adapter registry — dynamic, no hardcoded enum
# ---------------------------------------------------------------------------


# An output adapter takes whatever ``compute()`` returned and produces:
#   1. A JSON-serializable representation for storage
#   2. A "rehydrate" function that reconstructs the original shape
#      when REPLAY returns the stored representation
#
# Adapters are explicit per (phase, kind) — phases register their own
# at module load. We use this two-step pattern so REPLAY can produce
# the SAME rich Python object (e.g., ProviderRoute enum) instead of
# a generic dict, making the capture transparent to callers.
@dataclass(frozen=True)
class OutputAdapter:
    """Bidirectional serializer for one phase's decision output.

    Attributes
    ----------
    serialize : Callable[[Any], Any]
        Convert the live output into a JSON-safe representation.
        Result must be JSON-serializable via canonical_serialize.
    deserialize : Callable[[Any], Any]
        Reconstruct the original output shape from the JSON-safe
        representation. Used by REPLAY to return the same Python
        object the caller would have gotten from compute().
    name : str
        Human-readable identifier for telemetry.
    """
    serialize: Callable[[Any], Any]
    deserialize: Callable[[Any], Any]
    name: str = "default"


# Identity adapter — passthrough for outputs that are already
# JSON-friendly (str, int, float, bool, None, list, dict of those).
_IDENTITY_ADAPTER = OutputAdapter(
    serialize=lambda x: x,
    deserialize=lambda x: x,
    name="identity",
)


_adapter_registry: Dict[Tuple[str, str], OutputAdapter] = {}


def register_adapter(
    *, phase: str, kind: str, adapter: OutputAdapter,
) -> None:
    """Register an adapter for ``(phase, kind)``. Idempotent —
    re-registering the same key with the same adapter is a no-op;
    re-registering with a DIFFERENT adapter logs a warning + replaces
    (operators tweaking adapters during dev shouldn't be silently
    ignored). NEVER raises."""
    if not phase or not kind:
        return
    key = (str(phase), str(kind))
    existing = _adapter_registry.get(key)
    if existing is not None and existing is not adapter:
        logger.info(
            "[determinism] adapter for (%s, %s) replaced "
            "(was=%s, new=%s)",
            phase, kind, existing.name, adapter.name,
        )
    _adapter_registry[key] = adapter


def get_adapter(*, phase: str, kind: str) -> OutputAdapter:
    """Return the registered adapter for ``(phase, kind)`` or the
    identity passthrough if none registered. NEVER raises."""
    return _adapter_registry.get(
        (str(phase), str(kind)), _IDENTITY_ADAPTER,
    )


def iter_registered() -> Tuple[Tuple[str, str], ...]:
    """Snapshot of all registered ``(phase, kind)`` pairs.
    Diagnostic — used by ``/determinism adapters`` REPL surface."""
    return tuple(sorted(_adapter_registry.keys()))


def reset_registry_for_tests() -> None:
    """Drop all registered adapters. Production code MUST NOT call
    this. Tests use it to isolate adapter registration between
    test functions."""
    _adapter_registry.clear()


# ---------------------------------------------------------------------------
# Common ctx → inputs canonicalization
# ---------------------------------------------------------------------------


def _build_ctx_inputs(
    ctx: Any, *, extra: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a canonical inputs dict from common ctx fields.

    Phase callsites that have an ``OperationContext``-shaped object
    can pass it directly; this helper extracts the standard fields
    that downstream decisions branch on (signal_urgency,
    signal_source, task_complexity, target_files, cross_repo).
    Extra phase-specific inputs merge on top.

    Defensive: missing attrs return empty strings / empty lists.
    NEVER raises."""
    out: Dict[str, Any] = {}
    if ctx is not None:
        try:
            out["signal_urgency"] = (
                getattr(ctx, "signal_urgency", "") or ""
            )
            out["signal_source"] = (
                getattr(ctx, "signal_source", "") or ""
            )
            out["task_complexity"] = (
                getattr(ctx, "task_complexity", "") or ""
            )
            out["cross_repo"] = bool(getattr(ctx, "cross_repo", False))
            out["is_read_only"] = bool(
                getattr(ctx, "is_read_only", False),
            )
            tfs = getattr(ctx, "target_files", None)
            if tfs:
                # Normalize to a sorted tuple of strings. Same
                # canonical hash regardless of original ordering.
                try:
                    out["target_files"] = sorted(
                        str(f) for f in tfs
                    )
                except (TypeError, ValueError):
                    out["target_files"] = []
            else:
                out["target_files"] = []
        except Exception:  # noqa: BLE001 — defensive
            pass
    if extra:
        try:
            for k, v in extra.items():
                out[str(k)] = v
        except Exception:  # noqa: BLE001 — defensive
            pass
    return out


# ---------------------------------------------------------------------------
# capture_phase_decision — the public phase-shaped wrapper
# ---------------------------------------------------------------------------


async def capture_phase_decision(
    *,
    op_id: str,
    phase: str,
    kind: str,
    ctx: Any = None,
    compute: Callable[[], Any],
    extra_inputs: Optional[Mapping[str, Any]] = None,
    output_adapter: Optional[OutputAdapter] = None,
) -> Any:
    """Phase-runner-shaped wrapper around Slice 1.2's ``decide(...)``.

    Behavior:
      * If either master flag is OFF → call ``compute()`` and return
        its result. Bit-for-bit legacy. Negligible overhead (one env
        var read + one truthy check).
      * If both flags ON → dispatch through ``decide(...)`` with the
        canonicalized ctx-inputs, registered output adapter, and
        full RECORD/REPLAY/VERIFY semantics.

    Parameters
    ----------
    op_id : str
        The operation identifier from the OperationContext.
    phase : str
        Phase name (e.g., ``"ROUTE"``, ``"CLASSIFY"``, ``"GENERATE"``).
    kind : str
        Decision kind within the phase (e.g., ``"route_assignment"``).
        Free-form string — register an adapter via ``register_adapter``
        if your output isn't JSON-friendly.
    ctx : Any, optional
        OperationContext (or duck-typed equivalent). Used to extract
        common decision inputs (signal_urgency, etc.).
    compute : Callable[[], Any]
        The decision function. Sync, sync-returning-awaitable, or
        async — Slice 1.2's ``decide`` handles all three.
    extra_inputs : Mapping[str, Any], optional
        Additional inputs that aren't on ctx (phase-specific
        signals). Merged on top of ctx-extracted inputs.
    output_adapter : OutputAdapter, optional
        Override the registered adapter for this call only.
        Useful for one-off decision shapes.

    Returns
    -------
    Any
        The decision output, in its original shape (post-deserialize
        when in REPLAY mode and the output had a non-trivial adapter).

    NEVER raises beyond what compute() raises in PASSTHROUGH mode,
    or DecisionMismatchError in VERIFY-strict mode."""
    # Fast path — both flags must be on for capture to engage. The
    # ledger flag governs whether decide() does anything; the
    # capture flag governs whether the wrapper engages at all. Two
    # gates so operators can rollback either independently.
    if not phase_capture_enabled() or not _ledger_enabled():
        return await _call_compute(compute)

    adapter = output_adapter or get_adapter(phase=phase, kind=kind)
    inputs = _build_ctx_inputs(ctx, extra=extra_inputs)

    # Wrap compute to apply the output adapter on the live path.
    async def _adapted_compute() -> Any:
        live = await _call_compute(compute)
        try:
            return adapter.serialize(live)
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning(
                "[determinism] phase_capture serialize failed for "
                "phase=%s kind=%s: %s — falling back to identity",
                phase, kind, exc,
            )
            return live  # storage may fail, caller still gets value

    serialized = await decide(
        op_id=op_id, phase=phase, kind=kind,
        inputs=inputs, compute=_adapted_compute,
    )

    # Apply the deserialize step so the caller gets back the original
    # shape (e.g., ProviderRoute enum, not raw string). REPLAY mode
    # returns the stored serialized form; deserialize reconstitutes.
    try:
        return adapter.deserialize(serialized)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.warning(
            "[determinism] phase_capture deserialize failed for "
            "phase=%s kind=%s: %s — returning raw stored repr",
            phase, kind, exc,
        )
        return serialized


async def _call_compute(compute: Callable[[], Any]) -> Any:
    """Same flexible call pattern as Slice 1.2's _maybe_await but
    duplicated here to avoid coupling on a private helper. Accepts
    sync, sync-returning-awaitable, or async."""
    import asyncio
    result = compute()
    if asyncio.iscoroutine(result) or hasattr(result, "__await__"):
        return await result
    return result


__all__ = [
    "OutputAdapter",
    "capture_phase_decision",
    "get_adapter",
    "iter_registered",
    "phase_capture_enabled",
    "register_adapter",
    "reset_registry_for_tests",
]
