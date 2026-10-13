"""Foreground session -- a conversation with the organism, governed.

docs/architecture/FOREGROUND_SESSION.md is the design. This module is its
only new state: one :class:`ForegroundSession` per conversation, holding its
turns, its focus, the ops it started and its budget. Everything else is
reused:

* the agent loop is :class:`tool_executor.ToolLoopCoordinator`, called with
  ``is_read_only=True`` so rule 0d refuses every mutation tool -- a session
  READS, searches and answers; it never writes (invariant 1);
* generation is :func:`rt_gate.gate_completion`, the local-first gate the
  cockpit's ``/ask`` already falls back to -- no second provider path;
* the tool protocol and its teaching text are ``providers``' own
  (``_build_tool_section`` / ``_parse_tool_call_response``);
* the context budget is the SERVED window (``prompt_window_for`` over the
  local lane's config), never a constant (invariant 6);
* output is delivered addressed to the asking cockpit through
  ``side_channel.emit_markup`` and drawn with ``ui.deck_grammar``.

A proposed change is captured as the turn's ``proposal`` and shown as a diff;
turning it into a governed op is :mod:`foreground_session_ops` (the session
never applies anything itself).

Each coordinator is built per turn: the shared GLS coordinator keeps
``_last_records`` on the instance and is "not safe for concurrent reuse".
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.ForegroundSession")

# --------------------------------------------------------------------- knobs


def _envi(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, "").strip() or default))
    except ValueError:
        return default


def _envf(name: str, default: float) -> float:
    try:
        return max(1.0, float(os.environ.get(name, "").strip() or default))
    except ValueError:
        return default


def max_rounds() -> int:
    return _envi("JARVIS_SESSION_MAX_ROUNDS", 8)


def turn_timeout_s() -> float:
    return _envf("JARVIS_SESSION_TURN_TIMEOUT_S", 600.0)


def round_timeout_s() -> float:
    return _envf("JARVIS_SESSION_ROUND_TIMEOUT_S", 180.0)


def answer_max_tokens() -> int:
    return _envi("JARVIS_SESSION_MAX_TOKENS", 4096)


def verbatim_turns() -> int:
    return _envi("JARVIS_SESSION_VERBATIM_TURNS", 4)


def max_sessions() -> int:
    return _envi("JARVIS_SESSION_MAX_SESSIONS", 16)


def auto_apply() -> bool:
    """FOREGROUND_SESSION §7 default: a change always waits for Enter."""
    return os.environ.get("JARVIS_SESSION_AUTO_APPLY", "").strip().lower() in ("1", "true", "yes", "on")


#: Fallback only when the served window cannot be read at all.
_UNKNOWN_WINDOW_TOKENS = 8192

SESSION_SYSTEM_PROMPT = (
    "You are Ouroboros, the self-developing organism inside the JARVIS repository, "
    "in a live conversation with your operator. You can read the repository with "
    "the tools below; you cannot change it from this conversation.\n\n"
    "Answer in plain prose. Ground every claim about the code in what a tool showed "
    "you this turn or an earlier one; say so when you have not looked.\n\n"
    "When the operator asks for a CHANGE, do not call edit, write or bash tools "
    "(they are refused here). Reply with ONLY a JSON change proposal:\n"
    '{"schema_version": "2b.1-diff", "candidates": [{"candidate_id": "c1", '
    '"file_path": "<repo-relative path>", "unified_diff": "<unified diff>", '
    '"rationale": "<one sentence>"}]}\n'
    "The operator sees the diff and decides; if they accept, it goes through the "
    "full governed pipeline (validate, gate, apply, verify) like any other change.\n"
)


# --------------------------------------------------------------------- state


@dataclass
class ToolDigest:
    """What one tool call did -- kept instead of its raw output (which lives
    in the coordinator's records and the op context cache)."""
    tool: str
    target: str
    status: str
    preview: str = ""


@dataclass
class SessionTurn:
    user: str
    answer: str = ""
    tools: List[ToolDigest] = field(default_factory=list)
    proposal: Optional[Dict[str, Any]] = None
    error: str = ""
    started: float = 0.0
    ended: float = 0.0
    #: The coordinator's ToolExecutionRecords: the session's REAL reads. An
    #: accepted proposal carries them to GENERATE, so the Iron Gate judges
    #: the exploration that actually produced it. Not logged.
    records: List[Any] = field(default_factory=list, repr=False)

    def log_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("records", None)
        return d


@dataclass
class ForegroundSession:
    id: str
    cockpit: str
    created: float
    turns: List[SessionTurn] = field(default_factory=list)
    focus: List[str] = field(default_factory=list)
    owned_ops: List[str] = field(default_factory=list)
    pending: Deque[str] = field(default_factory=deque)
    busy: bool = False
    ended: bool = False

    def last_proposal(self) -> Optional[Dict[str, Any]]:
        for t in reversed(self.turns):
            if t.proposal is not None:
                return t.proposal
        return None


# --------------------------------------------------------------------- pure helpers


_PATH_RE = re.compile(r"[\w./-]+\.(?:py|md|yaml|yml|json|toml|txt|sh|ps1)\b")


def note_focus(session: ForegroundSession, *texts: str, cap: int = 12) -> None:
    """Files the conversation is about, most recent first."""
    for text in texts:
        for path in _PATH_RE.findall(text or ""):
            if path in session.focus:
                session.focus.remove(path)
            session.focus.insert(0, path)
    del session.focus[cap:]


def parse_proposal(text: str) -> Optional[Dict[str, Any]]:
    """A ``2b.1-diff`` (or whole-file ``2b.1``) envelope with at least one
    candidate, or None for a prose answer."""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```\w*\n?|\n?```$", "", raw).strip()
    if not raw.startswith("{"):
        return None
    try:
        obj = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(obj, dict) or not str(obj.get("schema_version", "")).startswith("2b.1"):
        return None
    cands = [c for c in obj.get("candidates") or [] if isinstance(c, dict) and c.get("file_path")]
    return obj if cands else None


def summarize_turn(turn: SessionTurn) -> str:
    """Deterministic one-paragraph summary of an older turn: the question,
    what was looked at, and the answer's first paragraph. No model call."""
    looked = ", ".join(f"{d.tool}({d.target})" for d in turn.tools[:6])
    first = (turn.answer or turn.error or "").strip().split("\n\n", 1)[0][:400]
    if turn.proposal is not None:
        files = ", ".join(c.get("file_path", "") for c in turn.proposal.get("candidates", []))
        first = f"[proposed a change to {files}]"
    out = f"- Operator: {turn.user.strip()[:300]}"
    if looked:
        out += f"\n  Looked at: {looked}"
    return out + f"\n  You: {first}"


def build_prompt(session: ForegroundSession, message: str) -> str:
    """Older turns summarized, the last N verbatim, then the new message.
    The caller fits it to the served window."""
    n = verbatim_turns()
    old, recent = session.turns[:-n] if len(session.turns) > n else [], session.turns[-n:]
    parts: List[str] = []
    if old:
        parts.append("## Earlier in this conversation\n" + "\n".join(summarize_turn(t) for t in old))
    for t in recent:
        tools = "".join(f"\n  [{d.tool}({d.target}) -> {d.status}]" for d in t.tools)
        parts.append(f"## Operator\n{t.user}{tools}\n## You\n{t.answer or t.error or '(no answer)'}")
    if session.focus:
        parts.append("## Files in focus\n" + "\n".join(f"- {p}" for p in session.focus))
    parts.append(f"## Operator\n{message}")
    return "\n\n".join(parts)


def served_window_tokens() -> int:
    """The local lane's served input window (``prompt_window_for`` over its
    config). Read per turn so a model swap is honoured."""
    try:
        from types import SimpleNamespace  # noqa: PLC0415
        from backend.core.ouroboros.governance.local_inference_director import (  # noqa: PLC0415
            LocalConfig, prompt_window_for,
        )
        win = prompt_window_for(SimpleNamespace(_cfg=LocalConfig.from_env()))
        if win is not None and int(win.input_tokens) > 0:
            return int(win.input_tokens)
    except Exception:  # noqa: BLE001
        logger.debug("[Session] served window unreadable", exc_info=True)
    return _UNKNOWN_WINDOW_TOKENS


# --------------------------------------------------------------------- rendering


def _g():
    from backend.core.ouroboros.ui import deck_grammar  # noqa: PLC0415
    return deck_grammar


def render_tool(d: ToolDigest) -> str:
    g = _g()
    ok = d.status in ("success", "ok", "")
    head = g.action(_verb(d.tool), d.target, tone=None if ok else g.FAILED_TONE)
    return head if ok or not d.preview else head + "\n" + g.detail(d.preview[:200], tone="crit")


def _verb(tool: str) -> str:
    return {"read_file": "Read", "search_code": "Search", "list_symbols": "Symbols",
            "get_callers": "Callers", "glob_files": "Glob", "list_dir": "List",
            "git_log": "GitLog", "git_diff": "GitDiff", "git_blame": "Blame",
            "web_fetch": "Fetch", "web_search": "WebSearch"}.get(tool, tool)


def render_answer(text: str) -> str:
    g = _g()
    body = (text or "").strip() or "(no answer)"
    return g.bullet() + " " + g._escape(body)


def render_proposal(proposal: Dict[str, Any]) -> str:
    g = _g()
    lines: List[str] = []
    for c in proposal.get("candidates", []):
        path = str(c.get("file_path", ""))
        lines.append(g.action("Propose", path, tone=g.PENDING_TONE))
        if c.get("rationale"):
            lines.append(g.detail(str(c["rationale"])))
        for raw in str(c.get("unified_diff") or c.get("full_content") or "").splitlines()[:80]:
            if raw.startswith(("@@", "---", "+++")):
                lines.append(g.detail(raw, tone="faint"))
            elif raw[:1] in ("+", "-", " "):
                lines.append(g.diff(None, raw[0], raw[1:], path=path))
            else:
                lines.append(g.diff(None, " ", raw, path=path))
    lines.append(g.detail("/session apply to send it through the governed pipeline, "
                          "or keep talking to revise it", tone="info"))
    return "\n".join(lines)


# --------------------------------------------------------------------- manager


Emit = Callable[[str, str], Any]
GenerateFn = Callable[[str], Awaitable[str]]


async def _default_generate(prompt: str) -> str:
    from backend.core.ouroboros.governance.rt_gate import gate_completion  # noqa: PLC0415
    return await gate_completion(
        prompt, caller_id="foreground_session", system_prompt=SESSION_SYSTEM_PROMPT,
        max_tokens=answer_max_tokens(), timeout_s=round_timeout_s(),
    )


def _default_emit(markup: str, cockpit: str) -> Any:
    from backend.core.ouroboros.governance.side_channel import emit_markup  # noqa: PLC0415
    return emit_markup(markup, cockpit)


def _default_coordinator(on_tool_call: Callable[..., None]) -> Any:
    from backend.core.ouroboros.governance.tool_executor import (  # noqa: PLC0415
        AsyncProcessToolBackend, GoverningToolPolicy, ToolLoopCoordinator,
    )
    root = Path(os.environ.get("JARVIS_REPO_PATH", "") or os.getcwd()).resolve()
    return ToolLoopCoordinator(
        backend=AsyncProcessToolBackend(semaphore=asyncio.Semaphore(2)),
        policy=GoverningToolPolicy(repo_roots={"jarvis": root}),
        max_rounds=max_rounds(), tool_timeout_s=60.0, on_tool_call=on_tool_call,
    )


class SessionManager:
    """Every live session, keyed by id, and which one each cockpit is in.

    ``generate`` / ``emit`` / ``coordinator_factory`` are seams for tests;
    production uses the gate, the side channel and a fresh coordinator."""

    def __init__(self, *, generate: Optional[GenerateFn] = None,
                 emit: Optional[Emit] = None,
                 coordinator_factory: Optional[Callable[..., Any]] = None,
                 log_dir: Optional[Path] = None) -> None:
        self._sessions: Dict[str, ForegroundSession] = {}
        self._attached: Dict[str, str] = {}          # cockpit -> session id
        self._tasks: Dict[str, asyncio.Task] = {}
        self._generate = generate or _default_generate
        self._emit = emit or _default_emit
        self._coordinator = coordinator_factory or _default_coordinator
        self._log_dir = log_dir

    # ---- lifecycle
    def active(self, cockpit: str) -> Optional[ForegroundSession]:
        sid = self._attached.get(cockpit or "")
        s = self._sessions.get(sid or "")
        return s if s is not None and not s.ended else None

    def start(self, cockpit: str) -> ForegroundSession:
        live = self.active(cockpit)
        if live is not None:
            return live
        if len(self._sessions) >= max_sessions():
            oldest = min((s for s in self._sessions.values() if not s.busy),
                         key=lambda s: s.created, default=None)
            if oldest is not None:
                self._sessions.pop(oldest.id, None)
        s = ForegroundSession(id=f"s-{uuid.uuid4().hex[:6]}", cockpit=cockpit or "", created=time.time())
        self._sessions[s.id] = s
        self._attached[cockpit or ""] = s.id
        return s

    def resume(self, cockpit: str, sid: str) -> Optional[ForegroundSession]:
        s = self._sessions.get(sid)
        if s is None or s.ended:
            return None
        s.cockpit = cockpit or ""
        self._attached[cockpit or ""] = s.id
        return s

    def end(self, cockpit: str) -> Optional[ForegroundSession]:
        s = self.active(cockpit)
        if s is None:
            return None
        s.ended = True
        self._attached.pop(cockpit or "", None)
        task = self._tasks.pop(s.id, None)
        if task is not None and not task.done():
            task.cancel()
        return s

    def list(self) -> List[ForegroundSession]:
        return sorted(self._sessions.values(), key=lambda s: s.created)

    def cancel_turn(self, cockpit: str) -> bool:
        s = self.active(cockpit)
        task = self._tasks.get(s.id) if s else None
        if task is None or task.done():
            return False
        s.pending.clear()
        task.cancel()
        return True

    # ---- turns
    def submit(self, cockpit: str, text: str) -> str:
        """Queue one operator message. Returns at once (the REPL consumer
        must never await a provider); the turn runs as its own task."""
        s = self.active(cockpit)
        if s is None:
            return "no session"
        s.pending.append(text)
        if s.busy:
            return f"queued ({len(s.pending)} waiting)"
        s.busy = True
        self._tasks[s.id] = asyncio.get_running_loop().create_task(self._drain(s))
        return "thinking"

    async def _drain(self, s: ForegroundSession) -> None:
        try:
            while s.pending and not s.ended:
                await self.run_turn(s, s.pending.popleft())
        finally:
            s.busy = False

    async def run_turn(self, s: ForegroundSession, message: str) -> SessionTurn:
        turn = SessionTurn(user=message, started=time.time())
        note_focus(s, message)

        def on_tool_call(*, op_id: str, tool_name: str, args_summary: str = "",
                         round_index: int = 0, result_preview: str = "",
                         duration_ms: float = 0.0, status: str = "",
                         preamble: str = "", **_: Any) -> None:
            if not status:
                return                                   # the start event; draw on completion
            d = ToolDigest(tool=tool_name, target=args_summary, status=status,
                           preview="" if status == "success" else result_preview)
            turn.tools.append(d)
            note_focus(s, args_summary)
            self._emit(render_tool(d), s.cockpit)

        prompt = build_prompt(s, message)
        try:
            from backend.core.ouroboros.governance.local_inference_director import (  # noqa: PLC0415
                fit_prompt_to_window,
            )
            _sys, prompt, _ = fit_prompt_to_window(SESSION_SYSTEM_PROMPT, prompt,
                                                   max_tokens=served_window_tokens())
        except Exception:  # noqa: BLE001 — an unfittable prompt is sent as built
            logger.debug("[Session] fit skipped", exc_info=True)
        try:
            from backend.core.ouroboros.governance.providers import (  # noqa: PLC0415
                _build_tool_section, _parse_tool_call_response,
            )
            coordinator = self._coordinator(on_tool_call)
            final, records = await coordinator.run(
                prompt=prompt + "\n\n" + _build_tool_section(),
                generate_fn=self._generate,
                parse_fn=_parse_tool_call_response,
                repo="jarvis",
                op_id=f"session-{s.id}-t{len(s.turns) + 1}",
                deadline=time.monotonic() + turn_timeout_s(),
                is_read_only=True,
            )
            turn.records = list(records or [])
            turn.proposal = parse_proposal(final)
            turn.answer = "" if turn.proposal is not None else str(final or "")
        except asyncio.CancelledError:
            turn.error = "cancelled"
            s.turns.append(turn)
            self._emit(_g().detail("turn cancelled", tone="warn"), s.cockpit)
            raise
        except Exception as exc:  # noqa: BLE001 — a failed turn is an answer, not a crash
            turn.error = f"{type(exc).__name__}: {exc}"[:300]
            logger.warning("[Session] %s turn failed: %s", s.id, turn.error)
        turn.ended = time.time()
        s.turns.append(turn)
        if turn.proposal is not None:
            self._emit(render_proposal(turn.proposal), s.cockpit)
        elif turn.error:
            self._emit(_g().detail(f"the turn failed: {turn.error}", tone="crit"), s.cockpit)
        else:
            self._emit(render_answer(turn.answer), s.cockpit)
        self._log(s, turn)
        return turn

    def _log(self, s: ForegroundSession, turn: SessionTurn) -> None:
        """Append-only record under .jarvis/sessions/<id>.jsonl."""
        try:
            d = self._log_dir or Path(".jarvis/sessions")
            d.mkdir(parents=True, exist_ok=True)
            with open(d / f"{s.id}.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(turn.log_dict(), default=str) + "\n")
        except Exception:  # noqa: BLE001
            logger.debug("[Session] log write failed", exc_info=True)


_MANAGER: Optional[SessionManager] = None


def get_manager() -> SessionManager:
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = SessionManager()
    return _MANAGER


def reset_for_tests(manager: Optional[SessionManager] = None) -> None:
    global _MANAGER
    _MANAGER = manager


__all__ = [
    "ForegroundSession", "SessionManager", "SessionTurn", "ToolDigest",
    "build_prompt", "get_manager", "note_focus", "parse_proposal",
    "render_proposal", "reset_for_tests", "served_window_tokens", "summarize_turn",
]
