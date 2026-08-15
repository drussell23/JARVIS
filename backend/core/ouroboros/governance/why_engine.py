"""why_engine — the causal account, as a projection. PRD §27.5.

For a proactive organism, *"why did you do that"* is the primary human
question. In Claude Code it never arises, because the human asked. Here the
organism acts on its own initiative, and without an answer every proactive
act is trusted blindly or refused blindly — **neither of which teaches
anybody anything**, not the operator and not the preference memory.

FOUR BANDS, NOT A DUMP
----------------------
An explanation that is a JSON blob is an explanation nobody reads. The
answer has a fixed shape, and it is the shape of the decision itself::

    TRIGGER   what woke the organism        (sensor, signal, or a parent op)
    CONTEXT   what it knew when it decided  (recall, index, narrative)
    LOGIC     how it reasoned               (plan, tool rounds, gate verdicts)
    ACTION    what it did                   (apply, verify, commit)

Fixed order, always all four, each stating *unknown* rather than being
omitted. A band that vanished when empty would make "it had no context" and
"we did not record the context" render identically, and those are different
answers to the operator's question.

DETERMINISTIC, AND FREE
-----------------------
Every value is read from the spine and its timeline projection. **No model
call.** Two properties follow, and both are requirements rather than
optimisations:

* it answers when every provider lane is dry — which is precisely the state
  in which an operator most wants to ask;
* it cannot hallucinate a rationale, because it is a projection of the
  ledger rather than a generation from it.

Model-authored prose may decorate this later. It may never be the mechanism.

THIS MODULE PARSES NOTHING
--------------------------
``transcript_spine`` owns records and eviction. ``transcript_timeline`` owns
the apply/verify/commit fold. ``transcript_log`` owns frame recovery and
already reports *where* a log stops via typed ``stop_reason`` / ``stop_frame``.
This module joins those three and adds no fourth reader — a second parser
would be a second opinion about the same bytes.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import enum
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.WhyEngine")

WHY_ENGINE_SCHEMA_VERSION: str = "why_engine.1"


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(minimum, int(raw))
    except (TypeError, ValueError):
        return default


def max_lineage_depth() -> int:
    """How far back a single ``/why`` walks. Default 1 — one hop.

    §27.5.5's decision: steering acts must stay cheap to READ. An
    explanation that unrolled an eight-deep ancestry into the terminal on
    every query would be skipped, and a skipped explanation is worse than a
    short one because it looks like it was offered.

    ``/why <ref> --full`` raises this; the drill-down path is the default.
    """
    return _env_int("JARVIS_WHY_LINEAGE_DEPTH", 1)


def max_band_items() -> int:
    """Items rendered per band before the tail is summarised."""
    return _env_int("JARVIS_WHY_BAND_ITEMS", 5)


class Certainty(str, enum.Enum):
    """How well a band is known. The distinction the operator needs.

    ``OBSERVED``  read from a surviving record.
    ``TOMBSTONE`` the record existed and was evicted — the spine says so.
    ``UNKNOWN``   never recorded, or the log ends before it.

    Collapsing TOMBSTONE into UNKNOWN would tell an operator "we don't know"
    about something the system *did* know and then discarded on a retention
    policy they can change. Those call for different actions.
    """

    OBSERVED = "observed"
    TOMBSTONE = "tombstone"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Band:
    """One of the four causal bands."""

    name: str
    certainty: Certainty
    items: Tuple[str, ...] = ()
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "certainty": self.certainty.value,
                "items": list(self.items), "detail": self.detail}


@dataclass(frozen=True)
class Explanation:
    """One op's causal account, ready to render."""

    ref: str
    op_id: str
    bands: Tuple[Band, ...]
    #: Ancestors, nearest first. Empty when this op is a root.
    lineage: Tuple[str, ...] = ()
    lineage_truncated: bool = False
    #: True when the answer includes live in-memory state, not only disk.
    in_flight: bool = False
    #: Where the durable log stops, when it stops early. From
    #: ``transcript_log.recover_log``'s typed reason — never re-derived.
    loss_point: str = ""
    partial: bool = False
    schema_version: str = WHY_ENGINE_SCHEMA_VERSION

    def band(self, name: str) -> Optional[Band]:
        return next((b for b in self.bands if b.name == name), None)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version, "ref": self.ref,
            "op_id": self.op_id, "bands": [b.to_dict() for b in self.bands],
            "lineage": list(self.lineage),
            "lineage_truncated": self.lineage_truncated,
            "in_flight": self.in_flight, "loss_point": self.loss_point,
            "partial": self.partial,
        }


BAND_ORDER: Tuple[str, ...] = ("trigger", "context", "logic", "action")


class RefNotFound(LookupError):
    """The ref resolves nowhere — on disk or in flight."""


def _spine() -> Optional[Any]:
    try:
        from backend.core.ouroboros.battle_test.transcript_spine import (
            get_default_spine,
        )
        return get_default_spine()
    except Exception:  # noqa: BLE001
        return None


def _live_ops() -> Dict[str, Any]:
    """Ops currently executing, from the in-memory registry.

    THE IN-FLIGHT RACE. An operator asks about the thing they are watching,
    which is by definition the thing least likely to have reached disk. A
    ``/why`` that answered "not found" for the op on screen would be useless
    at exactly the moment it is most wanted.

    Injected rather than imported: the live registry belongs to
    ``GovernedLoopService``, and reaching into the orchestrator from a
    read-only projection would invert the authority boundary — the same
    reason ``proactive_mode`` takes its pool through a sink.
    """
    try:
        return dict(_LIVE_SOURCE() or {}) if _LIVE_SOURCE else {}
    except Exception:  # noqa: BLE001
        return {}


_LIVE_SOURCE: Optional[Callable[[], Dict[str, Any]]] = None


def set_live_source(source: Optional[Callable[[], Dict[str, Any]]]) -> None:
    """Register the in-flight op registry. NEVER raises.

    Absent, ``/why`` answers from disk alone and says so — a degraded answer
    that names its own limit, rather than a confident one that is missing
    the live half.
    """
    global _LIVE_SOURCE
    _LIVE_SOURCE = source


def _resolve_ref(ref: str) -> Tuple[str, Optional[Any], bool]:
    """``(op_id, record, in_flight)``. Raises :class:`RefNotFound`.

    Accepts either a spine ref (``o-12``, ``d-7``, ``m-3``) or a bare
    ``op_id``, because an operator reading a log has the op id and an
    operator reading the deck has the ref, and demanding they know which is
    which would be the interface asking the human to do a lookup.
    """
    token = str(ref or "").strip()
    if not token:
        raise RefNotFound("no reference given")
    spine = _spine()
    rec = None
    if spine is not None:
        try:
            rec = spine.resolve(token)
        except Exception:  # noqa: BLE001
            rec = None
    if rec is not None:
        return str(getattr(rec, "op_id", "") or token), rec, False

    live = _live_ops()
    if token in live:
        return token, None, True
    # A ref the spine has EVICTED is not "not found" — see band tombstones.
    if spine is not None:
        try:
            if spine.was_evicted(token):
                return token, None, False
        except Exception:  # noqa: BLE001
            pass
    # Last chance: the token may be an op_id whose records survive.
    if spine is not None:
        try:
            for r in spine.page(limit=None):
                if str(getattr(r, "op_id", "")) == token:
                    return token, r, False
        except Exception:  # noqa: BLE001
            pass
    raise RefNotFound(
        f"{token!r} resolves to nothing on disk or in flight — it may have "
        f"been evicted beyond the spine's retention")


def _loss_point() -> str:
    """Where the durable transcript stops, if it stops early.

    Reads ``recover_log``'s TYPED stop reason rather than inferring one.
    That function already returns the longest valid prefix plus
    ``stop_reason`` and ``stop_frame``, which is the whole corrupted-lineage
    requirement: the log says where it ends, so this reports rather than
    guesses.
    """
    try:
        from pathlib import Path

        from backend.core.ouroboros.battle_test.transcript_log import (
            recover_log,
        )
        session = os.environ.get("JARVIS_OUROBOROS_SESSION_DIR", "")
        if not session:
            return ""
        result = recover_log(Path(session) / "transcript.log")
        if getattr(result, "clean", True):
            return ""
        reason = getattr(getattr(result, "stop_reason", None), "value",
                         str(getattr(result, "stop_reason", "")))
        frame = getattr(result, "stop_frame", 0)
        trailing = getattr(result, "trailing_bytes", 0)
        return (f"transcript ends at frame {frame} ({reason}); "
                f"{trailing} byte(s) beyond are unreadable")
    except Exception:  # noqa: BLE001
        return ""


def _band_from(name: str, items: List[str], evicted: bool,
               detail: str = "") -> Band:
    """Build a band, choosing the certainty the evidence supports."""
    if items:
        return Band(name, Certainty.OBSERVED, tuple(items[:max_band_items()]),
                    detail)
    if evicted:
        return Band(name, Certainty.TOMBSTONE, (),
                    detail or "records existed and were evicted by retention")
    return Band(name, Certainty.UNKNOWN, (), detail or "never recorded")


def _collect(op_id: str, spine: Optional[Any]) -> Dict[str, List[str]]:
    """Records for one op, bucketed by band. Never raises."""
    buckets: Dict[str, List[str]] = {b: [] for b in BAND_ORDER}
    if spine is None:
        return buckets
    #: Which spine vocabulary answers which band. Data, not branching, so a
    #: sixth namespace lands here rather than in four if-statements.
    routing = {
        "op_block": "trigger", "narrative": "context",
        "tool_render": "logic", "diff": "action", "milestone": "action",
    }
    try:
        for rec in spine.page(limit=None):
            if str(getattr(rec, "op_id", "")) != op_id:
                continue
            band = routing.get(str(getattr(rec, "kind", "")), "logic")
            ref = str(getattr(rec, "ref", ""))
            payload = getattr(rec, "payload", None)
            label = ref
            if isinstance(payload, dict):
                for key in ("summary", "kind", "title", "reason", "text"):
                    val = payload.get(key)
                    if isinstance(val, str) and val.strip():
                        label = f"{ref}  {val.strip()[:72]}"
                        break
            buckets[band].append(label)
    except Exception:  # noqa: BLE001
        logger.debug("[Why] spine walk degraded", exc_info=True)
    return buckets


def _lineage(op_id: str, live: Dict[str, Any], depth: int) -> Tuple[
        Tuple[str, ...], bool]:
    """Ancestors nearest-first, bounded. ``(lineage, truncated)``.

    THE CHAIN REACTION. A background loop can produce A→B→C→D, and an
    operator asking about D wants to reach A. Rendering the whole ancestry
    on every query would flood the terminal, so the default is ONE hop with
    the next ancestor named — the drill-down is the operator following it,
    not the tool deciding for them.

    A cycle is possible if an op-id were ever reused, so the walk carries a
    seen-set. It terminates on depth, on a root, or on a repeat.
    """
    out: List[str] = []
    seen = {op_id}
    cursor = op_id
    truncated = False
    for _ in range(max(0, depth) + 1):
        parent = _parent_of(cursor, live)
        if not parent or parent in seen:
            break
        if len(out) >= depth:
            truncated = True
            break
        out.append(parent)
        seen.add(parent)
        cursor = parent
    return tuple(out), truncated


def _parent_of(op_id: str, live: Dict[str, Any]) -> str:
    """The op that caused this one, or "". Never raises."""
    entry = live.get(op_id)
    if isinstance(entry, dict):
        val = entry.get("parent_op_id")
        if isinstance(val, str) and val.strip():
            return val.strip()
    spine = _spine()
    if spine is None:
        return ""
    try:
        for rec in spine.page(limit=None):
            if str(getattr(rec, "op_id", "")) != op_id:
                continue
            payload = getattr(rec, "payload", None)
            if isinstance(payload, dict):
                val = payload.get("parent_op_id")
                if isinstance(val, str) and val.strip():
                    return val.strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


def explain(ref: str, *, depth: Optional[int] = None) -> Explanation:
    """The causal account for one ref or op-id. Raises :class:`RefNotFound`.

    Joins three sources and says which it used: the spine's surviving
    records, the timeline projection's apply/verify/commit fold, and — when
    the op is still running — the live registry.
    """
    op_id, _rec, in_flight = _resolve_ref(ref)
    spine = _spine()
    live = _live_ops()
    in_flight = in_flight or op_id in live

    buckets = _collect(op_id, spine)
    evicted_any = False
    if spine is not None:
        try:
            evicted_any = bool(getattr(spine, "evicted_count", 0)) or \
                spine.was_evicted(str(ref))
        except Exception:  # noqa: BLE001
            evicted_any = False

    # The ACTION band prefers the timeline projection: it is the canonical
    # fold of apply/verify/commit, and re-deriving those from raw milestone
    # records here would be a second opinion about the same three callbacks.
    row = None
    try:
        from backend.core.ouroboros.battle_test.transcript_timeline import (
            timeline_for_op,
        )
        row = timeline_for_op(op_id)
    except Exception:  # noqa: BLE001
        row = None
    if row is not None:
        summary = []
        if getattr(row, "has_apply", False):
            summary.append(f"applied {row.apply_files} file(s) "
                           f"[{row.apply_mode or 'mode?'}]")
        if getattr(row, "has_verify", False):
            summary.append(f"verified {row.verify_passed}/{row.verify_total}"
                           + ("" if row.verify_scoped_to_op
                              else " (repo-wide, not this op)"))
        if getattr(row, "commit_hash", ""):
            summary.append(f"committed {row.commit_hash[:10]}")
        if summary:
            buckets["action"] = summary + buckets["action"]

    if in_flight:
        entry = live.get(op_id) or {}
        phase = str((entry or {}).get("phase", "") or "running")
        buckets["logic"].insert(0, f"IN FLIGHT — currently {phase}")

    bands = tuple(
        _band_from(name, buckets[name], evicted_any)
        for name in BAND_ORDER
    )
    lineage, truncated = _lineage(
        op_id, live, max_lineage_depth() if depth is None else max(0, depth))
    return Explanation(
        ref=str(ref), op_id=op_id, bands=bands, lineage=lineage,
        lineage_truncated=truncated, in_flight=in_flight,
        loss_point=_loss_point(),
        partial=bool(getattr(row, "partial", False)) or evicted_any,
    )
