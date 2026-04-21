"""
ContextPinRegistry — Slice 3 of the Context Preservation arc.
==============================================================

Explicit "keep this no matter what" layer on top of Slice 2's
intent-aware scoring. Pinned chunks get :data:`math.inf` score, which
means they survive every compaction pass regardless of recency or
intent drift.

Design pillars
--------------

* **§1 Authority** — pins are authored by the OPERATOR (via REPL) or
  the ORCHESTRATOR (auto-pin on specific authorization events). The
  model CANNOT pin itself. Enforcement: :meth:`pin` takes an explicit
  ``source`` parameter and refuses ``model`` source at write time.
* **§7 Fail-closed** — every pin carries an explicit expiry timestamp
  (wall-clock). Forgotten pins auto-expire; nothing stays pinned
  forever by accident. A pin without a TTL is rejected.
* **§8 Observable** — every pin / unpin / expiry emits a listener
  event; Slice 4 bridges those to SSE + a manifest entry.
* **Auto-pin triggers** — a small, conservative set of ledger events
  auto-pin their associated chunks:
    - a new ``open`` ErrorEntry: pin the chunks that mention the
      error's file / message, TTL = 30 minutes
    - a ``DecisionEntry`` with outcome=``approved``: pin for 1 hour
      (so the blessed paths stay in-context while the op is active)
    - a newly-open ``QuestionEntry``: pin for 15 minutes (so the
      question stays in-context until answered or expired)

Registry shape
--------------

Per-op :class:`ContextPinRegistry` holds entries keyed by ``pin_id``.
The :class:`PreservationScorer` integration is purely read-through:
``scorer.score(candidate, intent)`` checks ``candidate.pinned`` (a
boolean set by the caller who consults the registry). The registry
does not reach into the scorer or mutate its scoring logic — it's a
lookup.

REPL surface
------------

``/pin <chunk-id> [reason]`` — operator pin, default TTL 1h
``/unpin <pin-id>`` — remove a pin before its TTL
``/pins`` — list active pins (one line each)
``/pins show <pin-id>`` — detail
``/pins clear`` — remove every non-auto pin
``/pins help`` — command reference
"""
from __future__ import annotations

import enum
import logging
import math
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.ContextPins")


PIN_REGISTRY_SCHEMA_VERSION: str = "context_pins.v1"


# ---------------------------------------------------------------------------
# Env knobs
# ---------------------------------------------------------------------------


def _default_operator_ttl_s() -> float:
    try:
        return max(60.0, float(os.environ.get(
            "JARVIS_CONTEXT_PIN_DEFAULT_TTL_S", "3600",  # 1 hour
        )))
    except (TypeError, ValueError):
        return 3600.0


def _auto_error_ttl_s() -> float:
    try:
        return max(60.0, float(os.environ.get(
            "JARVIS_CONTEXT_PIN_ERROR_TTL_S", "1800",  # 30 min
        )))
    except (TypeError, ValueError):
        return 1800.0


def _auto_decision_ttl_s() -> float:
    try:
        return max(60.0, float(os.environ.get(
            "JARVIS_CONTEXT_PIN_DECISION_TTL_S", "3600",
        )))
    except (TypeError, ValueError):
        return 3600.0


def _auto_question_ttl_s() -> float:
    try:
        return max(60.0, float(os.environ.get(
            "JARVIS_CONTEXT_PIN_QUESTION_TTL_S", "900",
        )))
    except (TypeError, ValueError):
        return 900.0


def _max_pins_per_op() -> int:
    try:
        return max(4, int(os.environ.get(
            "JARVIS_CONTEXT_PIN_MAX_PER_OP", "64",
        )))
    except (TypeError, ValueError):
        return 64


# ---------------------------------------------------------------------------
# Pin sources (§1 authority boundary)
# ---------------------------------------------------------------------------


class PinSource(str, enum.Enum):
    OPERATOR = "operator"
    ORCHESTRATOR = "orchestrator"
    # MODEL is deliberately NOT in the enum — writers validate and refuse.


_AUTHORITATIVE_SOURCES = frozenset({
    PinSource.OPERATOR, PinSource.ORCHESTRATOR,
})


# ---------------------------------------------------------------------------
# Pin entry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PinEntry:
    """One pinned chunk.

    ``chunk_id`` — whatever the caller uses to identify the chunk
    (mirrors :class:`ChunkCandidate.chunk_id`). One pin per chunk_id
    per op; re-pinning extends the TTL and overwrites the reason.

    ``kind`` — "operator" / "auto_error" / "auto_decision" / "auto_question".
    Operator pins are cleared by ``/pins clear``; auto pins are not.
    """

    pin_id: str
    op_id: str
    chunk_id: str
    source: str
    kind: str
    reason: str
    created_at_iso: str
    expires_at_iso: str
    linked_ledger_entry_id: str = ""
    schema_version: str = PIN_REGISTRY_SCHEMA_VERSION

    def expires_epoch(self) -> float:
        try:
            return datetime.fromisoformat(self.expires_at_iso).timestamp()
        except Exception:  # noqa: BLE001
            return 0.0

    def is_expired(self, *, now: Optional[float] = None) -> bool:
        ts = now if now is not None else time.time()
        return self.expires_epoch() <= ts


class PinError(Exception):
    """Raised on illegal pin operations."""


# ---------------------------------------------------------------------------
# ContextPinRegistry
# ---------------------------------------------------------------------------


class ContextPinRegistry:
    """Per-op pin store with TTL + listener hooks."""

    def __init__(
        self,
        op_id: str,
        *,
        max_pins: Optional[int] = None,
    ) -> None:
        if not op_id:
            raise ValueError("op_id must be non-empty")
        self._op_id = op_id
        self._cap = max_pins or _max_pins_per_op()
        self._lock = threading.Lock()
        # pin_id → PinEntry
        self._pins: Dict[str, PinEntry] = {}
        # chunk_id → pin_id (primary index for fast lookup)
        self._by_chunk: Dict[str, str] = {}
        self._listeners: List[Callable[[Dict[str, Any]], None]] = []

    # --- core operations -------------------------------------------------

    def pin(
        self,
        *,
        chunk_id: str,
        source: PinSource,
        ttl_s: Optional[float] = None,
        reason: str = "",
        kind: str = "operator",
        linked_ledger_entry_id: str = "",
    ) -> PinEntry:
        """Register a pin for *chunk_id*.

        Idempotent — re-pinning the same chunk replaces the existing
        entry (new TTL, new reason, new kind). Rejects malformed
        inputs and non-authoritative sources (§1).
        """
        if not chunk_id:
            raise PinError("chunk_id must be non-empty")
        if source not in _AUTHORITATIVE_SOURCES:
            raise PinError(
                f"pin source {source!r} not authoritative "
                "(only operator / orchestrator may pin)"
            )
        effective_ttl = ttl_s if ttl_s is not None and ttl_s > 0 \
            else _default_operator_ttl_s()
        now_iso = datetime.now(timezone.utc).replace(
            microsecond=0,
        ).isoformat()
        exp_iso = (
            datetime.now(timezone.utc)
            + timedelta(seconds=effective_ttl)
        ).replace(microsecond=0).isoformat()
        pin_id = (
            f"pin-"
            f"{abs(hash((chunk_id, now_iso, time.time_ns()))) & 0xFFFFFFFF:08x}"
        )
        entry = PinEntry(
            pin_id=pin_id,
            op_id=self._op_id,
            chunk_id=chunk_id,
            source=source.value,
            kind=kind,
            reason=(reason or "").strip()[:500],
            created_at_iso=now_iso,
            expires_at_iso=exp_iso,
            linked_ledger_entry_id=linked_ledger_entry_id,
        )
        with self._lock:
            # Replace any existing pin for this chunk_id.
            prior_pin_id = self._by_chunk.get(chunk_id)
            if prior_pin_id is not None:
                self._pins.pop(prior_pin_id, None)
            if len(self._pins) >= self._cap:
                # Evict the oldest NON-auto pin; if all are auto, evict
                # the globally-oldest (safety valve — auto-pins must
                # not starve operator pins).
                evictable = [
                    p for p in self._pins.values()
                    if p.kind == "operator"
                ]
                if not evictable:
                    evictable = list(self._pins.values())
                evictable.sort(key=lambda p: p.created_at_iso)
                oldest = evictable[0]
                self._pins.pop(oldest.pin_id, None)
                self._by_chunk.pop(oldest.chunk_id, None)
            self._pins[pin_id] = entry
            self._by_chunk[chunk_id] = pin_id
        self._fire("context_pinned", entry)
        logger.info(
            "[ContextPins] pinned op=%s chunk=%s kind=%s ttl_s=%.0f reason=%r",
            self._op_id, chunk_id, kind, effective_ttl, entry.reason[:80],
        )
        return entry

    def unpin(self, pin_id: str) -> Optional[PinEntry]:
        """Remove a pin by id. Returns the evicted entry or None."""
        with self._lock:
            entry = self._pins.pop(pin_id, None)
            if entry is not None:
                self._by_chunk.pop(entry.chunk_id, None)
        if entry is not None:
            self._fire("context_unpinned", entry)
            logger.info(
                "[ContextPins] unpinned op=%s pin=%s chunk=%s",
                self._op_id, pin_id, entry.chunk_id,
            )
        return entry

    def unpin_chunk(self, chunk_id: str) -> Optional[PinEntry]:
        with self._lock:
            pin_id = self._by_chunk.get(chunk_id)
        if pin_id is None:
            return None
        return self.unpin(pin_id)

    def clear_operator_pins(self) -> int:
        """Remove every ``operator`` kind pin. Auto pins survive."""
        with self._lock:
            victims = [
                p for p in self._pins.values() if p.kind == "operator"
            ]
        n = 0
        for p in victims:
            if self.unpin(p.pin_id) is not None:
                n += 1
        return n

    def is_pinned(self, chunk_id: str, *, now: Optional[float] = None) -> bool:
        with self._lock:
            pin_id = self._by_chunk.get(chunk_id)
            if pin_id is None:
                return False
            entry = self._pins.get(pin_id)
        if entry is None or entry.is_expired(now=now):
            return False
        return True

    def get(self, pin_id: str) -> Optional[PinEntry]:
        with self._lock:
            return self._pins.get(pin_id)

    def list_active(
        self, *, now: Optional[float] = None,
    ) -> List[PinEntry]:
        t = now if now is not None else time.time()
        with self._lock:
            out = [p for p in self._pins.values() if not p.is_expired(now=t)]
        out.sort(key=lambda p: p.created_at_iso, reverse=True)
        return out

    def prune_expired(self, *, now: Optional[float] = None) -> int:
        t = now if now is not None else time.time()
        expired: List[PinEntry] = []
        with self._lock:
            for pid, entry in list(self._pins.items()):
                if entry.is_expired(now=t):
                    self._pins.pop(pid, None)
                    self._by_chunk.pop(entry.chunk_id, None)
                    expired.append(entry)
        for entry in expired:
            self._fire("context_pin_expired", entry)
        return len(expired)

    # --- auto-pin triggers (orchestrator-driven) -------------------------

    def auto_pin_for_error(
        self,
        *,
        chunk_id: str,
        ledger_entry_id: str,
        error_class: str,
    ) -> PinEntry:
        return self.pin(
            chunk_id=chunk_id,
            source=PinSource.ORCHESTRATOR,
            ttl_s=_auto_error_ttl_s(),
            kind="auto_error",
            reason=f"open error: {error_class}",
            linked_ledger_entry_id=ledger_entry_id,
        )

    def auto_pin_for_decision(
        self,
        *,
        chunk_id: str,
        ledger_entry_id: str,
        decision_type: str,
    ) -> PinEntry:
        return self.pin(
            chunk_id=chunk_id,
            source=PinSource.ORCHESTRATOR,
            ttl_s=_auto_decision_ttl_s(),
            kind="auto_decision",
            reason=f"blessed decision: {decision_type}",
            linked_ledger_entry_id=ledger_entry_id,
        )

    def auto_pin_for_question(
        self,
        *,
        chunk_id: str,
        ledger_entry_id: str,
    ) -> PinEntry:
        return self.pin(
            chunk_id=chunk_id,
            source=PinSource.ORCHESTRATOR,
            ttl_s=_auto_question_ttl_s(),
            kind="auto_question",
            reason="open question awaiting answer",
            linked_ledger_entry_id=ledger_entry_id,
        )

    # --- listener hooks (Slice 4 bridges to SSE) -------------------------

    def on_change(
        self, listener: Callable[[Dict[str, Any]], None],
    ) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(listener)

        def _unsub() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return _unsub

    def _fire(self, event_type: str, entry: PinEntry) -> None:
        payload = {
            "event_type": event_type,
            "pin_id": entry.pin_id,
            "op_id": self._op_id,
            "projection": self._project(entry),
        }
        for l in list(self._listeners):
            try:
                l(payload)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "[ContextPins] listener exception on %s: %s",
                    event_type, exc,
                )

    @staticmethod
    def _project(entry: PinEntry) -> Dict[str, Any]:
        return {
            "pin_id": entry.pin_id,
            "op_id": entry.op_id,
            "chunk_id": entry.chunk_id,
            "source": entry.source,
            "kind": entry.kind,
            "reason": entry.reason,
            "created_at_iso": entry.created_at_iso,
            "expires_at_iso": entry.expires_at_iso,
            "linked_ledger_entry_id": entry.linked_ledger_entry_id,
        }

    @property
    def op_id(self) -> str:
        return self._op_id


# ---------------------------------------------------------------------------
# Registry-of-registries
# ---------------------------------------------------------------------------


class ContextPinRegistries:
    def __init__(self, *, max_ops: int = 64) -> None:
        self._lock = threading.Lock()
        self._by_op: Dict[str, ContextPinRegistry] = {}
        self._max_ops = max(4, max_ops)

    def get_or_create(self, op_id: str) -> ContextPinRegistry:
        if not op_id:
            raise ValueError("op_id must be non-empty")
        with self._lock:
            reg = self._by_op.get(op_id)
            if reg is not None:
                return reg
            if len(self._by_op) >= self._max_ops:
                oldest = next(iter(self._by_op))
                self._by_op.pop(oldest)
            fresh = ContextPinRegistry(op_id)
            self._by_op[op_id] = fresh
        return fresh

    def get(self, op_id: str) -> Optional[ContextPinRegistry]:
        with self._lock:
            return self._by_op.get(op_id)

    def drop(self, op_id: str) -> bool:
        with self._lock:
            return self._by_op.pop(op_id, None) is not None

    def reset(self) -> None:
        with self._lock:
            self._by_op.clear()


_default_pin_registries: Optional[ContextPinRegistries] = None
_pin_registries_lock = threading.Lock()


def get_default_pin_registries() -> ContextPinRegistries:
    global _default_pin_registries
    with _pin_registries_lock:
        if _default_pin_registries is None:
            _default_pin_registries = ContextPinRegistries()
        return _default_pin_registries


def reset_default_pin_registries() -> None:
    global _default_pin_registries
    with _pin_registries_lock:
        if _default_pin_registries is not None:
            _default_pin_registries.reset()
        _default_pin_registries = None


def pin_registry_for(op_id: str) -> ContextPinRegistry:
    return get_default_pin_registries().get_or_create(op_id)


# ---------------------------------------------------------------------------
# REPL dispatcher
# ---------------------------------------------------------------------------


@dataclass
class PinDispatchResult:
    ok: bool
    text: str
    matched: bool = True


_PIN_HELP = (
    "Context pin commands\n"
    "--------------------\n"
    "  /pin <chunk-id> [reason]    operator pin (default TTL 1h)\n"
    "  /unpin <pin-id>             remove a pin\n"
    "  /pins                       list active pins\n"
    "  /pins show <pin-id>         full detail\n"
    "  /pins clear                 remove every operator pin (auto-pins stay)\n"
    "  /pins prune                 expire stale pins now\n"
    "  /pins help                  this text\n"
)


_COMMANDS = frozenset({"/pin", "/unpin", "/pins"})


def _matches(line: str) -> bool:
    if not line:
        return False
    first = line.split(None, 1)[0]
    return first in _COMMANDS


def dispatch_pin_command(
    line: str,
    *,
    registry: Optional[ContextPinRegistry] = None,
    op_id: Optional[str] = None,
) -> PinDispatchResult:
    """One-call REPL dispatcher for pin commands.

    Either ``registry`` or ``op_id`` must be supplied so the dispatcher
    knows which per-op registry to consult. In production SerpentFlow
    injects ``op_id=active_op``; tests inject a direct registry.
    """
    if not _matches(line):
        return PinDispatchResult(ok=False, text="", matched=False)
    import shlex
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return PinDispatchResult(
            ok=False, text=f"  /pins: parse error: {exc}",
        )
    if not tokens:
        return PinDispatchResult(ok=False, text="", matched=False)

    if registry is None:
        if op_id:
            registry = pin_registry_for(op_id)
        else:
            return PinDispatchResult(
                ok=False, text="  /pins: no active op_id",
            )

    cmd = tokens[0]
    args = tokens[1:]

    if cmd == "/pin":
        return _handle_pin(registry, args)
    if cmd == "/unpin":
        return _handle_unpin(registry, args)
    if cmd == "/pins":
        return _handle_pins(registry, args)
    return PinDispatchResult(ok=False, text="", matched=False)


def _handle_pin(
    registry: ContextPinRegistry, args: List[str],
) -> PinDispatchResult:
    if not args:
        return PinDispatchResult(
            ok=False, text="  /pin <chunk-id> [reason]",
        )
    chunk_id = args[0]
    reason = " ".join(args[1:]).strip()
    try:
        entry = registry.pin(
            chunk_id=chunk_id, source=PinSource.OPERATOR, reason=reason,
        )
    except PinError as exc:
        return PinDispatchResult(ok=False, text=f"  /pin: {exc}")
    return PinDispatchResult(
        ok=True,
        text=f"  pinned: {entry.pin_id} chunk={entry.chunk_id} "
             f"expires={entry.expires_at_iso}",
    )


def _handle_unpin(
    registry: ContextPinRegistry, args: List[str],
) -> PinDispatchResult:
    if not args:
        return PinDispatchResult(ok=False, text="  /unpin <pin-id>")
    pin_id = args[0]
    entry = registry.unpin(pin_id)
    if entry is None:
        return PinDispatchResult(
            ok=False, text=f"  /unpin: unknown pin: {pin_id}",
        )
    return PinDispatchResult(
        ok=True, text=f"  unpinned: {entry.pin_id}",
    )


def _handle_pins(
    registry: ContextPinRegistry, args: List[str],
) -> PinDispatchResult:
    if not args:
        return _pins_list(registry)
    head = args[0]
    if head == "help":
        return PinDispatchResult(ok=True, text=_PIN_HELP)
    if head == "show":
        if len(args) < 2:
            return PinDispatchResult(
                ok=False, text="  /pins show <pin-id>",
            )
        return _pins_show(registry, args[1])
    if head == "clear":
        n = registry.clear_operator_pins()
        return PinDispatchResult(
            ok=True, text=f"  /pins cleared {n} operator pin(s)",
        )
    if head == "prune":
        n = registry.prune_expired()
        return PinDispatchResult(
            ok=True, text=f"  /pins pruned {n} expired pin(s)",
        )
    # `/pins <pin-id>` short-form
    return _pins_show(registry, head)


def _pins_list(registry: ContextPinRegistry) -> PinDispatchResult:
    active = registry.list_active()
    if not active:
        return PinDispatchResult(ok=True, text="  (no active pins)")
    lines: List[str] = [f"  Active pins ({len(active)}):"]
    for p in active:
        lines.append(
            f"  - {p.pin_id}  kind={p.kind:<14} chunk={p.chunk_id:<32} "
            f"expires={p.expires_at_iso}"
        )
    return PinDispatchResult(ok=True, text="\n".join(lines))


def _pins_show(
    registry: ContextPinRegistry, pin_id: str,
) -> PinDispatchResult:
    entry = registry.get(pin_id)
    if entry is None:
        return PinDispatchResult(
            ok=False, text=f"  /pins: unknown pin: {pin_id}",
        )
    lines = [
        f"  Pin {entry.pin_id}",
        f"    op         : {entry.op_id}",
        f"    chunk      : {entry.chunk_id}",
        f"    source     : {entry.source}",
        f"    kind       : {entry.kind}",
        f"    created_at : {entry.created_at_iso}",
        f"    expires_at : {entry.expires_at_iso}",
    ]
    if entry.reason:
        lines.append(f"    reason     : {entry.reason}")
    if entry.linked_ledger_entry_id:
        lines.append(f"    linked_ledger: {entry.linked_ledger_entry_id}")
    return PinDispatchResult(ok=True, text="\n".join(lines))


__all__ = [
    "ContextPinRegistries",
    "ContextPinRegistry",
    "PIN_REGISTRY_SCHEMA_VERSION",
    "PinEntry",
    "PinError",
    "PinSource",
    "PinDispatchResult",
    "dispatch_pin_command",
    "get_default_pin_registries",
    "pin_registry_for",
    "reset_default_pin_registries",
]

_ = (Tuple, math)  # silence unused-import guards
