"""
InlinePermissionPrompt — Slice 2 of the Inline Permission Prompts arc.
======================================================================

Three load-bearing primitives live here:

* :class:`InlinePromptController` — Future-backed per-prompt registry.
  Operator answers (allow/deny/always/pause) resolve a Future exactly
  once. Timeout → auto-deny. Mirrors :class:`PlanApprovalController`'s
  shape (Problem #7) so both observability consumers (IDE broker /
  SerpentFlow / webhook) can reuse the same mental model.

* :class:`BlessedShapeLedger` — per-op record of *risk shapes* already
  blessed by an upstream human gate (NOTIFY_APPLY's 5s reject window,
  PlanApproval's halt-before-generate, OrangePR review). The ledger
  answers one question: "has a human already authorized a tool call
  of this exact shape in this op?" When the answer is yes, the inline
  gate must NOT re-prompt (double-ask guard). This is the lock the user
  pinned in prose + tests rather than leaving implicit.

* :class:`InlinePermissionMiddleware` — the single composed entry point
  callers use. It runs :func:`inline_permission.decide` (Slice 1), then
  checks the ledger, then registers a prompt with the controller and
  awaits the Future. Returns an :class:`InlineMiddlewareOutcome` that
  the ToolExecutor translates into a :class:`PolicyResult`.

Manifesto alignment
-------------------

* §1 — execution authority is deterministic. The gate / ledger answer
  the yes/no question; the model-generated ``rationale`` string is
  display-only.
* §3 — the Future blocks *only* the awaiting coroutine. Rendering is
  sync + non-blocking; the event loop keeps running.
* §5 — Tier 0 fast path (SAFE / BLOCK / blessed) never touches the
  operator. Only the ASK verdict with no blessing reaches a prompt.
* §6 — additive to :class:`PolicyEngine` and :class:`InlinePermissionGate`.
  Never weakens an upstream BLOCK.
* §7 — fail-closed: broken renderer / lost Future / registry overflow
  all resolve to DENY, never silent allow.
* §8 — every state transition emits an INFO log line keyed by
  ``[InlinePrompt]``. Controller keeps a bounded history for postmortems.
"""
from __future__ import annotations

import asyncio
import enum
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass
from typing import (
    Any, Callable, Dict, FrozenSet, List, Optional, Protocol,
    runtime_checkable,
)

from backend.core.ouroboros.governance.inline_permission import (
    InlineDecision,
    InlineGateInput,
    InlineGateVerdict,
    InlinePermissionGate,
    OpApprovedScope,
    RoutePosture,
    UpstreamPolicy,
)

logger = logging.getLogger("Ouroboros.InlinePrompt")


# ---------------------------------------------------------------------------
# Env knobs
# ---------------------------------------------------------------------------


def inline_permission_enabled() -> bool:
    """Master switch for the ToolExecutor hook. Default OFF (Slice 5 graduates).

    Slices 1–4 ship the primitive. The operator opts in by setting
    ``JARVIS_INLINE_PERMISSION_ENABLED=true``. Slice 5 flips the default
    after a ``3-clean-session`` graduation arc.
    """
    return os.environ.get(
        "JARVIS_INLINE_PERMISSION_ENABLED", "false",
    ).strip().lower() == "true"


def _default_prompt_timeout_s() -> float:
    try:
        return max(1.0, float(os.environ.get(
            "JARVIS_INLINE_PERMISSION_TIMEOUT_S", "120",
        )))
    except (TypeError, ValueError):
        return 120.0


def _max_pending_prompts() -> int:
    try:
        return max(1, int(os.environ.get(
            "JARVIS_INLINE_PERMISSION_MAX_PENDING", "16",
        )))
    except (TypeError, ValueError):
        return 16


def _reason_max_len() -> int:
    try:
        return max(1, int(os.environ.get(
            "JARVIS_INLINE_PERMISSION_REASON_MAX_LEN", "2000",
        )))
    except (TypeError, ValueError):
        return 2000


def _bless_default_ttl_s() -> float:
    try:
        return max(1.0, float(os.environ.get(
            "JARVIS_INLINE_PERMISSION_BLESS_TTL_S", "3600",
        )))
    except (TypeError, ValueError):
        return 3600.0


# ---------------------------------------------------------------------------
# State machine + response kinds
# ---------------------------------------------------------------------------


STATE_PENDING = "pending"
STATE_ALLOWED = "allowed"
STATE_DENIED = "denied"
STATE_EXPIRED = "expired"
STATE_PAUSED = "paused"

_TERMINAL_STATES: FrozenSet[str] = frozenset({
    STATE_ALLOWED, STATE_DENIED, STATE_EXPIRED, STATE_PAUSED,
})


class ResponseKind(str, enum.Enum):
    """What the operator chose on the inline prompt."""

    ALLOW_ONCE = "allow_once"
    ALLOW_ALWAYS = "allow_always"  # Slice 3 will persist; Slice 2 treats as allow_once
    DENY = "deny"
    PAUSE_OP = "pause_op"


# ---------------------------------------------------------------------------
# Request / outcome dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InlinePromptRequest:
    """Payload rendered to the operator and recorded for §8 audit.

    Note: ``rationale`` is model-generated *display text only*. It never
    flows into authorization (§1).
    """

    prompt_id: str
    op_id: str
    call_id: str
    tool: str
    arg_fingerprint: str       # full, unredacted — renderer may truncate
    arg_preview: str           # pre-truncated ≤ 200 chars for REPL display
    target_path: str
    verdict: InlineGateVerdict
    rationale: str = ""
    route: RoutePosture = RoutePosture.INTERACTIVE
    upstream_decision: UpstreamPolicy = UpstreamPolicy.NO_MATCH
    created_ts: float = 0.0
    timeout_s: float = 0.0


@dataclass(frozen=True)
class InlinePromptOutcome:
    """Result delivered to the ``await``-ing middleware."""

    prompt_id: str
    state: str                 # STATE_ALLOWED / STATE_DENIED / STATE_EXPIRED / STATE_PAUSED
    response: Optional[ResponseKind]
    operator_reason: str = ""
    reviewer: str = ""         # "repl" / "ide" / "auto-timeout"
    elapsed_s: float = 0.0

    @property
    def allowed(self) -> bool:
        return self.state == STATE_ALLOWED

    @property
    def remembered(self) -> bool:
        return self.response is ResponseKind.ALLOW_ALWAYS and self.state == STATE_ALLOWED


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class InlinePromptError(Exception):
    """Base for InlinePrompt errors."""


class InlinePromptStateError(InlinePromptError):
    """Illegal state transition."""


class InlinePromptCapacityError(InlinePromptError):
    """Registry full."""


# ---------------------------------------------------------------------------
# Renderer Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class InlinePromptRenderer(Protocol):
    """Operator-facing renderer (SerpentFlow REPL, IDE surface, etc.).

    Synchronous, non-blocking. The actual waiting is done by the
    middleware awaiting the controller's Future; the renderer's sole
    responsibility is to display / dismiss.
    """

    def render(self, request: InlinePromptRequest) -> None: ...

    def dismiss(
        self, prompt_id: str, outcome: InlinePromptOutcome,
    ) -> None: ...


class _NullRenderer:
    """No-op renderer. Used when the operator surface isn't attached yet.

    The prompt is still registered (audit trail intact), the Future is
    still awaited, the timeout still fires. Tests can assert behavior
    without any REPL.
    """

    def render(self, request: InlinePromptRequest) -> None:
        logger.info(
            "[InlinePrompt] render(null) prompt_id=%s tool=%s target=%s",
            request.prompt_id, request.tool, request.target_path,
        )

    def dismiss(
        self, prompt_id: str, outcome: InlinePromptOutcome,
    ) -> None:
        logger.info(
            "[InlinePrompt] dismiss(null) prompt_id=%s state=%s",
            prompt_id, outcome.state,
        )


# ---------------------------------------------------------------------------
# Pending record (internal)
# ---------------------------------------------------------------------------


@dataclass
class _PendingPrompt:
    """One pending prompt awaiting operator action."""

    request: InlinePromptRequest
    future: "asyncio.Future[InlinePromptOutcome]"
    state: str = STATE_PENDING
    response: Optional[ResponseKind] = None
    reviewer: str = ""
    operator_reason: str = ""
    _timeout_task: Optional["asyncio.Task[None]"] = None

    @property
    def is_terminal(self) -> bool:
        return self.state in _TERMINAL_STATES


# ---------------------------------------------------------------------------
# InlinePromptController — Future-backed registry
# ---------------------------------------------------------------------------


class InlinePromptController:
    """Per-process registry of pending inline prompts.

    Thread-safe. Future resolution uses ``loop.call_soon_threadsafe``
    so an answer arriving from a different thread (REPL input thread,
    IDE webhook handler) resolves cleanly.
    """

    def __init__(
        self,
        *,
        max_pending: Optional[int] = None,
        default_timeout_s: Optional[float] = None,
    ) -> None:
        self._max_pending = max_pending or _max_pending_prompts()
        self._default_timeout_s = default_timeout_s or _default_prompt_timeout_s()
        self._pending: Dict[str, _PendingPrompt] = {}
        self._history: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._listeners: List[Callable[[Dict[str, Any]], None]] = []
        self._history_max = 256

    # --- introspection ---------------------------------------------------

    @property
    def pending_count(self) -> int:
        with self._lock:
            return sum(
                1 for p in self._pending.values()
                if p.state == STATE_PENDING
            )

    def pending_ids(self) -> List[str]:
        with self._lock:
            return [
                pid for pid, p in self._pending.items()
                if p.state == STATE_PENDING
            ]

    def snapshot(self, prompt_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            p = self._pending.get(prompt_id)
            return None if p is None else self._project(p)

    def snapshot_all(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [self._project(p) for p in self._pending.values()]

    def history(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._history)

    @staticmethod
    def _project(p: _PendingPrompt) -> Dict[str, Any]:
        r = p.request
        return {
            "prompt_id": r.prompt_id,
            "op_id": r.op_id,
            "call_id": r.call_id,
            "tool": r.tool,
            "target_path": r.target_path,
            "arg_preview": r.arg_preview,
            "verdict_rule_id": r.verdict.rule_id,
            "verdict_decision": r.verdict.decision.value,
            "state": p.state,
            "response": p.response.value if p.response is not None else None,
            "reviewer": p.reviewer,
            "operator_reason": p.operator_reason,
            "created_ts": r.created_ts,
            "timeout_s": r.timeout_s,
            "expires_ts": r.created_ts + r.timeout_s,
        }

    # --- listeners -------------------------------------------------------

    def on_transition(
        self, listener: Callable[[Dict[str, Any]], None],
    ) -> Callable[[], None]:
        """Slice 4 IDE broker subscribes here; returns an unsubscribe handle."""
        with self._lock:
            self._listeners.append(listener)

        def _unsub() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return _unsub

    def _fire(self, event_type: str, pending: _PendingPrompt) -> None:
        payload = {
            "event_type": event_type,
            "projection": self._project(pending),
        }
        for fn in list(self._listeners):
            try:
                fn(payload)
            except Exception as exc:  # noqa: BLE001
                logger.debug("[InlinePrompt] listener exception: %s", exc)

    # --- request ---------------------------------------------------------

    def request(
        self,
        request: InlinePromptRequest,
        *,
        timeout_s: Optional[float] = None,
    ) -> "asyncio.Future[InlinePromptOutcome]":
        """Register a pending prompt; return a Future.

        The caller is expected to render the prompt (usually via the
        middleware, which also attaches the renderer). Timeout is
        scheduled on the current event loop.
        """
        if not isinstance(request.prompt_id, str) or not request.prompt_id:
            raise InlinePromptStateError("prompt_id must be a non-empty string")

        loop = asyncio.get_event_loop()
        future: "asyncio.Future[InlinePromptOutcome]" = loop.create_future()
        effective_timeout = (
            timeout_s if timeout_s is not None and timeout_s > 0
            else self._default_timeout_s
        )

        # Stamp created_ts / timeout_s on the request for §8 audit.
        stamped = InlinePromptRequest(
            **{**request.__dict__,
               "created_ts": time.monotonic(),
               "timeout_s": effective_timeout},
        )

        with self._lock:
            if stamped.prompt_id in self._pending:
                existing = self._pending[stamped.prompt_id]
                if not existing.is_terminal:
                    raise InlinePromptStateError(
                        "pending prompt already exists for id="
                        + stamped.prompt_id,
                    )
            non_terminal = sum(
                1 for p in self._pending.values() if not p.is_terminal
            )
            if non_terminal >= self._max_pending:
                raise InlinePromptCapacityError(
                    "inline-prompt registry at capacity ("
                    + str(self._max_pending) + ")",
                )
            pending = _PendingPrompt(request=stamped, future=future)
            self._pending[stamped.prompt_id] = pending

        # Timeout scheduled OUTSIDE the lock.
        pending._timeout_task = loop.create_task(
            self._run_timeout(stamped.prompt_id, effective_timeout),
            name="inline-prompt-timeout-" + stamped.prompt_id,
        )

        logger.info(
            "[InlinePrompt] pending prompt_id=%s op=%s tool=%s target=%s "
            "rule=%s route=%s timeout_s=%.1f",
            stamped.prompt_id, stamped.op_id, stamped.tool,
            stamped.target_path, stamped.verdict.rule_id,
            stamped.route.value, effective_timeout,
        )
        self._fire("inline_prompt_pending", pending)
        return future

    async def _run_timeout(self, prompt_id: str, timeout_s: float) -> None:
        try:
            await asyncio.sleep(timeout_s)
        except asyncio.CancelledError:
            return
        with self._lock:
            p = self._pending.get(prompt_id)
            if p is None or p.is_terminal:
                return
        try:
            self._resolve(
                prompt_id,
                state=STATE_EXPIRED,
                response=None,
                reviewer="auto-timeout",
                operator_reason="prompt_expired after " + str(int(timeout_s)) + "s",
            )
        except InlinePromptStateError:
            pass

    # --- operator actions ------------------------------------------------

    def allow_once(
        self, prompt_id: str, *, reviewer: str = "", reason: str = "",
    ) -> InlinePromptOutcome:
        return self._resolve(
            prompt_id,
            state=STATE_ALLOWED,
            response=ResponseKind.ALLOW_ONCE,
            reviewer=reviewer or "unknown",
            operator_reason=reason,
        )

    def allow_always(
        self, prompt_id: str, *, reviewer: str = "", reason: str = "",
    ) -> InlinePromptOutcome:
        return self._resolve(
            prompt_id,
            state=STATE_ALLOWED,
            response=ResponseKind.ALLOW_ALWAYS,
            reviewer=reviewer or "unknown",
            operator_reason=reason,
        )

    def deny(
        self, prompt_id: str, *, reviewer: str = "", reason: str = "",
    ) -> InlinePromptOutcome:
        return self._resolve(
            prompt_id,
            state=STATE_DENIED,
            response=ResponseKind.DENY,
            reviewer=reviewer or "unknown",
            operator_reason=reason,
        )

    def pause_op(
        self, prompt_id: str, *, reviewer: str = "", reason: str = "",
    ) -> InlinePromptOutcome:
        return self._resolve(
            prompt_id,
            state=STATE_PAUSED,
            response=ResponseKind.PAUSE_OP,
            reviewer=reviewer or "unknown",
            operator_reason=reason,
        )

    def _resolve(
        self,
        prompt_id: str,
        *,
        state: str,
        response: Optional[ResponseKind],
        reviewer: str,
        operator_reason: str,
    ) -> InlinePromptOutcome:
        r = operator_reason.strip() if isinstance(operator_reason, str) else ""
        max_len = _reason_max_len()
        if len(r) > max_len:
            r = r[:max_len] + "...<truncated>"
        with self._lock:
            p = self._pending.get(prompt_id)
            if p is None:
                raise InlinePromptStateError(
                    "no pending prompt for id=" + prompt_id,
                )
            if p.is_terminal:
                raise InlinePromptStateError(
                    "prompt id=" + prompt_id
                    + " already in terminal state " + p.state,
                )
            elapsed = time.monotonic() - p.request.created_ts
            p.state = state
            p.response = response
            p.reviewer = reviewer
            p.operator_reason = r
            outcome = InlinePromptOutcome(
                prompt_id=prompt_id,
                state=state,
                response=response,
                operator_reason=r,
                reviewer=reviewer,
                elapsed_s=elapsed,
            )
            t = p._timeout_task
            if t is not None and not t.done():
                try:
                    t.cancel()
                except RuntimeError:
                    pass
            self._history.append({
                "prompt_id": prompt_id,
                "op_id": p.request.op_id,
                "state": state,
                "response": response.value if response else None,
                "reviewer": reviewer,
                "operator_reason": r,
                "elapsed_s": elapsed,
                "resolved_ts": time.monotonic(),
            })
            if len(self._history) > self._history_max:
                self._history.pop(0)
            future = p.future

        event_type = (
            "inline_prompt_allowed" if state == STATE_ALLOWED
            else "inline_prompt_denied" if state == STATE_DENIED
            else "inline_prompt_expired" if state == STATE_EXPIRED
            else "inline_prompt_paused"
        )
        logger.info(
            "[InlinePrompt] %s prompt_id=%s reviewer=%s elapsed_s=%.1f "
            "response=%s reason=%.200s",
            event_type, prompt_id, reviewer, elapsed,
            response.value if response else "none",
            r or "",
        )
        if not future.done():
            _loop = future.get_loop()
            try:
                _loop.call_soon_threadsafe(future.set_result, outcome)
            except RuntimeError:
                try:
                    future.set_result(outcome)
                except Exception:  # noqa: BLE001
                    pass
        self._fire(event_type, p)
        return outcome

    # --- cleanup ---------------------------------------------------------

    def evict_terminal(self, prompt_id: str) -> bool:
        with self._lock:
            p = self._pending.get(prompt_id)
            if p is None or not p.is_terminal:
                return False
            del self._pending[prompt_id]
        return True

    def reset(self) -> None:
        """Test helper. Never called from production."""
        with self._lock:
            for p in self._pending.values():
                if not p.future.done():
                    try:
                        p.future.cancel()
                    except Exception:  # noqa: BLE001
                        pass
                t = p._timeout_task
                if t is not None and not t.done():
                    try:
                        t.cancel()
                    except RuntimeError:
                        pass
            self._pending.clear()
            self._history.clear()
            self._listeners.clear()


# ---------------------------------------------------------------------------
# Module singleton — mirrors PlanApprovalController pattern
# ---------------------------------------------------------------------------


_default_controller: Optional[InlinePromptController] = None
_default_controller_lock = threading.Lock()


def get_default_controller() -> InlinePromptController:
    global _default_controller
    with _default_controller_lock:
        if _default_controller is None:
            _default_controller = InlinePromptController()
        return _default_controller


# ===========================================================================
# BlessedShapeLedger — the double-ask guard
# ===========================================================================


# Canonical tool-family map. Each family groups tools that share the same
# "risk shape" for blessing purposes. NOTIFY_APPLY blesses edit/write (the
# diff touches those tools); PlanApproval blesses all file-scoped tools
# inside plan-approved paths; bash is its own family because a blessed
# bash command is ALWAYS exact-match (a blessed diff never blesses `bash`).
_TOOL_FAMILY: Dict[str, str] = {
    "edit_file": "edit",
    "write_file": "write",
    "delete_file": "delete",
    "bash": "bash",
    "apply_patch": "edit",
}


def tool_family(tool: str) -> str:
    return _TOOL_FAMILY.get(tool, tool)


class BlessingSource(str, enum.Enum):
    """Upstream gate that emitted the blessing."""

    NOTIFY_APPLY = "notify_apply"
    """NOTIFY_APPLY's 5s reject window; operator implicitly accepted."""

    PLAN_APPROVAL = "plan_approval"
    """PlanApprovalController emitted approve(); plan covers the shape."""

    ORANGE_REVIEW = "orange_review"
    """OrangePRReviewer PR was merged; full op is blessed."""

    ASK_HUMAN = "ask_human"
    """Model asked a clarifying question. Intentionally does NOT bless any
    tool shape — clarification ≠ authorization. Included in the enum so
    audit logs can still record that ask_human was consulted."""


@dataclass(frozen=True)
class BlessedShape:
    """One record in :class:`BlessedShapeLedger`.

    A tool call is "covered" when:
        * its tool-family is in ``tool_families`` (or ``"*"`` present); AND
        * (if file-scoped) its ``target_path`` is nested under any entry
          in ``approved_paths``; AND
        * (if bash-family) its ``arg_fingerprint`` exactly matches one
          of the entries in ``blessed_commands``; AND
        * if ``candidate_hash`` is non-empty, the caller's hash matches.

    ASK_HUMAN blessings are DEGENERATE — empty ``tool_families``,
    ``approved_paths``, ``blessed_commands``. They never match any call.
    This is intentional and pinned by tests.
    """

    source: BlessingSource
    tool_families: FrozenSet[str]
    approved_paths: FrozenSet[str]
    blessed_commands: FrozenSet[str] = frozenset()
    candidate_hash: str = ""
    blessed_at_ts: float = 0.0
    expires_at_ts: float = 0.0
    note: str = ""


def _path_covered_by(target: str, approved: FrozenSet[str]) -> bool:
    if not target:
        return False
    if "*" in approved:
        return True
    for ap in approved:
        if not ap:
            continue
        clean = ap.rstrip("/")
        if target == clean or target.startswith(clean + "/"):
            return True
    return False


class BlessedShapeLedger:
    """Per-op record of human-blessed risk shapes.

    Lifecycle: :meth:`bless` adds; :meth:`find_blessing` reads;
    :meth:`clear_op` purges when the op completes. Entries also expire
    via ``expires_at_ts``.
    """

    def __init__(self, *, default_ttl_s: Optional[float] = None) -> None:
        self._default_ttl_s = default_ttl_s or _bless_default_ttl_s()
        self._by_op: Dict[str, List[BlessedShape]] = {}
        self._lock = threading.Lock()

    # --- write -----------------------------------------------------------

    def bless(
        self,
        op_id: str,
        shape: BlessedShape,
    ) -> None:
        if not op_id:
            raise InlinePromptStateError("op_id must be non-empty")
        with self._lock:
            self._by_op.setdefault(op_id, []).append(shape)
        logger.info(
            "[InlinePrompt.Ledger] bless op=%s source=%s families=%s "
            "paths=%d cmds=%d hash=%s expires_in=%.0fs",
            op_id, shape.source.value,
            sorted(shape.tool_families),
            len(shape.approved_paths),
            len(shape.blessed_commands),
            shape.candidate_hash[:8] or "-",
            max(0.0, shape.expires_at_ts - time.monotonic()),
        )

    def bless_notify_apply(
        self,
        op_id: str,
        *,
        approved_paths: FrozenSet[str],
        candidate_hash: str,
        ttl_s: Optional[float] = None,
        note: str = "",
    ) -> BlessedShape:
        ttl = ttl_s or self._default_ttl_s
        now = time.monotonic()
        shape = BlessedShape(
            source=BlessingSource.NOTIFY_APPLY,
            tool_families=frozenset({"edit", "write"}),
            approved_paths=approved_paths,
            candidate_hash=candidate_hash,
            blessed_at_ts=now,
            expires_at_ts=now + ttl,
            note=note,
        )
        self.bless(op_id, shape)
        return shape

    def bless_plan_approval(
        self,
        op_id: str,
        *,
        approved_paths: FrozenSet[str],
        ttl_s: Optional[float] = None,
        note: str = "",
    ) -> BlessedShape:
        ttl = ttl_s or self._default_ttl_s
        now = time.monotonic()
        shape = BlessedShape(
            source=BlessingSource.PLAN_APPROVAL,
            tool_families=frozenset({"edit", "write", "delete"}),
            approved_paths=approved_paths,
            candidate_hash="",
            blessed_at_ts=now,
            expires_at_ts=now + ttl,
            note=note,
        )
        self.bless(op_id, shape)
        return shape

    def bless_orange_review(
        self,
        op_id: str,
        *,
        approved_paths: FrozenSet[str],
        candidate_hash: str,
        blessed_commands: FrozenSet[str] = frozenset(),
        ttl_s: Optional[float] = None,
        note: str = "",
    ) -> BlessedShape:
        ttl = ttl_s or self._default_ttl_s
        now = time.monotonic()
        shape = BlessedShape(
            source=BlessingSource.ORANGE_REVIEW,
            tool_families=frozenset({"*"}),
            approved_paths=approved_paths,
            blessed_commands=blessed_commands,
            candidate_hash=candidate_hash,
            blessed_at_ts=now,
            expires_at_ts=now + ttl,
            note=note,
        )
        self.bless(op_id, shape)
        return shape

    # --- read ------------------------------------------------------------

    def find_blessing(
        self,
        *,
        op_id: str,
        tool: str,
        target_path: str,
        arg_fingerprint: str,
        candidate_hash: str = "",
    ) -> Optional[BlessedShape]:
        """Return the blessing that covers this tool call, else None.

        First-match wins (ledger order = insertion order).
        """
        if not op_id:
            return None
        family = tool_family(tool)
        now = time.monotonic()

        with self._lock:
            shapes = list(self._by_op.get(op_id, []))

        for shape in shapes:
            # Expired?
            if shape.expires_at_ts and shape.expires_at_ts < now:
                continue
            # Family match?
            if "*" not in shape.tool_families and family not in shape.tool_families:
                continue
            # Candidate hash match (if the blessing pinned one)?
            if shape.candidate_hash and candidate_hash \
                    and shape.candidate_hash != candidate_hash:
                continue
            # bash-family: exact command match against blessed_commands
            if family == "bash":
                if arg_fingerprint and arg_fingerprint in shape.blessed_commands:
                    return shape
                continue
            # File-scoped: path must be covered
            if not target_path:
                continue
            if _path_covered_by(target_path, shape.approved_paths):
                # If hash pinning applies, only valid when hash matched or
                # none was supplied by caller (already vetted above).
                return shape
        return None

    def clear_op(self, op_id: str) -> int:
        with self._lock:
            shapes = self._by_op.pop(op_id, [])
        return len(shapes)

    def reset(self) -> None:
        """Test helper."""
        with self._lock:
            self._by_op.clear()

    def snapshot(self, op_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                {
                    "source": s.source.value,
                    "tool_families": sorted(s.tool_families),
                    "approved_paths_n": len(s.approved_paths),
                    "blessed_commands_n": len(s.blessed_commands),
                    "candidate_hash": s.candidate_hash[:12],
                    "expires_in_s": max(
                        0.0, s.expires_at_ts - time.monotonic(),
                    ),
                    "note": s.note,
                }
                for s in self._by_op.get(op_id, [])
            ]


_default_ledger: Optional[BlessedShapeLedger] = None
_default_ledger_lock = threading.Lock()


def get_default_ledger() -> BlessedShapeLedger:
    global _default_ledger
    with _default_ledger_lock:
        if _default_ledger is None:
            _default_ledger = BlessedShapeLedger()
        return _default_ledger


# ===========================================================================
# InlinePermissionMiddleware — the composed entry point
# ===========================================================================


@runtime_checkable
class ApprovedScopeResolver(Protocol):
    """Orchestrator-side adapter: op_id → :class:`OpApprovedScope`.

    Implementations read the op's OperationContext and return the
    structured facts the Slice 1 gate needs. Supplied at middleware
    construction; never derived from model output.
    """

    def resolve(self, op_id: str) -> OpApprovedScope: ...


class _DefaultScopeResolver:
    """Safe fallback — empty scope. Gate will treat everything as
    out-of-approved (fail-closed)."""

    def resolve(self, op_id: str) -> OpApprovedScope:
        _ = op_id
        return OpApprovedScope()


class OutcomeSource(str, enum.Enum):
    GATE_SAFE = "gate_safe"
    GATE_BLOCK = "gate_block"
    LEDGER_BLESSED = "ledger_blessed"
    OPERATOR_ALLOW_ONCE = "operator_allow_once"
    OPERATOR_ALLOW_ALWAYS = "operator_allow_always"
    OPERATOR_DENY = "operator_deny"
    OPERATOR_PAUSE = "operator_pause"
    TIMEOUT_DENY = "timeout_deny"
    AUTONOMOUS_COERCE = "autonomous_coerce"
    UPSTREAM_BLOCK = "upstream_block"


@dataclass(frozen=True)
class InlineMiddlewareOutcome:
    """Translated into a :class:`PolicyResult` by the ToolExecutor hook."""

    proceed: bool
    source: OutcomeSource
    rule_id: str = ""
    reason: str = ""
    prompt_id: str = ""
    response: Optional[ResponseKind] = None
    remembered: bool = False
    blessing_source: Optional[BlessingSource] = None


class InlinePermissionMiddleware:
    """Composes :class:`InlinePermissionGate`, :class:`BlessedShapeLedger`,
    and :class:`InlinePromptController` behind a single ``await check()``."""

    def __init__(
        self,
        *,
        gate: Optional[InlinePermissionGate] = None,
        controller: Optional[InlinePromptController] = None,
        ledger: Optional[BlessedShapeLedger] = None,
        renderer: Optional[InlinePromptRenderer] = None,
        scope_resolver: Optional[ApprovedScopeResolver] = None,
        prompt_timeout_s: Optional[float] = None,
    ) -> None:
        self._gate = gate or InlinePermissionGate()
        self._controller = controller or get_default_controller()
        self._ledger = ledger or get_default_ledger()
        self._renderer: InlinePromptRenderer = renderer or _NullRenderer()
        self._scope_resolver = scope_resolver or _DefaultScopeResolver()
        self._prompt_timeout_s = prompt_timeout_s

    # --- entry point -----------------------------------------------------

    async def check(
        self,
        *,
        op_id: str,
        call_id: str,
        tool: str,
        arg_fingerprint: str,
        target_path: str,
        route: RoutePosture,
        upstream_decision: UpstreamPolicy,
        candidate_hash: str = "",
        rationale: str = "",
    ) -> InlineMiddlewareOutcome:
        """Evaluate a proposed tool call end-to-end.

        Returns an :class:`InlineMiddlewareOutcome`. The caller (usually
        :class:`ToolExecutor`) translates ``proceed=False`` into a
        :class:`PolicyResult` with ``decision=DENY``.
        """
        approved_scope = self._scope_resolver.resolve(op_id)
        inp = InlineGateInput(
            tool=tool,
            arg_fingerprint=arg_fingerprint,
            target_path=target_path,
            route=route,
            approved_scope=approved_scope,
            upstream_decision=upstream_decision,
        )
        verdict = self._gate.classify(inp)

        # Upstream BLOCK mirrors.
        if verdict.rule_id == "RULE_UPSTREAM_BLOCKED":
            return InlineMiddlewareOutcome(
                proceed=False,
                source=OutcomeSource.UPSTREAM_BLOCK,
                rule_id=verdict.rule_id,
                reason=verdict.reason,
            )

        # Gate BLOCK (including autonomous_coerce:*) — no prompt.
        if verdict.decision is InlineDecision.BLOCK:
            source = (
                OutcomeSource.AUTONOMOUS_COERCE
                if verdict.rule_id.startswith("autonomous_coerce:")
                else OutcomeSource.GATE_BLOCK
            )
            return InlineMiddlewareOutcome(
                proceed=False, source=source,
                rule_id=verdict.rule_id, reason=verdict.reason,
            )

        # Gate SAFE — proceed immediately.
        if verdict.decision is InlineDecision.SAFE:
            return InlineMiddlewareOutcome(
                proceed=True, source=OutcomeSource.GATE_SAFE,
                rule_id=verdict.rule_id, reason=verdict.reason,
            )

        # Gate ASK — check ledger (double-ask guard).
        blessing = self._ledger.find_blessing(
            op_id=op_id,
            tool=tool,
            target_path=target_path,
            arg_fingerprint=arg_fingerprint,
            candidate_hash=candidate_hash,
        )
        if blessing is not None:
            logger.info(
                "[InlinePrompt] ledger_blessed op=%s tool=%s target=%s source=%s",
                op_id, tool, target_path, blessing.source.value,
            )
            return InlineMiddlewareOutcome(
                proceed=True,
                source=OutcomeSource.LEDGER_BLESSED,
                rule_id=verdict.rule_id,
                reason="blessed by " + blessing.source.value,
                blessing_source=blessing.source,
            )

        # No blessing → prompt required. Autonomous routes should have
        # been coerced by the gate; we defend anyway.
        if route is RoutePosture.AUTONOMOUS:
            return InlineMiddlewareOutcome(
                proceed=False,
                source=OutcomeSource.AUTONOMOUS_COERCE,
                rule_id=verdict.rule_id,
                reason="autonomous route cannot render inline prompt",
            )

        # Build and register the request.
        prompt_id = f"{op_id}:{call_id}:{uuid.uuid4().hex[:8]}"
        arg_preview = _truncate(arg_fingerprint or target_path, 200)
        request = InlinePromptRequest(
            prompt_id=prompt_id,
            op_id=op_id,
            call_id=call_id,
            tool=tool,
            arg_fingerprint=arg_fingerprint,
            arg_preview=arg_preview,
            target_path=target_path,
            verdict=verdict,
            rationale=_truncate(rationale or "", 500),
            route=route,
            upstream_decision=upstream_decision,
        )
        future = self._controller.request(
            request, timeout_s=self._prompt_timeout_s,
        )
        try:
            self._renderer.render(request)
        except Exception as exc:  # noqa: BLE001
            # Broken renderer must NEVER escalate privilege. Fail closed:
            # deny immediately and resolve the Future so the task cleans up.
            logger.warning(
                "[InlinePrompt] renderer.render raised; denying (§7): %s", exc,
            )
            try:
                self._controller.deny(
                    prompt_id,
                    reviewer="middleware",
                    reason="renderer_failure:" + type(exc).__name__,
                )
            except InlinePromptStateError:
                pass
            outcome = await future
            return InlineMiddlewareOutcome(
                proceed=False,
                source=OutcomeSource.OPERATOR_DENY,
                rule_id=verdict.rule_id,
                reason="renderer_failure",
                prompt_id=prompt_id,
                response=outcome.response,
            )

        try:
            outcome = await future
        finally:
            try:
                # dismiss always runs; renderer exceptions here are swallowed.
                # We have to fetch a projection for the outcome snapshot; if
                # the future was cancelled we synthesize a "dismissed" outcome.
                last_outcome = _maybe_outcome(future, prompt_id)
                self._renderer.dismiss(prompt_id, last_outcome)
            except Exception as exc:  # noqa: BLE001
                logger.debug("[InlinePrompt] renderer.dismiss raised: %s", exc)

        return _translate(outcome, verdict)


def _truncate(s: str, n: int) -> str:
    if not s:
        return ""
    if len(s) <= n:
        return s
    return s[: max(1, n - 3)] + "..."


def _maybe_outcome(
    future: "asyncio.Future[InlinePromptOutcome]",
    prompt_id: str,
) -> InlinePromptOutcome:
    if future.done() and not future.cancelled():
        try:
            return future.result()
        except Exception:  # noqa: BLE001
            pass
    return InlinePromptOutcome(
        prompt_id=prompt_id,
        state=STATE_EXPIRED,
        response=None,
        operator_reason="dismissed_without_resolution",
        reviewer="middleware",
    )


def _translate(
    outcome: InlinePromptOutcome,
    verdict: InlineGateVerdict,
) -> InlineMiddlewareOutcome:
    # Allowed paths — distinguish once vs always for Slice 3 learning.
    if outcome.state == STATE_ALLOWED:
        if outcome.response is ResponseKind.ALLOW_ALWAYS:
            return InlineMiddlewareOutcome(
                proceed=True,
                source=OutcomeSource.OPERATOR_ALLOW_ALWAYS,
                rule_id=verdict.rule_id,
                reason=outcome.operator_reason or "operator allow-always",
                prompt_id=outcome.prompt_id,
                response=outcome.response,
                remembered=True,
            )
        return InlineMiddlewareOutcome(
            proceed=True,
            source=OutcomeSource.OPERATOR_ALLOW_ONCE,
            rule_id=verdict.rule_id,
            reason=outcome.operator_reason or "operator allow-once",
            prompt_id=outcome.prompt_id,
            response=outcome.response,
        )
    # Denied / paused / expired → proceed=False with distinct source tags.
    if outcome.state == STATE_EXPIRED:
        return InlineMiddlewareOutcome(
            proceed=False,
            source=OutcomeSource.TIMEOUT_DENY,
            rule_id=verdict.rule_id,
            reason=outcome.operator_reason or "prompt timed out",
            prompt_id=outcome.prompt_id,
        )
    if outcome.state == STATE_PAUSED:
        return InlineMiddlewareOutcome(
            proceed=False,
            source=OutcomeSource.OPERATOR_PAUSE,
            rule_id=verdict.rule_id,
            reason=outcome.operator_reason or "operator paused op",
            prompt_id=outcome.prompt_id,
            response=outcome.response,
        )
    return InlineMiddlewareOutcome(
        proceed=False,
        source=OutcomeSource.OPERATOR_DENY,
        rule_id=verdict.rule_id,
        reason=outcome.operator_reason or "operator denied",
        prompt_id=outcome.prompt_id,
        response=outcome.response,
    )


# ---------------------------------------------------------------------------
# ProviderRoute → RoutePosture helper
# ---------------------------------------------------------------------------


_AUTONOMOUS_ROUTES = frozenset({"background", "speculative"})


def posture_for_route(route_value: str) -> RoutePosture:
    """Map a :class:`ProviderRoute` string value to a :class:`RoutePosture`.

    Kept as a module-level function to avoid importing
    :class:`ProviderRoute` (circular-import risk in a test-light module).
    """
    return (
        RoutePosture.AUTONOMOUS
        if (route_value or "").lower() in _AUTONOMOUS_ROUTES
        else RoutePosture.INTERACTIVE
    )


# ---------------------------------------------------------------------------
# Module singleton for the middleware
# ---------------------------------------------------------------------------


_default_middleware: Optional[InlinePermissionMiddleware] = None
_default_middleware_lock = threading.Lock()


def get_default_middleware() -> InlinePermissionMiddleware:
    global _default_middleware
    with _default_middleware_lock:
        if _default_middleware is None:
            _default_middleware = InlinePermissionMiddleware()
        return _default_middleware


def reset_default_singletons() -> None:
    """Test helper — clears all module-level singletons."""
    global _default_controller, _default_ledger, _default_middleware
    with _default_controller_lock:
        if _default_controller is not None:
            _default_controller.reset()
        _default_controller = None
    with _default_ledger_lock:
        if _default_ledger is not None:
            _default_ledger.reset()
        _default_ledger = None
    with _default_middleware_lock:
        _default_middleware = None
