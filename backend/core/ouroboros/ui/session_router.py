"""Where a frame lands while the operator is in a /session.

FOREGROUND_SESSION §4: one routing decision, by DATA, never by text --
except the ambient severity, which is the deck's own classifier. Outside a
session every frame goes where it always has. Inside one:

    addressed to this cockpit (the conversation, its gate prompts) -> transcript
    a block whose op the session owns                               -> transcript
    ambient FATAL                                                   -> transcript (breaks through)
    ambient WARN                                                    -> sidelined, counted as a warning
    everything else (ambient INFO/SOCIAL, other ops' lines/blocks)  -> sidelined

Sidelined frames are buffered (bounded) and summarized in ONE live row;
``/bg`` reads the buffer and leaving the session replays a collapsed digest
once. The buffer holds MARKUP: markup frames and block lines as composed,
raw ``line`` frames escaped on the way in (they are inert data), so a
reader renders every entry one way. Invariant 3: each frame is routed exactly once, and the counters
account for every sidelined frame even after the buffer wraps.

The cockpit's sinks (markup, raw line, frozen block, live blocks) all ask
this object; it holds no rendering.
"""
from __future__ import annotations

import os
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional, Set, Tuple

TRANSCRIPT = "transcript"
SIDELINE = "sideline"


def _buffer_cap() -> int:
    try:
        return max(50, int(os.environ.get("JARVIS_SESSION_SIDELINE_LINES", "") or 400))
    except ValueError:
        return 400


def _mark() -> str:
    """The background row's glyph, from the theme's registry (the UI plane
    admits no raw emoji literals; ASCII degradation comes with it)."""
    try:
        from backend.core.ouroboros.ui.theme import mark
        return mark("cycle")
    except Exception:  # noqa: BLE001
        return "~"


def _severity(text: str) -> str:
    """The deck's own classifier; an unclassifiable line is INFO."""
    try:
        from backend.core.ouroboros.battle_test.ambient_deck import classify
        sev = classify(text)
        return str(getattr(sev, "name", sev)).upper()
    except Exception:  # noqa: BLE001
        return "INFO"


@dataclass
class SessionRouter:
    session_id: str = ""
    owned_ops: Set[str] = field(default_factory=set)
    buffer: Deque[str] = field(default_factory=lambda: deque(maxlen=_buffer_cap()))
    sidelined: int = 0
    warnings: int = 0
    last_warning: str = ""
    finished: Set[str] = field(default_factory=set)
    classify: Callable[[str], str] = _severity
    _last_phase: Dict[str, str] = field(default_factory=dict)

    # ---- state
    @property
    def active(self) -> bool:
        return bool(self.session_id)

    def apply_mode(self, frame: Dict[str, Any]) -> Optional[List[str]]:
        """A ``session_mode`` frame from the daemon. Returns the one-time
        replay when the session just ended, else None."""
        sid = str(frame.get("session") or "")
        ops = {str(o) for o in frame.get("owned_ops") or [] if o}
        if sid:
            if sid != self.session_id:
                self._reset()
            self.session_id = sid
            self.owned_ops = ops
            return None
        if not self.session_id:
            return None
        replay = self.replay()
        # The buffer outlives the session, so /bg still answers right after
        # leaving; it resets when the next session starts.
        self.session_id = ""
        self.owned_ops = set()
        return replay

    def _reset(self) -> None:
        self.buffer.clear()
        self.sidelined = self.warnings = 0
        self.last_warning = ""
        self.finished = set()

    # ---- routing (each call routes one frame exactly once)
    def route_markup(self, text: str, addressed: bool) -> str:
        if not self.active or addressed:
            return TRANSCRIPT
        sev = self.classify(text)
        if sev == "FATAL":
            return TRANSCRIPT
        if sev in ("WARN", "WARNING"):
            self.warnings += 1
            self.last_warning = text.strip()[:120]
        self._sideline(text)
        return SIDELINE

    def route_line(self, text: str) -> str:
        """A raw ``line`` frame: never addressed, and inert data -- escaped
        before it joins the markup buffer."""
        try:
            from rich.markup import escape
            text = escape(str(text))
        except Exception:  # noqa: BLE001
            pass
        return self.route_markup(text, addressed=False)

    def route_block(self, op_id: str, lines: Iterable[str], *, finished: bool = False) -> str:
        """A FROZEN block. The session's own ops reach the transcript."""
        if not self.active or (op_id and op_id in self.owned_ops):
            return TRANSCRIPT
        if finished and op_id:
            self.finished.add(op_id)
        for ln in lines:
            self._sideline(ln)
        return SIDELINE

    def split_live(self, blocks: List[Any]) -> Tuple[List[Any], List[Any]]:
        """``(shown, background)`` for the live region. Outside a session
        everything is shown.

        Also where "finished" is learned, from DATA: a background op whose
        pipeline block leaves the live area has ended. It is NOT called
        "landed": the stages end at VERIFY, and an op that leaves from VERIFY
        may still have rolled back; the outcome is in /bg, not guessed here."""
        live_ops = {getattr(b, "op_id", "") for b in blocks}
        for op in list(self._last_phase):
            if op not in live_ops:
                if self.active and op not in self.owned_ops:
                    self.finished.add(op)
                self._last_phase.pop(op, None)
        for b in blocks:
            op = getattr(b, "op_id", "")
            phase = (getattr(b, "meta", None) or {}).get("phase")
            if op and phase:
                self._last_phase[op] = str(phase)
        if not self.active:
            return list(blocks), []
        shown = [b for b in blocks if getattr(b, "op_id", "") in self.owned_ops]
        return shown, [b for b in blocks if b not in shown]

    def _sideline(self, text: str) -> None:
        self.sidelined += 1
        self.buffer.append(text)

    # ---- surfaces
    def summary_row(self, background_live: int) -> str:
        """``↻ 3 ops running · 1 finished · 1 warning · /bg`` -- or "" when
        there is nothing in the background."""
        if not self.active:
            return ""
        parts = []
        if background_live:
            parts.append(f"{background_live} op{'s' if background_live != 1 else ''} running")
        if self.finished:
            parts.append(f"{len(self.finished)} finished")
        if self.warnings:
            parts.append(f"{self.warnings} warning{'s' if self.warnings != 1 else ''}")
        if not parts and not self.sidelined:
            return ""
        parts.append("/bg")
        return f"{_mark()} " + " · ".join(parts)

    def background(self, limit: int = 60) -> List[str]:
        """``/bg``: the newest sidelined lines, oldest first."""
        items = list(self.buffer)[-limit:]
        dropped = self.sidelined - len(self.buffer)
        head = [f"… {dropped} earlier line(s) not kept"] if dropped > 0 else []
        return head + items

    def replay(self) -> List[str]:
        """Shown once on leaving: a digest, not the flood."""
        if not self.sidelined and not self.finished:
            return []
        out = [f"{_mark()} while you were in session {self.session_id}: "
               f"{self.sidelined} background line(s)"
               + (f" · {len(self.finished)} op(s) finished" if self.finished else "")
               + (f" · {self.warnings} warning(s)" if self.warnings else "")]
        if self.last_warning:
            out.append(f"  last warning: {self.last_warning}")
        out.append("  /bg shows the detail")
        return out


__all__ = ["SIDELINE", "TRANSCRIPT", "SessionRouter"]
