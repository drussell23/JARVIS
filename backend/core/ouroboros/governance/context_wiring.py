"""
Context preservation auto-wiring — Slice 3 of Production Integration.
======================================================================

Two adapters that close the "ledger events flow into intent + pins"
loop operators currently have to wire by hand:

* :func:`bridge_ledger_to_tracker` — every ledger entry automatically
  feeds the matching :class:`IntentTracker`. File-read entries bump
  path signals; error entries contribute both the error term and any
  path in ``where``; question entries fan out over ``related_paths``
  / ``related_tools``; decision entries carry ``approved_paths``.

* :func:`bridge_ledger_to_pins` — a short allowlist of ledger event
  kinds triggers an auto-pin on the pin registry. Only ``error`` (when
  status=``open``), ``decision`` (when outcome=``approved``), and
  ``question`` (when status=``open``) auto-pin. The caller supplies a
  :class:`ChunkIdResolver` that maps a ledger entry_id to a conversation
  chunk_id; when no resolver is provided the bridge pins the ledger
  entry_id directly (useful when entry IDs are themselves chunk IDs).

Discipline preserved from Slice 1 + 2
-------------------------------------

* §1 Authority — neither bridge grants new authority. Intent signals
  are advisory; pins are explicitly authored by the orchestrator via
  ``PinSource.ORCHESTRATOR`` (see :mod:`context_pins`).
* §5 — deterministic. No LLM calls, no network I/O.
* §7 — fail-closed. Listener exceptions are swallowed; bridges never
  raise into the ledger/pin write path.
* §8 — every auto-pin writes an INFO log line (inherited from the
  pin registry) so the audit trail shows which ledger event caused it.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional, Protocol, runtime_checkable

logger = logging.getLogger("Ouroboros.ContextWiring")


# ---------------------------------------------------------------------------
# ChunkIdResolver — optional mapping from ledger entry → conversation chunk
# ---------------------------------------------------------------------------


@runtime_checkable
class ChunkIdResolver(Protocol):
    """Maps a ledger entry_id (or referenced path) to a chunk_id.

    Implementations are caller-supplied; the bridge treats a ``None``
    return as "no chunk to pin, skip". When no resolver is supplied at
    all, the bridge pins the ledger entry_id as the chunk_id — useful
    for orchestrators that identify chunks by entry_id natively.
    """

    def resolve(self, *, entry_id: str, kind: str, projection: Dict[str, Any]) -> Optional[str]: ...


class _EntryIdPassthroughResolver:
    """Default resolver: uses the ledger entry_id as the chunk_id."""

    def resolve(
        self,
        *,
        entry_id: str,
        kind: str,
        projection: Dict[str, Any],
    ) -> Optional[str]:
        _ = (kind, projection)
        return entry_id or None


# ---------------------------------------------------------------------------
# bridge_ledger_to_tracker
# ---------------------------------------------------------------------------


def bridge_ledger_to_tracker(
    *,
    ledger: Any,
    tracker: Any,
) -> Callable[[], None]:
    """Subscribe ledger.on_change; feed every entry to tracker.ingest_ledger_entry.

    Returns an unsubscribe callback. Listener exceptions are logged at
    DEBUG and swallowed — ledger writes never stall on tracker state.
    """

    def _listener(payload: Dict[str, Any]) -> None:
        projection = payload.get("projection") or {}
        if not projection:
            return
        try:
            tracker.ingest_ledger_entry(projection)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[ContextWiring] tracker ingest failed: %s", exc,
            )

    unsub = ledger.on_change(_listener)
    logger.info(
        "[ContextWiring] bridge_ledger_to_tracker attached op=%s",
        getattr(ledger, "op_id", "?"),
    )
    return unsub


# ---------------------------------------------------------------------------
# bridge_ledger_to_pins
# ---------------------------------------------------------------------------


# Allowlist of (kind, trigger_predicate) pairs that warrant an auto-pin.
def _error_trigger(projection: Dict[str, Any]) -> bool:
    return projection.get("kind") == "error" and \
        projection.get("status", "open") == "open"


def _decision_trigger(projection: Dict[str, Any]) -> bool:
    return projection.get("kind") == "decision" and \
        projection.get("outcome") == "approved"


def _question_trigger(projection: Dict[str, Any]) -> bool:
    return projection.get("kind") == "question" and \
        projection.get("status", "open") == "open"


def bridge_ledger_to_pins(
    *,
    ledger: Any,
    pins: Any,
    resolver: Optional[ChunkIdResolver] = None,
) -> Callable[[], None]:
    """Subscribe ledger.on_change; auto-pin on open-error / approved-decision / open-question.

    Returns an unsubscribe callback.

    Only the three trigger kinds above auto-pin. ``file_read`` and
    ``tool_call`` entries — which far outnumber the interesting ones —
    do NOT pin (they'd saturate the pin registry's bounded cap and
    crowd out genuinely important chunks).
    """
    active_resolver: ChunkIdResolver = (
        resolver if resolver is not None else _EntryIdPassthroughResolver()
    )

    def _listener(payload: Dict[str, Any]) -> None:
        projection = payload.get("projection") or {}
        if not projection:
            return
        kind = projection.get("kind", "")
        entry_id = projection.get("entry_id", "") or \
            payload.get("entry_id", "")
        try:
            chunk_id = active_resolver.resolve(
                entry_id=entry_id, kind=kind, projection=projection,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[ContextWiring] resolver raised: %s", exc,
            )
            return
        if not chunk_id:
            return
        try:
            if _error_trigger(projection):
                pins.auto_pin_for_error(
                    chunk_id=chunk_id,
                    ledger_entry_id=entry_id,
                    error_class=projection.get("error_class", "unknown"),
                )
            elif _decision_trigger(projection):
                pins.auto_pin_for_decision(
                    chunk_id=chunk_id,
                    ledger_entry_id=entry_id,
                    decision_type=projection.get("decision_type", "unknown"),
                )
            elif _question_trigger(projection):
                pins.auto_pin_for_question(
                    chunk_id=chunk_id,
                    ledger_entry_id=entry_id,
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[ContextWiring] pin adapter raised: %s", exc,
            )

    unsub = ledger.on_change(_listener)
    logger.info(
        "[ContextWiring] bridge_ledger_to_pins attached op=%s",
        getattr(ledger, "op_id", "?"),
    )
    return unsub


# ---------------------------------------------------------------------------
# Composite attach helper
# ---------------------------------------------------------------------------


def attach_preservation_wiring(
    *,
    ledger: Any,
    tracker: Any,
    pins: Any,
    resolver: Optional[ChunkIdResolver] = None,
) -> Callable[[], None]:
    """One-call composite. Attaches both bridges; returns composite unsub."""
    un1 = bridge_ledger_to_tracker(ledger=ledger, tracker=tracker)
    un2 = bridge_ledger_to_pins(ledger=ledger, pins=pins, resolver=resolver)

    def _unsub_all() -> None:
        for u in (un1, un2):
            try:
                u()
            except Exception:  # noqa: BLE001
                pass

    return _unsub_all


__all__ = [
    "ChunkIdResolver",
    "attach_preservation_wiring",
    "bridge_ledger_to_pins",
    "bridge_ledger_to_tracker",
]
