"""SideChannel — a question the operator can ask WITHOUT taking the floor.

The root problem
----------------
O+V is proactive. It starts ops nobody asked for, and the operator watches
them happen. So the operator's most common utterance is not a command — it
is an aside: *"…why did that route to DoubleWord?"*, *"is that the same
file as before?"* Those are questions ABOUT the work in flight, asked
WHILE it is in flight.

The organism had exactly two things to do with such a line, and both are
wrong for an aside:

    /goal <text>   →  becomes an IntentEnvelope. The question turns into
                      WORK: an op, a provider spend, a patch. Asking
                      "why is this slow" must not schedule a refactor.

    /ask <text>    →  `_handle_ask` AWAITS `fast_path_qa.ask_question`
                      inline, on the body of the operator-input queue's
                      single consumer (`operator_input_queue._drain`).
                      For the whole provider round-trip — tens of seconds
                      on a cold Claude call — the NEXT line the operator
                      types cannot run. `/cancel` queues behind the
                      question about the thing they wanted to cancel.

So the only question surface blocks the only control surface, and the
only non-blocking surface converts questions into work. There was no
third thing: no lane on which a question is *subordinate* to the work
it is about.

That lane is this module. Three properties, and the feature is the
conjunction of them — any two without the third is one of the two
failures above.

**1. Submission is instant.** :meth:`SideChannel.submit` allocates a
ticket and returns. It never awaits a provider, never touches the loop's
input consumer. The operator gets an ``s-N`` handle and their prompt
back in the same keystroke.

**2. Answering is non-preemptive.** The worker consults
:func:`assess_admission` before spending anything: while the organism is
saturated — ops in flight past the configured headroom, memory pressure
high — the question WAITS, with bounded exponential backoff. It yields;
it never competes.

**3. Deferral is bounded.** A question deferred forever is a question
dropped, and this codebase's rule about operator intent is that a
silently discarded one is worse than a refused one. After
``JARVIS_BTW_DEFER_MAX_S`` the ticket is admitted anyway and the answer
says it was admitted under pressure. Courtesy is not starvation.

Why an aside is CHEAP enough to be non-preemptive
-------------------------------------------------
Because it does not go through the pipeline at all. The answering
substrate is :func:`fast_path_qa.ask_question` — read-only by
construction, one bounded provider call, charged to the INFORMATIONAL
route's own daily budget. There is no orchestrator, no GENERATE, no
APPLY. Admission control here is about not stealing *attention* and not
stealing a *provider slot*, not about protecting the repository: the
repository is protected structurally, by the substrate having no tool
loop and no write authority.

Situated, or it is not an aside
--------------------------------
"Why is that taking so long?" is unanswerable by a stateless Q&A
endpoint — *what* is "that"? So every question carries a **situation
digest**: the status line's own reading, what is streaming right now,
the model's recent narrative, the ops that just finished, the current
strategic posture. Composed from the canonical surfaces through
:data:`_SITUATION_READERS`, each bounded and fail-soft, and handed to
the substrate as ``grounding`` — the same seam retrieval snippets use.

The digest is READ-ONLY and the readers are a registry, not a ladder:
a surface that ships tomorrow registers itself and appears in the next
aside without an edit here.

Delivery, which is its own problem
-----------------------------------
The answer arrives minutes after the question, into a terminal that has
moved on. Three things follow, and all three are handled:

* it is addressed back to the cockpit that ASKED, by capturing
  :func:`attach_session.current_session` at submit and re-entering that
  scope at delivery — a ContextVar does not survive the task boundary on
  its own, so the capture is explicit;
* it is held while a modal overlay owns the screen (an Iron Gate prompt
  is a decision; painting an aside over it is the interruption this
  module exists to avoid) — bounded, then delivered anyway;
* it carries the ``q-N`` the substrate parked, so the operator can
  re-read it with the same ``/expand`` they use for everything else.

Authority
---------
Zero. This module carries text and decides WHEN to ask, never WHAT to
do. It imports stdlib + authority-free composers only; NEVER
orchestrator / policy / iron_gate / candidate_generator / change_engine
/ tool_executor / urgency_router / semantic_guardian. Every public
entry point is documented as never raising.

Master: ``JARVIS_BTW_ENABLED`` (default **true** — the LANE exists).
Whether an aside is actually ANSWERED still depends on
``JARVIS_FAST_PATH_QA_ENABLED``, which is the paid gate and stays where
it is; a ticket submitted with that gate off is answered with the
substrate's own honest ``disabled`` verdict, never silently lost.
"""
from __future__ import annotations

import asyncio
import contextlib
import enum
import logging
import os
import random
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.SideChannel")


SIDE_CHANNEL_SCHEMA_VERSION: str = "side_channel.v1"

#: Ticket handle prefix. Joins the artifact-ref family (``t-``/``d-``/
#: ``o-``/``n-``/``p-``/``q-``/``b-``/``m-``/``c-``) as the 10th; monotonic
#: and NEVER reused, for the same reason every sibling ring says so — a
#: recycled handle answers the wrong question.
REF_PREFIX: str = "s-"

#: Which worker produced the output, for `attach_session.lane_scope`. An
#: aside is not the ambient organism speaking and it is not an op — it is
#: its own lane, and the deck can colour it as one.
BTW_LANE: str = "btw"


# ===========================================================================
# Env vocabulary — every bound is a knob, every knob is clamped
# ===========================================================================


ENV_MASTER = "JARVIS_BTW_ENABLED"
ENV_QUEUE_DEPTH = "JARVIS_BTW_QUEUE_DEPTH"
ENV_LEDGER_SIZE = "JARVIS_BTW_LEDGER_SIZE"
ENV_CONCURRENCY = "JARVIS_BTW_CONCURRENCY"
ENV_ADMISSION = "JARVIS_BTW_ADMISSION_ENABLED"
ENV_OPS_HEADROOM = "JARVIS_BTW_OPS_HEADROOM"
ENV_DEFER_MAX_S = "JARVIS_BTW_DEFER_MAX_S"
ENV_DEFER_BASE_S = "JARVIS_BTW_DEFER_BASE_S"
ENV_DEFER_CEILING_S = "JARVIS_BTW_DEFER_CEILING_S"
ENV_PRESSURE_TTL_S = "JARVIS_BTW_PRESSURE_TTL_S"
ENV_PRESSURE_HOLD = "JARVIS_BTW_PRESSURE_HOLD_LEVELS"
ENV_SITUATION = "JARVIS_BTW_SITUATION_ENABLED"
ENV_SITUATION_BUDGET_S = "JARVIS_BTW_SITUATION_BUDGET_S"
ENV_SITUATION_MAX_CHARS = "JARVIS_BTW_SITUATION_MAX_CHARS"
ENV_SITUATION_READERS = "JARVIS_BTW_SITUATION_READERS"
ENV_DELIVERY_HOLD_S = "JARVIS_BTW_DELIVERY_HOLD_S"
ENV_CHARTER = "JARVIS_BTW_CHARTER"
ENV_MAX_QUESTION_CHARS = "JARVIS_BTW_MAX_QUESTION_CHARS"

_TRUTHY = ("1", "true", "yes", "on")
_FALSY = ("0", "false", "no", "off")


def _env_bool(name: str, default: bool) -> bool:
    """NEVER raises. Unrecognised text keeps the default rather than
    silently reading as False — a typo'd flag must not disarm a lane."""
    try:
        raw = os.environ.get(name)
        if raw is None:
            return default
        token = raw.strip().lower()
        if token in _TRUTHY:
            return True
        if token in _FALSY:
            return False
        return default
    except Exception:  # noqa: BLE001
        return default


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(str(os.environ.get(name, default)).strip())))
    except Exception:  # noqa: BLE001
        return default


def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    try:
        return max(lo, min(hi, float(str(os.environ.get(name, default)).strip())))
    except Exception:  # noqa: BLE001
        return default


def side_channel_enabled() -> bool:
    """The LANE. Default ON.

    Off, ``/btw`` declines at submit with the flag name in the reason —
    it does not pretend to queue. A refusal an operator can read is the
    only acceptable form of "no"."""
    return _env_bool(ENV_MASTER, True)


def queue_depth() -> int:
    """Un-answered tickets held before refusing. Generous enough for a
    burst of asides, finite because a busy organism must not be able to
    accumulate an invisible backlog of paid questions."""
    return _env_int(ENV_QUEUE_DEPTH, 16, 1, 256)


def ledger_size() -> int:
    """Ring capacity across ALL states. Only TERMINAL tickets are ever
    evicted (see :meth:`SideChannel._evict_if_needed`)."""
    return _env_int(ENV_LEDGER_SIZE, 50, 4, 1000)


def concurrency() -> int:
    """How many asides may be in a provider call at once. One by
    default: asides are a courtesy lane, and the default should not be
    able to look like a load generator."""
    return _env_int(ENV_CONCURRENCY, 1, 1, 8)


def admission_enabled() -> bool:
    """Default ON. Off answers immediately regardless of load — the only
    honest A/B for the deferral policy, and the rollback if it ever
    holds a question it should not have."""
    return _env_bool(ENV_ADMISSION, True)


def ops_headroom() -> int:
    """In-flight ops at or above which an aside waits. ``0`` disables
    the ops dimension entirely (pressure alone then decides)."""
    return _env_int(ENV_OPS_HEADROOM, 2, 0, 64)


def defer_max_s() -> float:
    """The starvation ceiling. After this the ticket is admitted under
    pressure and SAYS so. ``0`` means never defer."""
    return _env_float(ENV_DEFER_MAX_S, 120.0, 0.0, 3600.0)


def defer_base_s() -> float:
    return _env_float(ENV_DEFER_BASE_S, 1.5, 0.1, 60.0)


def defer_ceiling_s() -> float:
    return _env_float(ENV_DEFER_CEILING_S, 15.0, 0.5, 300.0)


def pressure_ttl_s() -> float:
    """How long a memory-pressure reading stays usable.

    Not a polling interval — the sampler is pull-driven by the worker,
    so this is the age past which a reading is retired to UNKNOWN
    rather than believed. Clamped [1, 600]."""
    return _env_float(ENV_PRESSURE_TTL_S, 15.0, 1.0, 600.0)


def situation_enabled() -> bool:
    return _env_bool(ENV_SITUATION, True)


def situation_budget_s() -> float:
    """Wall-clock ceiling on composing the digest. A reader that hangs
    costs the aside its context, never the aside itself."""
    return _env_float(ENV_SITUATION_BUDGET_S, 1.5, 0.1, 30.0)


def situation_max_chars() -> int:
    return _env_int(ENV_SITUATION_MAX_CHARS, 2000, 200, 20_000)


def selected_readers() -> Tuple[str, ...]:
    """Restrict the digest to these reader names. Empty = all of them.

    An allowlist rather than a denylist, because the failure it exists
    for is "one reader is noisy in my terminal", and naming what you
    want survives a reader being renamed better than naming what you
    don't."""
    try:
        raw = str(os.environ.get(ENV_SITUATION_READERS, "") or "").strip()
        if not raw:
            return ()
        return tuple(
            tok.strip().lower() for tok in raw.split(",") if tok.strip()
        )
    except Exception:  # noqa: BLE001
        return ()


def delivery_hold_s() -> float:
    """How long an answer waits for a modal overlay to clear. ``0``
    delivers immediately — which paints over an open Iron Gate prompt,
    so it is a rollback, not a tuning."""
    return _env_float(ENV_DELIVERY_HOLD_S, 20.0, 0.0, 300.0)


def max_question_chars() -> int:
    return _env_int(ENV_MAX_QUESTION_CHARS, 2000, 32, 4096)


#: The charter. NOT a system prompt — `fast_path_qa.system_prompt()` owns
#: that, and duplicating it here would be a second opinion about who the
#: answerer is. This is the delta that makes an aside an aside: the
#: answerer is being interrupted, has no authority, and must say which
#: verb does the thing if asked to do something.
_DEFAULT_CHARTER: str = (
    "You are answering a SIDE QUESTION from the operator, asked while "
    "O+V (the autonomous developer loop) is working. Be brief — three "
    "sentences unless the question genuinely needs more.\n"
    "\n"
    "You have NO authority to change anything: no files, no ops, no "
    "settings. If the operator is asking you to DO something rather "
    "than to explain something, say so in one line and name the REPL "
    "verb that does it (`/goal` to queue work, `/cancel` to stop an op, "
    "`/accept` or `/reject` for a pending review) — do not describe a "
    "change as though you had made it.\n"
    "\n"
    "The live-situation block below is what O+V is doing RIGHT NOW. "
    "Prefer it over general project knowledge when the question uses a "
    "deictic — \"that\", \"this op\", \"why is it slow\" — because those "
    "words refer to the situation, not to the repository. If the block "
    "does not contain what the question refers to, say that plainly "
    "rather than guessing which op was meant."
)


def charter() -> str:
    """Operator-overridable via :data:`ENV_CHARTER`. NEVER raises."""
    try:
        raw = str(os.environ.get(ENV_CHARTER, "") or "").strip()
        return raw if raw else _DEFAULT_CHARTER
    except Exception:  # noqa: BLE001
        return _DEFAULT_CHARTER


# ===========================================================================
# Closed taxonomies
# ===========================================================================


class TicketState(str, enum.Enum):
    """Closed 7-value lifecycle.

    ``DEFERRED`` is deliberately distinct from ``PENDING``: "waiting its
    turn" and "waiting for the organism to have room" look identical in
    a queue depth and are entirely different things to an operator
    wondering whether they have been heard.
    """

    PENDING = "pending"
    DEFERRED = "deferred"
    ASKING = "asking"
    ANSWERED = "answered"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"

    @property
    def is_terminal(self) -> bool:
        return self in (
            TicketState.ANSWERED,
            TicketState.FAILED,
            TicketState.CANCELLED,
            TicketState.ABANDONED,
        )

    @property
    def is_live(self) -> bool:
        """Occupies queue depth — submitted and not yet resolved."""
        return not self.is_terminal


class AdmissionState(str, enum.Enum):
    """Closed 3-value verdict from :func:`assess_admission`."""

    #: The organism has room. Ask now.
    READY = "ready"
    #: The organism is busy. Wait, and try again after ``retry_after_s``.
    DEFER = "defer"
    #: Deferred past the ceiling. Ask anyway, and SAY it was under load —
    #: a starved question is a lost question.
    FORCED = "forced"


# ===========================================================================
# Frozen records
# ===========================================================================


@dataclass(frozen=True)
class SideQuestion:
    """One aside, from keystroke to answer.

    Frozen; every transition is a :func:`dataclasses.replace` performed
    under the ledger lock, so an observer holding a reference can never
    watch a ticket mutate underneath it.
    """

    ref: str
    text: str
    #: The cockpit that asked, or "" for the daemon's own terminal. Captured
    #: at SUBMIT because a ContextVar is per-task: the worker that answers
    #: minutes later is a different task and would otherwise broadcast a
    #: reply to every attached terminal.
    session: str = ""
    state: TicketState = TicketState.PENDING
    submitted_at: float = 0.0
    admitted_at: float = 0.0
    resolved_at: float = 0.0
    #: How many times admission said DEFER, and why it last said it.
    defer_count: int = 0
    defer_reason: str = ""
    #: Set when the ticket was admitted past the deferral ceiling.
    forced: bool = False
    #: `q-N` in the fast_path_qa ring, once answered.
    answer_ref: str = ""
    answer: str = ""
    verdict: str = ""
    diagnostic: str = ""
    cost_usd: float = 0.0
    model: str = ""
    elapsed_s: float = 0.0
    #: Situation reader names that actually contributed. Provenance: an
    #: answer grounded in nothing must not look like one grounded in the
    #: live status line.
    grounded_by: Tuple[str, ...] = ()
    schema_version: str = SIDE_CHANNEL_SCHEMA_VERSION

    def preview(self, width: int = 56) -> str:
        """One-line flattening of the question. NEVER raises."""
        try:
            flat = " ".join(str(self.text or "").split())
            if len(flat) <= width:
                return flat
            return flat[: max(1, width - 1)] + "…"
        except Exception:  # noqa: BLE001
            return ""

    def age_s(self, now: Optional[float] = None) -> float:
        try:
            moment = time.monotonic() if now is None else float(now)
            return max(0.0, moment - float(self.submitted_at))
        except Exception:  # noqa: BLE001
            return 0.0

    def to_dict(self) -> Dict[str, Any]:
        """Transport-safe projection. Body is BOUNDED — an observability
        surface must not become a way to exfiltrate a whole answer."""
        return {
            "ref": self.ref,
            "question": self.text[:1024],
            "session": self.session[:32],
            "state": self.state.value,
            "submitted_at": float(self.submitted_at),
            "defer_count": int(self.defer_count),
            "defer_reason": self.defer_reason[:120],
            "forced": bool(self.forced),
            "answer_ref": self.answer_ref,
            "answer_chars": len(self.answer or ""),
            "verdict": self.verdict[:32],
            "diagnostic": self.diagnostic[:256],
            "cost_usd": float(self.cost_usd),
            "model": self.model[:128],
            "elapsed_s": float(self.elapsed_s),
            "grounded_by": list(self.grounded_by),
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class SubmitOutcome:
    """What happened to a submitted aside. Never a bare bool.

    ``accepted=False`` ALWAYS carries a reason, for the reason
    :class:`operator_input_queue.SubmitResult` states: an operator who
    believes their line landed is worse off than one told it did not.
    """

    accepted: bool
    ticket: Optional[SideQuestion] = None
    reason: str = ""
    #: True when an identical live question already existed and this
    #: submission was folded into it. Accepted, but not a second spend.
    coalesced: bool = False
    depth: int = 0


@dataclass(frozen=True)
class Admission:
    """Whether an aside may be asked right now, and why not."""

    state: AdmissionState
    reason: str = ""
    retry_after_s: float = 0.0
    #: Free-form signal snapshot, for the ledger's defer_reason and for
    #: tests that assert on WHICH dimension held the question.
    signals: Tuple[Tuple[str, str], ...] = ()

    @property
    def admit(self) -> bool:
        return self.state in (AdmissionState.READY, AdmissionState.FORCED)


@dataclass(frozen=True)
class SituationBlock:
    """The composed live-situation digest handed to the answerer."""

    text: str = ""
    readers: Tuple[str, ...] = ()
    elapsed_s: float = 0.0
    truncated: bool = False


@dataclass(frozen=True)
class ChannelSnapshot:
    """Read-only projection for observability surfaces."""

    capacity: int
    size: int
    live: int
    next_seq: int
    answered: int
    failed: int
    refused: int
    cost_usd: float
    worker_running: bool
    tickets: Tuple[Dict[str, Any], ...] = ()
    schema_version: str = SIDE_CHANNEL_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "capacity": self.capacity,
            "size": self.size,
            "live": self.live,
            "next_seq": self.next_seq,
            "answered": self.answered,
            "failed": self.failed,
            "refused": self.refused,
            "cost_usd": float(self.cost_usd),
            "worker_running": bool(self.worker_running),
            "tickets": list(self.tickets),
            "schema_version": self.schema_version,
        }


# ===========================================================================
# Situation readers — a registry, not a ladder
# ===========================================================================
#
# Each reader is `() -> str`. SYNCHRONOUS on purpose: they are called on a
# worker thread through `async_offload.call_off_loop`, so a reader that
# touches disk (the posture store does) cannot stall the event loop. Making
# them coroutines would put that decision in each reader instead of in one
# place, and the durable lesson in this repo is that `async def` around a
# blocking call buys nothing.

SituationReader = Callable[[], str]

_READERS_LOCK = threading.RLock()
_SITUATION_READERS: "OrderedDict[str, SituationReader]" = OrderedDict()


def register_situation_reader(
    name: object, reader: SituationReader, *, override: bool = True,
) -> bool:
    """Add a named contributor to the situation digest. NEVER raises.

    Registration rather than a call list, for the reason the overlay
    arbiter gives about its own: a hardcoded list of surfaces needs an
    edit every time the cockpit grows one, and the edit is exactly what
    gets forgotten.
    """
    try:
        key = str(name or "").strip().lower()
        if not key or not callable(reader):
            return False
        with _READERS_LOCK:
            if key in _SITUATION_READERS and not override:
                return False
            _SITUATION_READERS[key] = reader
        return True
    except Exception:  # noqa: BLE001
        return False


def unregister_situation_reader(name: object) -> bool:
    """NEVER raises."""
    try:
        with _READERS_LOCK:
            return _SITUATION_READERS.pop(
                str(name or "").strip().lower(), None,
            ) is not None
    except Exception:  # noqa: BLE001
        return False


def situation_reader_names() -> Tuple[str, ...]:
    with _READERS_LOCK:
        return tuple(_SITUATION_READERS.keys())


def _read_status() -> str:
    """The status line's OWN reading — phase, cost, ops, route, provider.

    Composed rather than re-derived. The status line already decides what
    "what is happening" means for this cockpit, and a second answer to
    that question is how two surfaces come to disagree in front of the
    operator.
    """
    from backend.core.ouroboros.battle_test.status_line import (
        get_status_line_builder,
    )
    builder = get_status_line_builder()
    if builder is None:
        return ""
    line = str(builder.render_plain() or "").strip()
    return f"Status line: {line}" if line else ""


def _read_inflight() -> str:
    """What is streaming right now — the tool tails and model prose."""
    from backend.core.ouroboros.battle_test.inflight_registry import (
        live_inflight,
    )
    rows: List[str] = []
    for entry in live_inflight()[:4]:
        head = "tool" if getattr(entry, "is_tool", False) else "model"
        op = str(getattr(entry, "op_id", "") or "")
        body = " ".join(str(getattr(entry, "text", "") or "").split())[:220]
        if not body:
            continue
        age = f"{entry.age_s():.0f}s" if hasattr(entry, "age_s") else ""
        rows.append(f"- [{head}{' ' + op[:12] if op else ''} {age}] {body}")
    return "In flight right now:\n" + "\n".join(rows) if rows else ""


def _read_narrative() -> str:
    """The model's own recent voice — why it says it is doing this."""
    from backend.core.ouroboros.battle_test.narrative_channel import (
        get_default_channel,
    )
    frames = get_default_channel().list_recent(limit=4)
    rows: List[str] = []
    for frame in frames:
        prose = " ".join(str(getattr(frame, "prose", "") or "").split())[:280]
        if not prose:
            continue
        kind = getattr(getattr(frame, "kind", None), "value", "") or "narrative"
        phase = str(getattr(frame, "phase", "") or "")
        ref = str(getattr(frame, "ref", "") or "")
        stamp = " ".join(x for x in (ref, kind, phase) if x)
        rows.append(f"- [{stamp}] {prose}")
    return "Recent narrative:\n" + "\n".join(rows) if rows else ""


def _read_recent_ops() -> str:
    """What just finished — the collapsed op blocks the operator saw."""
    from backend.core.ouroboros.battle_test.op_block_buffer import (
        get_default_buffer,
    )
    blocks = get_default_buffer().list_recent(limit=5)
    rows: List[str] = []
    for block in blocks:
        summary = " ".join(
            str(getattr(block, "summary_line", "") or "").split()
        )[:200]
        if not summary:
            continue
        ref = str(getattr(block, "ref", "") or "")
        op = str(getattr(block, "op_id", "") or "")[:12]
        rows.append(f"- [{ref} {op}] {summary}")
    return "Recent operations:\n" + "\n".join(rows) if rows else ""


def _read_posture() -> str:
    """The strategic posture — WHY the organism is choosing this kind of
    work at all. Rendered by the canonical composer so the aside reads
    the same block CONTEXT_EXPANSION reads."""
    from backend.core.ouroboros.governance.posture_observer import (
        get_default_store,
    )
    from backend.core.ouroboros.governance.posture_prompt import (
        compose_posture_section,
    )
    reading = get_default_store().load_current()
    if reading is None:
        return ""
    # force=True: the aside is not a generation prompt, so the
    # CONTEXT_EXPANSION injection gate is not the right authority over
    # whether the operator may be TOLD the posture.
    return str(compose_posture_section(reading, top_n=2, force=True) or "")


def _read_queue() -> str:
    """The aside lane's own backlog — so "why haven't you answered the
    last one" is answerable by the answer to the next one."""
    channel = _CHANNEL
    if channel is None:
        return ""
    # PENDING/DEFERRED only. The ticket being grounded right now is
    # ASKING, and listing it under "other questions waiting" would tell
    # the answerer it is queued behind itself.
    waiting = [
        t for t in channel.live_tickets()
        if t.state in (TicketState.PENDING, TicketState.DEFERRED)
    ]
    if not waiting:
        return ""
    rows = [
        f"- {t.ref} [{t.state.value}] {t.preview(48)}"
        for t in waiting[:5]
    ]
    return (
        f"Other side questions waiting ({len(waiting)}):\n"
        + "\n".join(rows)
    )


def _install_default_readers() -> None:
    """Idempotent. Called at import; safe to call again after a reset."""
    for name, fn in (
        ("status", _read_status),
        ("inflight", _read_inflight),
        ("narrative", _read_narrative),
        ("ops", _read_recent_ops),
        ("posture", _read_posture),
        ("queue", _read_queue),
    ):
        register_situation_reader(name, fn)


_install_default_readers()


def compose_situation_sync() -> SituationBlock:
    """Run every selected reader and assemble the digest. NEVER raises.

    Synchronous and self-contained so it can be handed to
    :func:`async_offload.call_off_loop` whole. One reader raising costs
    that reader's section and nothing else — the digest is best-effort
    context, never a precondition for answering.
    """
    started = time.monotonic()
    if not situation_enabled():
        return SituationBlock()
    allow = selected_readers()
    with _READERS_LOCK:
        readers = list(_SITUATION_READERS.items())
    sections: List[str] = []
    contributed: List[str] = []
    for name, fn in readers:
        if allow and name not in allow:
            continue
        try:
            text = str(fn() or "").strip()
        except Exception:  # noqa: BLE001
            logger.debug("[SideChannel] reader %r degraded", name,
                         exc_info=True)
            continue
        if not text:
            continue
        sections.append(text)
        contributed.append(name)
    if not sections:
        return SituationBlock(
            elapsed_s=max(0.0, time.monotonic() - started),
        )
    body = "\n\n".join(sections)
    limit = situation_max_chars()
    truncated = len(body) > limit
    if truncated:
        # Truncate from the TAIL: the readers are registered in
        # descending order of how directly they answer "what is
        # happening now", so the head is the part a deictic refers to.
        body = body[:limit].rstrip() + "\n… (situation digest truncated)"
    return SituationBlock(
        text=body,
        readers=tuple(contributed),
        elapsed_s=max(0.0, time.monotonic() - started),
        truncated=truncated,
    )


async def compose_situation() -> SituationBlock:
    """Off-loop, time-boxed digest. NEVER raises.

    The budget is on the WHOLE composition rather than per reader: a
    per-reader timeout cannot bound a reader that blocks the thread it
    was given, and the thing being protected is the answer's latency,
    which is a property of the total.
    """
    try:
        from backend.core.async_offload import call_off_loop
        return await asyncio.wait_for(
            call_off_loop(compose_situation_sync),
            timeout=situation_budget_s(),
        )
    except asyncio.TimeoutError:
        logger.debug("[SideChannel] situation digest timed out")
        return SituationBlock()
    except Exception:  # noqa: BLE001
        logger.debug("[SideChannel] situation digest degraded", exc_info=True)
        # Last resort is still OFF the loop. Composing inline here would
        # be the one thing this whole module is arranged to avoid: a
        # reader that touches disk would stall every op, every heartbeat
        # and every other cockpit behind an ASIDE. Losing the context is
        # the cheaper failure by a wide margin.
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(compose_situation_sync),
                timeout=situation_budget_s(),
            )
        except Exception:  # noqa: BLE001
            return SituationBlock()


def build_grounding(situation: SituationBlock) -> str:
    """Charter + digest, in the shape ``ask_question`` grounds on.

    The charter travels even when the digest is empty: "you have no
    authority to change things" is true whether or not the status line
    is up, and it is the half that keeps an aside from reading like a
    commitment."""
    parts = [charter().strip()]
    if situation.text:
        parts.append(
            "Live situation (what O+V is doing right now):\n\n"
            + situation.text
        )
    return "\n\n".join(p for p in parts if p)


# ===========================================================================
# Admission — WHEN an aside may spend
# ===========================================================================


def _inflight_op_count() -> Optional[int]:
    """Ops the organism currently has open, or None when unknowable.

    None is a real answer and is treated as "no evidence of load", not
    as zero: a probe that cannot read must not be able to ARM a gate by
    reporting quiet. (The rate-sampler lesson: a signal polled before it
    can answer reads as zero and disarms the guard.)
    """
    try:
        from backend.core.ouroboros.battle_test.inflight_registry import (
            live_inflight,
        )
        entries = live_inflight()
    except Exception:  # noqa: BLE001
        return None
    try:
        ops = {
            str(getattr(e, "op_id", "") or "")
            for e in entries
            if str(getattr(e, "op_id", "") or "")
        }
        # An entry with no op_id is still WORK in flight, so it counts as
        # one — dropping it would let a busy tool stream read as idle.
        anonymous = sum(
            1 for e in entries if not str(getattr(e, "op_id", "") or "")
        )
        return len(ops) + anonymous
    except Exception:  # noqa: BLE001
        return None


def sample_memory_pressure_sync() -> Optional[str]:
    """Probe the canonical gate and return its level as a string.

    BLOCKING. ``MemoryPressureGate`` documents that it "caches nothing —
    each call triggers a fresh probe", and its cascade ends in a
    ``vm_stat`` subprocess on this platform. So this is never called
    from the event loop; :func:`refresh_memory_pressure` owns the
    off-loop hop and this function owns the reading.

    None means "no reading", never "fine". NEVER raises.
    """
    try:
        from backend.core.ouroboros.governance.memory_pressure_gate import (
            PressureLevel,
            get_default_gate,
            is_enabled,
        )
        if not is_enabled():
            return None
        level = get_default_gate().pressure()
        if level is None:
            return None
        value = getattr(level, "value", None)
        text = str(value if value is not None else level).strip().lower()
        # Validated against the gate's own closed enum rather than
        # trusted as a string: an unrecognised level must not silently
        # satisfy the busy comparison in EITHER direction.
        return text if text in {m.value for m in PressureLevel} else None
    except Exception:  # noqa: BLE001
        logger.debug("[SideChannel] pressure probe degraded", exc_info=True)
        return None


class _PressureCache:
    """Last-good pressure reading, with an age.

    Two properties, and the second is the one that has been got wrong
    before in this codebase: a reading that is too old does NOT decay to
    the healthy value, it decays to *no reading*. A sampler that reports
    zero/OK when it has not measured disarms the very guard it feeds,
    and the disarm is invisible because "fine" is what fine looks like.
    """

    __slots__ = ("_level", "_at", "_lock")

    def __init__(self) -> None:
        self._level: Optional[str] = None
        self._at: float = 0.0
        self._lock = threading.RLock()

    def read(self) -> Optional[str]:
        """The reading, or None when there is none or it has aged out."""
        try:
            with self._lock:
                if self._level is None:
                    return None
                if (time.monotonic() - self._at) > pressure_ttl_s():
                    return None
                return self._level
        except Exception:  # noqa: BLE001
            return None

    def stale(self) -> bool:
        """Would a read return None? Cheap; used to skip a needless hop."""
        return self.read() is None

    def store(self, level: Optional[str]) -> None:
        try:
            with self._lock:
                # A failed probe does not overwrite a good reading — it
                # simply stops refreshing it, and the TTL retires it.
                if level is None:
                    return
                self._level = level
                self._at = time.monotonic()
        except Exception:  # noqa: BLE001
            pass

    def clear(self) -> None:
        with self._lock:
            self._level = None
            self._at = 0.0


_PRESSURE = _PressureCache()


async def refresh_memory_pressure(*, force: bool = False) -> Optional[str]:
    """Re-sample pressure off-loop if the cached reading has aged out.

    Time-boxed by the same budget the situation digest uses: a hung
    ``vm_stat`` costs the admission gate one dimension, never the
    aside. NEVER raises.
    """
    try:
        if not force and not _PRESSURE.stale():
            return _PRESSURE.read()
        try:
            from backend.core.async_offload import call_off_loop
            level = await asyncio.wait_for(
                call_off_loop(sample_memory_pressure_sync),
                timeout=situation_budget_s(),
            )
        except asyncio.TimeoutError:
            logger.debug("[SideChannel] pressure probe timed out")
            return _PRESSURE.read()
        _PRESSURE.store(level)
        return _PRESSURE.read()
    except Exception:  # noqa: BLE001
        logger.debug("[SideChannel] pressure refresh degraded", exc_info=True)
        return _PRESSURE.read()


#: Pressure levels at which an aside waits.
#:
#: CRITICAL only, by default, and the reason is that this dimension is
#: easy to get wrong in the strict direction. ``MemoryPressureGate``
#: exists to bound FAN-OUT — how many models, subagents and worktrees
#: may be resident at once. An aside is one bounded read-only HTTPS call
#: with a 400-token ceiling; it is not what that gate protects against,
#: and holding it at HIGH means a host that simply runs warm (this one
#: sits between WARN and HIGH under a test suite) defers every question
#: to its 120s ceiling and then admits it anyway. That is a two-minute
#: wait bought with no memory saved.
#:
#: CRITICAL is different in kind: there, the process may be about to be
#: reaped, and a spend whose answer nobody will live to read is waste
#: rather than courtesy.
#:
#: Operators who want the stricter lane add ``high`` to the knob. Tokens
#: are matched against the gate's own vocabulary — which
#: `sample_memory_pressure_sync` has already validated — so a level
#: added to `PressureLevel` tomorrow arrives as an unrecognised token
#: and reads as "not busy" rather than as a crash.
_DEFAULT_PRESSURE_HOLD: Tuple[str, ...] = ("critical",)


def pressure_hold_levels() -> Tuple[str, ...]:
    """Levels that hold an aside. NEVER raises.

    An empty/garbage value keeps the default rather than disarming the
    dimension silently — the same reason `_env_bool` refuses to read a
    typo as False."""
    try:
        raw = str(os.environ.get(ENV_PRESSURE_HOLD, "") or "").strip()
        if not raw:
            return _DEFAULT_PRESSURE_HOLD
        tokens = tuple(
            t.strip().lower() for t in raw.split(",") if t.strip()
        )
        return tokens or _DEFAULT_PRESSURE_HOLD
    except Exception:  # noqa: BLE001
        return _DEFAULT_PRESSURE_HOLD


def assess_admission(ticket: SideQuestion) -> Admission:
    """May this aside be asked right now? Pure-ish, NEVER raises.

    Deliberately dimension-additive and strictest-wins, matching
    :mod:`risk_tier_floor`: each signal can only HOLD the question, none
    can force it through. The single thing that overrides them all is
    age, and that override exists because the alternative is starvation.
    """
    try:
        if not admission_enabled():
            return Admission(AdmissionState.READY, reason="admission disabled")

        ceiling = defer_max_s()
        signals: List[Tuple[str, str]] = []
        held: List[str] = []

        headroom = ops_headroom()
        ops = _inflight_op_count()
        if ops is not None:
            signals.append(("ops_inflight", str(ops)))
            if headroom > 0 and ops >= headroom:
                held.append(f"{ops} op(s) in flight (headroom {headroom})")
        else:
            signals.append(("ops_inflight", "unknown"))

        # Read, never probe. This function runs on the event loop and
        # the gate's probe cascade ends in a subprocess; the refresh is
        # `refresh_memory_pressure`'s job, off-loop, before the call.
        pressure = _PRESSURE.read()
        if pressure is not None:
            signals.append(("memory_pressure", pressure))
            if pressure in pressure_hold_levels():
                held.append(f"memory pressure {pressure}")
        else:
            signals.append(("memory_pressure", "unknown"))

        if not held:
            return Admission(
                AdmissionState.READY, reason="clear",
                signals=tuple(signals),
            )

        reason = "; ".join(held)
        if ceiling <= 0.0:
            # Deferral disabled entirely. Held but with a zero ceiling is
            # a contradiction, and the honest resolution is to ask.
            return Admission(
                AdmissionState.FORCED,
                reason=f"{reason} (deferral disabled)",
                signals=tuple(signals),
            )
        if ticket.age_s() >= ceiling:
            return Admission(
                AdmissionState.FORCED,
                reason=f"{reason} (waited {ticket.age_s():.0f}s)",
                signals=tuple(signals),
            )
        return Admission(
            AdmissionState.DEFER,
            reason=reason,
            retry_after_s=_backoff_delay(ticket.defer_count),
            signals=tuple(signals),
        )
    except Exception:  # noqa: BLE001
        logger.debug("[SideChannel] admission degraded", exc_info=True)
        # A gate that cannot evaluate must not be able to hold a question
        # forever. Fail OPEN here — the thing being protected is
        # politeness, and the cost of getting it wrong is one bounded
        # provider call.
        return Admission(AdmissionState.READY, reason="admission unevaluable")


def assess_admission_now() -> Admission:
    """Admission as it stands for a HYPOTHETICAL aside submitted now.

    For status surfaces. Constructed with ``submitted_at`` set to the
    current clock rather than the dataclass default of ``0.0``: a probe
    built with the default reads as having waited since the process
    started, which is past every deferral ceiling, so the lane would
    report FORCED at exactly the moments it is actually deferring.
    NEVER raises.
    """
    try:
        channel = _CHANNEL
        live = channel.live_tickets() if channel is not None else ()
        if live:
            # A real waiting ticket is a better probe than a synthetic
            # one: its age is what the ceiling is actually measured
            # against, so the reported verdict is the one it will get.
            return assess_admission(live[0])
        return assess_admission(SideQuestion(
            ref=f"{REF_PREFIX}?", text="", submitted_at=time.monotonic(),
        ))
    except Exception:  # noqa: BLE001
        return Admission(AdmissionState.READY, reason="admission unevaluable")


def _backoff_delay(defer_count: int) -> float:
    """Exponential with full jitter, clamped. NEVER raises.

    Jittered because several asides submitted together would otherwise
    re-check in lockstep forever, which is a thundering herd against the
    very signals that are telling them to wait.
    """
    try:
        base = defer_base_s()
        ceiling = defer_ceiling_s()
        raw = base * (2 ** max(0, min(16, int(defer_count))))
        return max(0.05, min(ceiling, random.uniform(base * 0.5, max(base, raw))))
    except Exception:  # noqa: BLE001
        return 1.0


# ===========================================================================
# Delivery — the sink, and the hold
# ===========================================================================


AnswerSink = Callable[[str], None]

_SINK_LOCK = threading.RLock()
_ANSWER_SINK: Optional[AnswerSink] = None


def set_answer_sink(sink: Optional[AnswerSink]) -> None:
    """Bind (or unbind with ``None``) the surface asides render onto.

    A binding point, not a singleton, and the producer publishes ITSELF
    — the same contract :func:`operator_input_queue.set_active_queue`
    documents, and for the same reason it documents it: the version of
    this that reached for a harness accessor failed silently because no
    such accessor existed and the ImportError was swallowed.

    Pass a callable that resolves the console at CALL time
    (``lambda m: flow.console.print(m)``), never one that closes over
    ``flow.console`` — the harness swaps that attribute for the spooled
    mirror after boot, and a captured reference would keep rendering to
    the un-mirrored original.

    NEVER raises.
    """
    global _ANSWER_SINK
    with _SINK_LOCK:
        _ANSWER_SINK = sink if callable(sink) else None


def answer_sink_bound() -> bool:
    with _SINK_LOCK:
        return _ANSWER_SINK is not None


def _overlay_owns_screen() -> bool:
    """Is a modal surface (Iron Gate prompt, diff preview, panic) up?

    Composed from the arbiter that already answers this for the Escape
    key. NEVER raises; unknown reads as False, because holding an answer
    on a surface we cannot see is indistinguishable from losing it.
    """
    try:
        from backend.core.ouroboros.battle_test.overlay_arbiter import (
            overlay_active,
        )
        return bool(overlay_active())
    except Exception:  # noqa: BLE001
        return False


async def _await_clear_screen(deadline_s: float) -> bool:
    """Wait out a modal overlay, up to ``deadline_s``. Returns True if
    the screen cleared. NEVER raises."""
    if deadline_s <= 0.0:
        return not _overlay_owns_screen()
    end = time.monotonic() + deadline_s
    # Poll rather than subscribe: the arbiter is a pull-registry with no
    # event, and inventing one here would make this module part of its
    # contract. The interval is derived from the hold so a short hold
    # still checks several times.
    interval = max(0.1, min(1.0, deadline_s / 20.0))
    while True:
        if not _overlay_owns_screen():
            return True
        if time.monotonic() >= end:
            return False
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            return False


def emit_markup(markup: str, session: str = "") -> bool:
    """Render one styled line-block to the operator. NEVER raises.

    Addressed, not broadcast: the block runs inside
    :func:`attach_session.session_scope` for the cockpit that asked, and
    inside :func:`attach_session.lane_scope` for the aside lane. The
    spooled console reads both from the ContextVar at print time, so no
    renderer below needs to know an IPC session exists.

    Returns True when something consumed it.
    """
    try:
        text = str(markup or "")
        if not text:
            return False
        with _SINK_LOCK:
            sink = _ANSWER_SINK
        try:
            from backend.core.ouroboros.battle_test.attach_session import (
                lane_scope,
                session_scope,
            )
            scope_session = session_scope
            scope_lane = lane_scope
        except Exception:  # noqa: BLE001
            scope_session = None
            scope_lane = None

        def _write() -> bool:
            if sink is not None:
                sink(text)
                return True
            # No console bound (a headless daemon that never built one).
            # The speech primitive still reaches attached cockpits, and
            # it takes the session explicitly rather than through the
            # ContextVar, so this path is correct with or without scopes.
            try:
                from backend.core.ouroboros.battle_test.cockpit_attach import (
                    publish_markup_global,
                )
                return bool(publish_markup_global(
                    text, session=(session or None),
                ))
            except Exception:  # noqa: BLE001
                return False

        if scope_session is None or scope_lane is None:
            return _write()
        with scope_session(session or None):
            with scope_lane(BTW_LANE):
                return _write()
    except Exception:  # noqa: BLE001
        logger.debug("[SideChannel] emit degraded", exc_info=True)
        return False


# ===========================================================================
# Rendering — pure, so every surface says the same thing
# ===========================================================================


_STATE_GLYPH: Dict[TicketState, str] = {
    TicketState.PENDING: "…",
    TicketState.DEFERRED: "⏳",
    TicketState.ASKING: "💭",
    TicketState.ANSWERED: "💡",
    TicketState.FAILED: "⚠",
    TicketState.CANCELLED: "×",
    TicketState.ABANDONED: "⚠",
}


def _escape(text: str) -> str:
    """Rich-markup-inert. Question text is OPERATOR-controlled and answer
    text is MODEL-controlled; neither may open a style tag on a surface
    that renders markup."""
    try:
        from rich.markup import escape as _rich_escape
        return _rich_escape(str(text or ""))
    except Exception:  # noqa: BLE001
        return str(text or "").replace("[", "\\[")


def render_ack(ticket: SideQuestion, *, depth: int = 0) -> str:
    """The receipt the operator gets in the same keystroke."""
    tail = f" · {depth} waiting" if depth > 1 else ""
    return (
        f"  [dim]{_STATE_GLYPH[TicketState.PENDING]} noted "
        f"[/dim][bold]{ticket.ref}[/bold][dim] — will answer without "
        f"interrupting{tail}[/dim]"
    )


def render_ticket_line(ticket: SideQuestion) -> str:
    """One ledger row."""
    glyph = _STATE_GLYPH.get(ticket.state, "·")
    bits = [f"  {glyph} [bold]{ticket.ref}[/bold] [dim]{ticket.state.value}"]
    if ticket.state is TicketState.DEFERRED and ticket.defer_reason:
        bits.append(f" · held: {_escape(ticket.defer_reason)}")
    if ticket.answer_ref:
        bits.append(f" · {ticket.answer_ref}")
    if ticket.cost_usd > 0.0:
        bits.append(f" · ${ticket.cost_usd:.5f}")
    bits.append(f"[/dim]  {_escape(ticket.preview())}")
    return "".join(bits)


def render_answer(ticket: SideQuestion) -> str:
    """The delivered aside. Includes the question, because the answer
    arrives long after it was asked and an answer alone is a riddle."""
    head = (
        f"  [bold]{_STATE_GLYPH[TicketState.ANSWERED]} btw[/bold] "
        f"[dim]· {ticket.ref}"
    )
    meta: List[str] = []
    if ticket.answer_ref:
        meta.append(ticket.answer_ref)
    if ticket.model:
        meta.append(ticket.model)
    if ticket.elapsed_s:
        meta.append(f"{ticket.elapsed_s:.1f}s")
    if ticket.cost_usd:
        meta.append(f"${ticket.cost_usd:.5f}")
    if ticket.grounded_by:
        meta.append("grounded: " + ",".join(ticket.grounded_by))
    if ticket.forced:
        # Said out loud, because it is the one case where the lane broke
        # its own courtesy rule, and an operator debugging a latency
        # spike deserves to know an aside spent while the organism was
        # busy.
        meta.append("admitted under load")
    if meta:
        head += " · " + " · ".join(_escape(m) for m in meta)
    head += "[/dim]"
    rows = [head, f"  [dim]⎿[/dim]  [dim]{_escape(ticket.preview(72))}[/dim]"]
    for line in str(ticket.answer or "").splitlines():
        rows.append(f"     {_escape(line)}")
    if ticket.answer_ref:
        rows.append(
            f"  [dim](re-read: /expand {ticket.answer_ref})[/dim]"
        )
    return "\n".join(rows)


def render_failure(ticket: SideQuestion) -> str:
    """A verdict that is not an answer, surfaced verbatim.

    The substrate's diagnostics already carry the actionable half —
    which env var is off, which budget is spent — so re-voicing them
    here would only lose detail.
    """
    return (
        f"  [bold]{_STATE_GLYPH[TicketState.FAILED]} btw[/bold] "
        f"[dim]· {ticket.ref} · {_escape(ticket.verdict or 'unresolved')}"
        f"[/dim]\n"
        f"  [dim]⎿[/dim]  [dim]{_escape(ticket.preview(72))}[/dim]\n"
        f"     [dim]{_escape(ticket.diagnostic)}[/dim]"
    )


def render_ledger(tickets: Tuple[SideQuestion, ...]) -> str:
    """The `/btw` listing with no question."""
    if not tickets:
        return "  [dim]no side questions yet — `/btw <question>`[/dim]"
    return "\n".join(render_ticket_line(t) for t in tickets)


# ===========================================================================
# SideChannel
# ===========================================================================


class SideChannel:
    """The aside lane: a bounded ledger plus one worker that drains it.

    Ordering is FIFO and established at :meth:`submit`, which is the only
    place submission order is knowable. Answering may overlap up to
    :func:`concurrency`, but ADMISSION is evaluated head-first: the
    signals that hold an aside are global, so letting a later ticket
    overtake a held one would only mean spending under the same load
    the hold exists to respect.
    """

    def __init__(
        self,
        *,
        capacity: Optional[int] = None,
        max_live: Optional[int] = None,
        clock: Optional[Callable[[], float]] = None,
        asker: Optional[Callable[..., Any]] = None,
    ) -> None:
        self._lock = threading.RLock()
        self._tickets: "OrderedDict[str, SideQuestion]" = OrderedDict()
        self._seq = 1
        self._capacity = int(capacity or ledger_size())
        self._max_live = int(max_live or queue_depth())
        self._clock = clock or time.monotonic
        #: Injectable answering substrate, for hermetic tests. None →
        #: composes `fast_path_qa.ask_question` at call time (late, so a
        #: test can monkeypatch the module without constructing a new
        #: channel).
        self._asker = asker
        self._wake: Optional[asyncio.Event] = None
        self._worker: Optional[Any] = None
        self._inflight: Dict[str, Any] = {}
        self._sem: Optional[asyncio.Semaphore] = None
        self._sem_size = 0
        self._closed = False
        self.refused = 0
        self.answered = 0
        self.failed = 0
        self.cost_usd = 0.0

    # -- producer -------------------------------------------------------

    def submit(
        self, text: object, session: Optional[str] = None,
    ) -> SubmitOutcome:
        """Take an aside. Non-blocking. NEVER raises.

        Returns immediately with a ticket; the provider call happens on
        the worker. This is the entire point of the module — the caller
        is the operator-input queue's single consumer, and awaiting here
        would put every later keystroke behind a provider round-trip.
        """
        try:
            if not side_channel_enabled():
                return SubmitOutcome(
                    accepted=False,
                    reason=f"side channel disabled ({ENV_MASTER}=false)",
                )
            raw = str(text or "").strip()
            if not raw:
                return SubmitOutcome(accepted=False, reason="empty question")
            limit = max_question_chars()
            if len(raw) > limit:
                raw = raw[:limit]
            if self._closed:
                return SubmitOutcome(accepted=False, reason="shutting down")

            with self._lock:
                # Coalesce a duplicate LIVE question. Double-Enter and a
                # re-ask while the first is still deferring are the same
                # keystroke twice; charging for both would be paying for
                # the operator's impatience.
                key = " ".join(raw.split()).casefold()
                for existing in reversed(self._tickets.values()):
                    if not existing.state.is_live:
                        continue
                    if " ".join(existing.text.split()).casefold() == key:
                        return SubmitOutcome(
                            accepted=True, ticket=existing,
                            coalesced=True,
                            depth=self._live_count_locked(),
                        )
                live = self._live_count_locked()
                if live >= self._max_live:
                    self.refused += 1
                    return SubmitOutcome(
                        accepted=False, depth=live,
                        reason=(
                            f"side-question queue full ({self._max_live}) — "
                            f"wait, or `/btw cancel <ref>`"
                        ),
                    )
                ticket = SideQuestion(
                    ref=f"{REF_PREFIX}{self._seq}",
                    text=raw,
                    session=str(session or ""),
                    submitted_at=self._clock(),
                )
                self._seq += 1
                self._tickets[ticket.ref] = ticket
                self._evict_if_needed()
                depth = self._live_count_locked()

            self._ensure_worker()
            self._signal()
            return SubmitOutcome(accepted=True, ticket=ticket, depth=depth)
        except Exception:  # noqa: BLE001
            logger.debug("[SideChannel] submit degraded", exc_info=True)
            return SubmitOutcome(accepted=False, reason="submit failed")

    def cancel(self, ref: object) -> Optional[SideQuestion]:
        """Withdraw an aside. NEVER raises. Returns the ticket, or None
        when unknown / already terminal.

        An ASKING ticket has its provider call cancelled: the operator
        said never mind, and finishing a call they no longer want is a
        spend with no consumer.
        """
        try:
            handle = str(ref or "").strip()
            with self._lock:
                ticket = self._tickets.get(handle)
                if ticket is None or ticket.state.is_terminal:
                    return None
                task = self._inflight.pop(handle, None)
                updated = replace(
                    ticket,
                    state=TicketState.CANCELLED,
                    resolved_at=self._clock(),
                    diagnostic="cancelled by operator",
                )
                self._tickets[handle] = updated
            if task is not None:
                try:
                    task.cancel()
                except Exception:  # noqa: BLE001
                    pass
            self._signal()
            return updated
        except Exception:  # noqa: BLE001
            logger.debug("[SideChannel] cancel degraded", exc_info=True)
            return None

    # -- observation ----------------------------------------------------

    def lookup(self, ref: object) -> Optional[SideQuestion]:
        """NEVER raises."""
        try:
            with self._lock:
                return self._tickets.get(str(ref or "").strip())
        except Exception:  # noqa: BLE001
            return None

    def list_recent(self, limit: int = 10) -> Tuple[SideQuestion, ...]:
        """Newest last, matching every sibling ring. NEVER raises."""
        try:
            with self._lock:
                items = list(self._tickets.values())
            n = max(1, min(len(items), int(limit)))
            return tuple(items[-n:])
        except Exception:  # noqa: BLE001
            return ()

    def live_tickets(self) -> Tuple[SideQuestion, ...]:
        """Submitted and unresolved, in submission order. NEVER raises."""
        try:
            with self._lock:
                return tuple(
                    t for t in self._tickets.values() if t.state.is_live
                )
        except Exception:  # noqa: BLE001
            return ()

    def all_refs(self) -> Tuple[str, ...]:
        try:
            with self._lock:
                return tuple(self._tickets.keys())
        except Exception:  # noqa: BLE001
            return ()

    def snapshot(self, *, tickets: int = 5) -> ChannelSnapshot:
        """Read-only projection. NEVER raises."""
        try:
            with self._lock:
                items = list(self._tickets.values())
                live = sum(1 for t in items if t.state.is_live)
                worker = self._worker
                return ChannelSnapshot(
                    capacity=self._capacity,
                    size=len(items),
                    live=live,
                    next_seq=self._seq,
                    answered=self.answered,
                    failed=self.failed,
                    refused=self.refused,
                    cost_usd=round(self.cost_usd, 6),
                    worker_running=bool(
                        worker is not None and not worker.done()
                    ),
                    tickets=tuple(
                        t.to_dict()
                        for t in items[-max(0, int(tickets)):]
                    ) if tickets else (),
                )
        except Exception:  # noqa: BLE001
            return ChannelSnapshot(
                capacity=0, size=0, live=0, next_seq=1, answered=0,
                failed=0, refused=0, cost_usd=0.0, worker_running=False,
            )

    # -- internals: ledger ----------------------------------------------

    def _live_count_locked(self) -> int:
        return sum(1 for t in self._tickets.values() if t.state.is_live)

    def _evict_if_needed(self) -> None:
        """Drop oldest TERMINAL tickets only. Caller holds the lock.

        A live ticket is never evicted, even at capacity: the queue-depth
        guard refuses BEFORE the ring can fill with live ones, so
        reaching here with nothing terminal to drop means the ring is
        legitimately full of work in progress — and silently discarding
        an unanswered question is the one outcome this module exists to
        prevent.
        """
        try:
            while len(self._tickets) > self._capacity:
                victim = next(
                    (r for r, t in self._tickets.items()
                     if t.state.is_terminal),
                    None,
                )
                if victim is None:
                    return
                self._tickets.pop(victim, None)
        except Exception:  # noqa: BLE001
            pass

    def _transition(self, ref: str, **changes: Any) -> Optional[SideQuestion]:
        """Replace-in-place under the lock, refusing to resurrect a
        terminal ticket. NEVER raises.

        The refusal is load-bearing: `cancel` runs on the operator's task
        while the worker is mid-answer on its own, and without it a
        provider result arriving after a cancel would flip the ticket
        back to ANSWERED and deliver an aside the operator withdrew.
        """
        try:
            with self._lock:
                current = self._tickets.get(ref)
                if current is None or current.state.is_terminal:
                    return None
                updated = replace(current, **changes)
                self._tickets[ref] = updated
                return updated
        except Exception:  # noqa: BLE001
            logger.debug("[SideChannel] transition degraded", exc_info=True)
            return None

    # -- internals: worker ----------------------------------------------

    def _signal(self) -> None:
        try:
            waiter = self._wake
            if waiter is not None:
                waiter.set()
        except Exception:  # noqa: BLE001
            pass

    def _ensure_worker(self) -> Optional[Any]:
        """Start the drain on the RUNNING loop, idempotently.

        Lazy rather than boot-wired on purpose: the lane then needs zero
        edits to any boot path, and a channel constructed in a test with
        no loop is inert instead of broken.
        """
        try:
            if self._worker is not None and not self._worker.done():
                return self._worker
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return None  # no loop — submit still ledgers the ticket
            self._wake = asyncio.Event()
            self._sem_size = concurrency()
            self._sem = asyncio.Semaphore(self._sem_size)
            self._closed = False
            try:
                from backend.core.ouroboros.battle_test.panic_arbiter import (
                    spawn_supervised,
                )
                self._worker = spawn_supervised(
                    self._drain(), origin="side_channel.drain",
                )
            except Exception:  # noqa: BLE001
                # Supervision unavailable — an unsupervised task can die
                # silently, so the fallback is explicitly second-best and
                # only exists so the lane still works.
                self._worker = asyncio.ensure_future(self._drain())
            return self._worker
        except Exception:  # noqa: BLE001
            logger.debug("[SideChannel] worker start degraded", exc_info=True)
            return None

    def _next_pending(self) -> Optional[SideQuestion]:
        with self._lock:
            for ticket in self._tickets.values():
                if ticket.state in (TicketState.PENDING, TicketState.DEFERRED):
                    return ticket
        return None

    async def _drain(self) -> None:
        """Answer asides forever. NEVER exits on a handler error.

        A drain that dies on one bad ticket wedges every later question
        the operator asks — which would make this lane strictly worse
        than the blocking `/ask` it replaces.
        """
        while True:
            try:
                ticket = self._next_pending()
                if ticket is None:
                    if self._closed:
                        return
                    waiter = self._wake
                    if waiter is not None:
                        waiter.clear()
                        await waiter.wait()
                    else:  # pragma: no cover — _ensure_worker sets it
                        await asyncio.sleep(0.1)
                    continue

                # Off-loop, TTL'd. Done HERE rather than inside
                # `assess_admission` so the gate itself stays a pure
                # read: the probe ends in a subprocess, and a
                # synchronous hop into it from the loop is the exact
                # stall class this organism has paid for before.
                await refresh_memory_pressure()
                admission = assess_admission(ticket)
                if admission.state is AdmissionState.DEFER:
                    self._transition(
                        ticket.ref,
                        state=TicketState.DEFERRED,
                        defer_count=ticket.defer_count + 1,
                        defer_reason=admission.reason,
                    )
                    # Sleep the backoff, but wake early if the operator
                    # submits or cancels — a cancelled ticket must not
                    # keep the queue parked behind its own hold.
                    waiter = self._wake
                    if waiter is not None:
                        waiter.clear()
                        with contextlib.suppress(asyncio.TimeoutError):
                            await asyncio.wait_for(
                                waiter.wait(),
                                timeout=admission.retry_after_s,
                            )
                    else:  # pragma: no cover
                        await asyncio.sleep(admission.retry_after_s)
                    continue

                admitted = self._transition(
                    ticket.ref,
                    state=TicketState.ASKING,
                    admitted_at=self._clock(),
                    forced=(admission.state is AdmissionState.FORCED),
                    defer_reason=(
                        admission.reason
                        if admission.state is AdmissionState.FORCED else ""
                    ),
                )
                if admitted is None:
                    continue  # cancelled between selection and admission

                sem = self._sem
                if sem is not None:
                    await sem.acquire()
                try:
                    from backend.core.ouroboros.battle_test.panic_arbiter import (  # noqa: E501
                        spawn_supervised,
                    )
                    task = spawn_supervised(
                        self._answer(admitted),
                        origin=f"side_channel.answer:{admitted.ref}",
                    )
                except Exception:  # noqa: BLE001
                    task = asyncio.ensure_future(self._answer(admitted))
                with self._lock:
                    self._inflight[admitted.ref] = task
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001
                logger.debug("[SideChannel] drain degraded", exc_info=True)
                await asyncio.sleep(0.1)

    async def _resolve_asker(self) -> Optional[Callable[..., Any]]:
        """The answering substrate, resolved LATE. NEVER raises.

        Late so a test can monkeypatch `fast_path_qa.ask_question` after
        the channel exists, and so an import failure degrades this one
        ticket instead of the lane.
        """
        if self._asker is not None:
            return self._asker
        try:
            from backend.core.ouroboros.governance.fast_path_qa import (
                ask_question,
            )
            return ask_question
        except Exception:  # noqa: BLE001
            logger.debug("[SideChannel] asker unavailable", exc_info=True)
            return None

    async def _answer(self, ticket: SideQuestion) -> None:
        """One aside, end to end. NEVER raises; ALWAYS releases the slot."""
        started = self._clock()
        try:
            asker = await self._resolve_asker()
            if asker is None:
                self._finish_failed(
                    ticket, "substrate_unavailable",
                    "fast_path_qa could not be imported",
                    started,
                )
                return

            situation = await compose_situation()
            grounding = build_grounding(situation)
            report = await self._invoke(asker, ticket, grounding)
            if report is None:
                self._finish_failed(
                    ticket, "provider_failed",
                    "the answering substrate raised",
                    started,
                )
                return

            verdict = getattr(
                getattr(report, "verdict", None), "value", "",
            ) or str(getattr(report, "verdict", "") or "")
            artifact = getattr(report, "artifact", None)
            diagnostic = str(getattr(report, "diagnostic", "") or "")

            if artifact is None or not str(
                getattr(artifact, "answer", "") or ""
            ).strip():
                self._finish_failed(
                    ticket, verdict or "unresolved", diagnostic, started,
                )
                return

            cost = float(getattr(artifact, "cost_usd", 0.0) or 0.0)
            resolved = self._transition(
                ticket.ref,
                state=TicketState.ANSWERED,
                resolved_at=self._clock(),
                answer=str(getattr(artifact, "answer", "") or ""),
                answer_ref=str(getattr(artifact, "ref", "") or ""),
                verdict=verdict or "answered",
                diagnostic=diagnostic,
                cost_usd=cost,
                model=str(getattr(artifact, "model", "") or ""),
                elapsed_s=max(0.0, self._clock() - started),
                grounded_by=situation.readers,
            )
            if resolved is None:
                return  # cancelled while the provider was working
            with self._lock:
                self.answered += 1
                self.cost_usd += cost
            await self._deliver(resolved, render_answer(resolved))
        except asyncio.CancelledError:
            # Cancellation is the operator's `/btw cancel`. The ticket is
            # already CANCELLED by `cancel()`; nothing more to say.
            raise
        except Exception:  # noqa: BLE001
            logger.debug("[SideChannel] answer degraded", exc_info=True)
            self._finish_failed(
                ticket, "internal_error",
                "the side channel failed while answering (see debug log)",
                started,
            )
        finally:
            with self._lock:
                self._inflight.pop(ticket.ref, None)
            sem = self._sem
            if sem is not None:
                try:
                    sem.release()
                except Exception:  # noqa: BLE001
                    pass
            self._signal()

    async def _invoke(
        self, asker: Callable[..., Any], ticket: SideQuestion, grounding: str,
    ) -> Optional[Any]:
        """Call the substrate, tolerating a build that predates
        ``grounding``. NEVER raises.

        The keyword is passed on the first attempt and dropped on a
        ``TypeError`` naming it, so a rollback of the substrate does not
        take the lane with it — the aside merely loses its situation.
        """
        try:
            result = asker(
                ticket.text,
                op_id=f"btw-{ticket.ref}",
                grounding=grounding,
            )
            return await result if asyncio.iscoroutine(result) else result
        except asyncio.CancelledError:
            raise
        except TypeError as exc:
            if "grounding" not in str(exc):
                logger.debug("[SideChannel] asker rejected call: %r", exc)
                return None
            try:
                result = asker(ticket.text, op_id=f"btw-{ticket.ref}")
                return await result if asyncio.iscoroutine(result) else result
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.debug("[SideChannel] asker degraded", exc_info=True)
                return None
        except Exception:  # noqa: BLE001
            logger.debug("[SideChannel] asker degraded", exc_info=True)
            return None

    def _finish_failed(
        self, ticket: SideQuestion, verdict: str, diagnostic: str,
        started: float,
    ) -> None:
        """Terminalise + deliver a non-answer. NEVER raises.

        A verdict is DELIVERED, not logged: `disabled` and
        `budget_exhausted` are the two most likely outcomes on a fresh
        install, and an operator who sees nothing cannot tell them from
        a lane that swallowed their question.
        """
        try:
            resolved = self._transition(
                ticket.ref,
                state=TicketState.FAILED,
                resolved_at=self._clock(),
                verdict=verdict,
                diagnostic=diagnostic,
                elapsed_s=max(0.0, self._clock() - started),
            )
            if resolved is None:
                return
            with self._lock:
                self.failed += 1
            markup = render_failure(resolved)
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                # No loop (a sync caller). Deliver inline — there is no
                # overlay to wait out on a surface with no event loop.
                emit_markup(markup, resolved.session)
                return
            # SUPERVISED. A bare `create_task` here holds the only
            # reference to the delivery, so a failure inside it would
            # surface at garbage-collection time or not at all — and the
            # thing being delivered is the notice that something already
            # went wrong.
            try:
                from backend.core.ouroboros.battle_test.panic_arbiter import (  # noqa: E501
                    spawn_supervised,
                )
                spawn_supervised(
                    self._deliver(resolved, markup),
                    origin=f"side_channel.deliver_failure:{resolved.ref}",
                )
            except Exception:  # noqa: BLE001
                asyncio.ensure_future(self._deliver(resolved, markup))
        except Exception:  # noqa: BLE001
            logger.debug("[SideChannel] failure delivery degraded",
                         exc_info=True)

    async def _deliver(self, ticket: SideQuestion, markup: str) -> None:
        """Put the answer on the operator's screen. NEVER raises.

        Held while a modal overlay owns the terminal — an Iron Gate
        prompt is a decision in progress and painting an aside over it
        is precisely the interruption this lane exists to avoid. The
        hold is BOUNDED, because a gate the operator walked away from
        must not swallow the answer with it.
        """
        try:
            cleared = await _await_clear_screen(delivery_hold_s())
            if not cleared:
                logger.debug(
                    "[SideChannel] %s delivered over a held overlay",
                    ticket.ref,
                )
            emit_markup(markup, ticket.session)
        except asyncio.CancelledError:
            # Losing the delivery task must not lose the answer: it is
            # parked as `q-N` and re-readable via `/expand`, and the
            # ledger row still says ANSWERED.
            raise
        except Exception:  # noqa: BLE001
            logger.debug("[SideChannel] deliver degraded", exc_info=True)

    # -- lifecycle -------------------------------------------------------

    def abandon_live_sync(
        self, reason: str = "session ended before this was answered",
    ) -> Tuple[SideQuestion, ...]:
        """Terminalise every unanswered aside and SAY so. NEVER raises.

        SYNCHRONOUS and asyncio-free ON PURPOSE. The termination-hook
        phase this is registered at runs on a ``threading.Thread``
        precisely so it survives a wedged event loop — so it must not
        touch a Task, a Future, or a loop, because on a wedged loop
        every one of those is either a no-op or a hang. Cancellation of
        in-flight work is :meth:`aclose`'s job, on the loop, where it is
        safe.

        The notice is emitted at WARNING as well as to the operator
        surface: a shutdown is exactly when the console may already be
        torn down, and an unanswered question that leaves no trace
        anywhere is the drop this module refuses everywhere else.
        """
        stranded: List[SideQuestion] = []
        try:
            self._closed = True
            with self._lock:
                for ref, ticket in list(self._tickets.items()):
                    if not ticket.state.is_live:
                        continue
                    updated = replace(
                        ticket,
                        state=TicketState.ABANDONED,
                        resolved_at=self._clock(),
                        diagnostic=str(reason)[:200],
                    )
                    self._tickets[ref] = updated
                    stranded.append(updated)
            if not stranded:
                return ()
            refs = ", ".join(t.ref for t in stranded)
            logger.warning(
                "[SideChannel] %d side question(s) unanswered at "
                "shutdown: %s", len(stranded), refs,
            )
            emit_markup(
                f"  [dim]\u26a0 {len(stranded)} side question(s) "
                f"unanswered at shutdown: {refs}[/dim]",
                stranded[0].session,
            )
        except Exception:  # noqa: BLE001
            logger.debug("[SideChannel] abandon degraded", exc_info=True)
        return tuple(stranded)

    async def aclose(self) -> None:
        """Stop the lane on the loop that owns it. NEVER raises.

        Cancels the worker and any in-flight provider call FIRST — a
        cancelled answer must not race the abandonment and flip a
        ticket back to ANSWERED — then reports what was never answered
        through the sync path both exits share.
        """
        self._closed = True
        try:
            self._signal()
            worker = self._worker
            if worker is not None:
                worker.cancel()
            with self._lock:
                tasks = list(self._inflight.values())
                self._inflight.clear()
            for task in tasks:
                try:
                    task.cancel()
                except Exception:  # noqa: BLE001
                    pass
            self.abandon_live_sync()
        except Exception:  # noqa: BLE001
            logger.debug("[SideChannel] close degraded", exc_info=True)


# ===========================================================================
# Module singleton
# ===========================================================================


_CHANNEL: Optional[SideChannel] = None
_CHANNEL_LOCK = threading.RLock()


def get_default_side_channel() -> SideChannel:
    """The process-wide aside lane. NEVER raises."""
    global _CHANNEL
    with _CHANNEL_LOCK:
        if _CHANNEL is None:
            _CHANNEL = SideChannel()
        return _CHANNEL


def reset_default_side_channel_for_tests() -> None:
    """Drop the singleton and reinstall the default readers. NEVER
    raises. Does NOT await ``aclose`` — a test without a loop must be
    able to call this."""
    global _CHANNEL
    with _CHANNEL_LOCK:
        _CHANNEL = None
    with _READERS_LOCK:
        _SITUATION_READERS.clear()
    _install_default_readers()
    set_answer_sink(None)
    _PRESSURE.clear()


def ask_aside(
    text: object, session: Optional[str] = None,
) -> SubmitOutcome:
    """Module-level submit against the default channel. NEVER raises."""
    try:
        return get_default_side_channel().submit(text, session)
    except Exception:  # noqa: BLE001
        return SubmitOutcome(accepted=False, reason="side channel unavailable")


def render_ref(ref: object) -> str:
    """Re-render one ticket for ``/expand s-N``. NEVER raises.

    Lives here so the REPL's prefix ladder and the `/btw` verb render
    the SAME thing — the divergence between two renderings of one
    artifact is the defect the ref rings were built to avoid.
    """
    try:
        handle = str(ref or "").strip()
        ticket = get_default_side_channel().lookup(handle)
        if ticket is None:
            return f"  [dim]{_escape(handle)}: no such side question[/dim]"
        if ticket.state is TicketState.ANSWERED:
            return render_answer(ticket)
        if ticket.state is TicketState.FAILED:
            return render_failure(ticket)
        return render_ticket_line(ticket)
    except Exception:  # noqa: BLE001
        return "  [dim]side question unavailable[/dim]"


# ===========================================================================
# Termination hook — an unanswered aside must not vanish with the process
# ===========================================================================


TERMINATION_HOOK_NAME: str = "side_channel_abandon_pending"


def _abandon_pending_hook(context: Any) -> None:  # noqa: ANN001
    """Termination hook body. Sync, loop-free, NEVER raises.

    Reads nothing from *context*: what happens to a stranded question
    is the same whether the process is exiting cleanly, on SIGTERM, or
    at a wall-clock cap. The cause belongs in the log line the
    dispatcher already writes, not in a branch here.
    """
    try:
        channel = _CHANNEL
        if channel is None:
            return  # the lane was never used — nothing to strand
        channel.abandon_live_sync(
            reason=str(getattr(context, "stop_reason", "") or "")
            or "session ended before this was answered",
        )
    except Exception:  # noqa: BLE001
        logger.debug("[SideChannel] termination hook degraded",
                     exc_info=True)


def register_termination_hooks(registry: Any) -> int:  # noqa: ANN001
    """Module-owned shutdown registration, auto-discovered by
    ``termination_hook_registry.discover_module_provided_hooks``.

    Registered at ``PRE_SHUTDOWN_EVENT_SET`` — the phase that runs on
    threads, before the loop is asked to stop — because that is the
    only phase guaranteed to run when the loop is the thing that broke.
    Priority is deliberately LOW-precedence (a large number): the
    partial-summary writer at priority 10 is the safety write, and an
    aside notice must never be able to preempt it.

    NEVER raises; returns the count of NEW registrations.
    """
    try:
        from backend.core.ouroboros.battle_test.termination_hook import (
            TerminationPhase,
        )
        from backend.core.ouroboros.battle_test.termination_hook_registry import (  # noqa: E501
            DuplicateHookNameError,
        )
    except Exception:  # noqa: BLE001
        return 0
    try:
        registry.register(
            TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            _abandon_pending_hook,
            name=TERMINATION_HOOK_NAME,
            priority=500,
            enabled_check=side_channel_enabled,
        )
        return 1
    except DuplicateHookNameError:
        return 0  # idempotent on re-import
    except Exception as exc:  # noqa: BLE001
        logger.debug("[SideChannel] hook registration degraded: %s", exc)
        return 0


# ===========================================================================
# Shipped-code invariants — the properties a refactor must not quietly lose
# ===========================================================================


_TARGET_FILE = "backend/core/ouroboros/governance/side_channel.py"

#: Imports this lane must never acquire. It carries text and decides
#: WHEN to ask; it must never be able to decide WHAT to do.
_FORBIDDEN_IMPORT_TOKENS: Tuple[str, ...] = (
    "orchestrator", "iron_gate", "policy_engine", "change_engine",
    "candidate_generator", "urgency_router", "semantic_guardian",
    "tool_executor", "auto_committer", "risk_tier_floor",
)


def register_shipped_invariants() -> list:
    """AST pins, auto-discovered by the shipped-code-invariants walker.

    The unit suite proves these hold TODAY. These pins make a later
    refactor that loses one fail the audit rather than fail silently —
    every property here is one whose loss is invisible at runtime until
    it costs something.
    """
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except Exception:  # noqa: BLE001 — defensive
        return []

    import ast as _ast

    def _find_func(tree: object, name: str) -> object:
        for node in _ast.walk(tree):  # type: ignore[arg-type]
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                if node.name == name:
                    return node
        return None

    def _validate_submit_is_sync(tree, source) -> tuple:  # noqa: ANN001
        del source
        node = _find_func(tree, "submit")
        if node is None:
            return ("SideChannel.submit is missing",)
        if isinstance(node, _ast.AsyncFunctionDef):
            # THE defect this module exists to remove. An async submit
            # puts a provider round-trip back on the operator input
            # queue's single consumer, which is `/ask`.
            return ("submit became a coroutine — the blocking path is back",)
        return ()

    def _validate_termination_hook_is_loop_free(tree, source) -> tuple:  # noqa: ANN001, E501
        del source
        node = _find_func(tree, "abandon_live_sync")
        if node is None:
            return ("abandon_live_sync is missing",)
        if isinstance(node, _ast.AsyncFunctionDef):
            return ("abandon_live_sync became a coroutine",)
        for sub in _ast.walk(node):
            if isinstance(sub, (_ast.Await, _ast.AsyncWith, _ast.AsyncFor)):
                return ("abandon_live_sync awaits — it runs on a thread "
                        "precisely so it survives a wedged loop",)
            if (isinstance(sub, _ast.Attribute)
                    and isinstance(sub.value, _ast.Name)
                    and sub.value.id == "asyncio"):
                return (f"abandon_live_sync touches asyncio.{sub.attr}",)
        return ()

    def _validate_admission_does_not_probe(tree, source) -> tuple:  # noqa: ANN001, E501
        del source
        node = _find_func(tree, "assess_admission")
        if node is None:
            return ("assess_admission is missing",)
        for sub in _ast.walk(node):
            if (isinstance(sub, _ast.Call)
                    and isinstance(sub.func, _ast.Name)
                    and sub.func.id == "sample_memory_pressure_sync"):
                # The gate runs on the event loop; the probe cascade
                # ends in a subprocess. Reading the cache is the whole
                # point of there being a cache.
                return ("assess_admission probes memory directly — that "
                        "is a subprocess on the event loop",)
        return ()

    def _validate_authority_asymmetry(tree, source) -> tuple:  # noqa: ANN001
        del source
        offenders = []
        for node in _ast.walk(tree):
            names = []
            if isinstance(node, _ast.ImportFrom) and node.module:
                names.append(node.module)
            elif isinstance(node, _ast.Import):
                names.extend(a.name for a in node.names)
            for name in names:
                if any(tok in name for tok in _FORBIDDEN_IMPORT_TOKENS):
                    offenders.append(name)
        if offenders:
            return (f"authority imports acquired: {sorted(set(offenders))}",)
        return ()

    def _validate_no_parallel_provider(tree, source) -> tuple:  # noqa: ANN001
        del source
        for node in _ast.walk(tree):
            names = []
            if isinstance(node, _ast.ImportFrom) and node.module:
                names.append(node.module)
            elif isinstance(node, _ast.Import):
                names.extend(a.name for a in node.names)
            for name in names:
                if "anthropic" in name or name.endswith("providers"):
                    return (f"parallel provider client: {name}",)
        return ()

    def _validate_ticket_state_closed(tree, source) -> tuple:  # noqa: ANN001
        del source
        expected = {
            "PENDING", "DEFERRED", "ASKING", "ANSWERED", "FAILED",
            "CANCELLED", "ABANDONED",
        }
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ClassDef) and node.name == "TicketState":
                found = {
                    t.id
                    for stmt in node.body
                    if isinstance(stmt, _ast.Assign)
                    for t in stmt.targets
                    if isinstance(t, _ast.Name)
                }
                if found != expected:
                    return (f"TicketState members {sorted(found)} != "
                            f"{sorted(expected)}",)
                return ()
        return ("TicketState is missing",)

    return [
        ShippedCodeInvariant(
            invariant_name="side_channel_submit_is_sync",
            target_file=_TARGET_FILE,
            description=(
                "SideChannel.submit must stay synchronous — an async "
                "submit puts a provider round-trip back on the operator "
                "input queue's single consumer."
            ),
            validate=_validate_submit_is_sync,
        ),
        ShippedCodeInvariant(
            invariant_name="side_channel_termination_hook_loop_free",
            target_file=_TARGET_FILE,
            description=(
                "abandon_live_sync runs on a termination thread so it "
                "survives a wedged loop; it must touch no asyncio."
            ),
            validate=_validate_termination_hook_is_loop_free,
        ),
        ShippedCodeInvariant(
            invariant_name="side_channel_admission_reads_never_probes",
            target_file=_TARGET_FILE,
            description=(
                "assess_admission runs on the event loop; the memory "
                "probe ends in a subprocess and must stay behind the "
                "TTL cache refreshed by refresh_memory_pressure."
            ),
            validate=_validate_admission_does_not_probe,
        ),
        ShippedCodeInvariant(
            invariant_name="side_channel_authority_asymmetry",
            target_file=_TARGET_FILE,
            description=(
                "The lane decides WHEN to ask, never WHAT to do: no "
                "orchestrator / iron_gate / policy / change_engine / "
                "tool_executor imports."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name="side_channel_no_parallel_provider",
            target_file=_TARGET_FILE,
            description=(
                "fast_path_qa ANSWERS; this module only SCHEDULES. A "
                "second provider client here would be a second opinion "
                "about cost, budget and who the answerer is."
            ),
            validate=_validate_no_parallel_provider,
        ),
        ShippedCodeInvariant(
            invariant_name="side_channel_ticket_state_closed",
            target_file=_TARGET_FILE,
            description=(
                "TicketState is a closed 7-value lifecycle; DEFERRED "
                "stays distinct from PENDING because 'waiting its turn' "
                "and 'waiting for room' are different answers to an "
                "operator asking whether they were heard."
            ),
            validate=_validate_ticket_state_closed,
        ),
    ]


# ===========================================================================
# §33.1 flag registry — module-owned specs, auto-discovered
# ===========================================================================


def register_flags(registry: Any) -> int:  # noqa: ANN001
    """Declare this module's knobs to the canonical FlagRegistry.

    Discovered by ``flag_registry_seed._discover_module_provided_flags``
    — no edit there. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category,
            FlagSpec,
            FlagType,
        )
    except Exception:  # noqa: BLE001
        return 0
    src = "backend/core/ouroboros/governance/side_channel.py"
    # Built inside the guard on purpose. The discovery loop swallows
    # exceptions from this function, so an enum member that gets renamed
    # upstream would otherwise register ZERO flags and say nothing —
    # `/help flags` would simply stop knowing about this lane. A WARNING
    # is the difference between a bug and a mystery.
    try:
        specs = [
            (ENV_MASTER, FlagType.BOOL, True, Category.SAFETY,
             "Side-question lane (`/btw`) master. ON = asides are accepted "
             "and answered out-of-band without blocking the operator input "
             "queue. OFF = `/btw` declines at submit, naming this flag.",
             "true"),
            (ENV_QUEUE_DEPTH, FlagType.INT, 16, Category.CAPACITY,
             "Unanswered side questions held before refusing. Refuses, never "
             "drops. Clamped [1, 256].", "16"),
            (ENV_LEDGER_SIZE, FlagType.INT, 50, Category.CAPACITY,
             "Capacity of the s-N ticket ring. Only TERMINAL tickets are "
             "evicted. Clamped [4, 1000].", "50"),
            (ENV_CONCURRENCY, FlagType.INT, 1, Category.CAPACITY,
             "Side questions that may be in a provider call at once. "
             "Clamped [1, 8].", "1"),
            (ENV_ADMISSION, FlagType.BOOL, True, Category.TUNING,
             "Non-preemptive admission control. OFF answers immediately "
             "regardless of load — the A/B and the rollback.", "true"),
            (ENV_OPS_HEADROOM, FlagType.INT, 2, Category.TUNING,
             "In-flight ops at or above which an aside waits. 0 disables the "
             "ops dimension. Clamped [0, 64].", "2"),
            (ENV_DEFER_MAX_S, FlagType.FLOAT, 120.0, Category.TIMING,
             "Starvation ceiling: after this an aside is admitted under load "
             "and the answer says so. 0 = never defer. Clamped [0, 3600].",
             "120"),
            (ENV_DEFER_BASE_S, FlagType.FLOAT, 1.5, Category.TIMING,
             "Base of the jittered exponential re-check backoff. Clamped "
             "[0.1, 60].", "1.5"),
            (ENV_DEFER_CEILING_S, FlagType.FLOAT, 15.0, Category.TIMING,
             "Longest single wait between admission re-checks. Clamped "
             "[0.5, 300].", "15"),
            (ENV_PRESSURE_TTL_S, FlagType.FLOAT, 15.0, Category.TIMING,
             "Age past which a memory-pressure reading is retired to "
             "UNKNOWN rather than believed. Never decays to OK. Clamped "
             "[1, 600].", "15"),
            (ENV_PRESSURE_HOLD, FlagType.STR, "critical", Category.TUNING,
             "Comma-separated memory-pressure levels that hold a side "
             "question (ok / warn / high / critical). Default 'critical' "
             "only: an aside is one bounded read-only call, not the "
             "fan-out the pressure gate exists to bound.",
             "high,critical"),
            (ENV_SITUATION, FlagType.BOOL, True, Category.OBSERVABILITY,
             "Ground each aside in a live-situation digest (status line, "
             "in-flight streams, narrative, recent ops, posture, queue).",
             "true"),
            (ENV_SITUATION_BUDGET_S, FlagType.FLOAT, 1.5, Category.TIMING,
             "Wall-clock ceiling on composing the digest, off-loop. A slow "
             "reader costs context, never the answer. Clamped [0.1, 30].",
             "1.5"),
            (ENV_SITUATION_MAX_CHARS, FlagType.INT, 2000, Category.TUNING,
             "Character ceiling on the digest handed to the provider. "
             "Clamped [200, 20000].", "2000"),
            (ENV_SITUATION_READERS, FlagType.STR, "", Category.OBSERVABILITY,
             "Comma-separated allowlist of situation readers (status, "
             "inflight, narrative, ops, posture, queue). Empty = all.",
             "status,inflight"),
            (ENV_DELIVERY_HOLD_S, FlagType.FLOAT, 20.0, Category.TIMING,
             "How long an answer waits for a modal overlay (Iron Gate "
             "prompt, diff preview) to clear before painting anyway. 0 "
             "delivers immediately. Clamped [0, 300].", "20"),
            (ENV_CHARTER, FlagType.STR, "", Category.OBSERVABILITY,
             "Override the side-question charter prepended to the grounding "
             "block. Empty = the built-in no-authority charter.", ""),
            (ENV_MAX_QUESTION_CHARS, FlagType.INT, 2000, Category.SAFETY,
             "Truncation ceiling on one side question. Clamped [32, 4096].",
             "2000"),
        ]
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[SideChannel] flag specs could not be built (%r) — this "
            "lane's knobs will be missing from /help flags", exc,
        )
        return 0
    count = 0
    for name, ftype, default, category, description, example in specs:
        try:
            registry.register(FlagSpec(
                name=name,
                type=ftype,
                default=default,
                description=description,
                category=category,
                source_file=src,
                example=f"{name}={example}" if example else name,
            ))
            count += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug("[SideChannel] register_flags degraded: %s", exc)
    return count


__all__ = [
    "Admission",
    "AdmissionState",
    "AnswerSink",
    "BTW_LANE",
    "ChannelSnapshot",
    "ENV_MASTER",
    "REF_PREFIX",
    "SIDE_CHANNEL_SCHEMA_VERSION",
    "SideChannel",
    "SideQuestion",
    "SituationBlock",
    "SituationReader",
    "SubmitOutcome",
    "TERMINATION_HOOK_NAME",
    "TicketState",
    "admission_enabled",
    "answer_sink_bound",
    "ask_aside",
    "assess_admission",
    "assess_admission_now",
    "build_grounding",
    "charter",
    "compose_situation",
    "compose_situation_sync",
    "concurrency",
    "defer_max_s",
    "delivery_hold_s",
    "emit_markup",
    "get_default_side_channel",
    "ledger_size",
    "ops_headroom",
    "pressure_hold_levels",
    "pressure_ttl_s",
    "queue_depth",
    "refresh_memory_pressure",
    "register_flags",
    "register_shipped_invariants",
    "register_situation_reader",
    "register_termination_hooks",
    "render_ack",
    "render_answer",
    "render_failure",
    "render_ledger",
    "render_ref",
    "render_ticket_line",
    "reset_default_side_channel_for_tests",
    "sample_memory_pressure_sync",
    "set_answer_sink",
    "side_channel_enabled",
    "situation_enabled",
    "situation_reader_names",
    "unregister_situation_reader",
]
