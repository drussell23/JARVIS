"""The turn spinner — one live line, bound to the question YOU asked.

    ❯ can you tell me what is O+V?
    ⏺ · Nesting… (10s · ↓ 63 tokens · DW-397B)      ← updates IN PLACE
    ⏺ O+V is Ouroboros + Venom …                    ← resolves into the reply

Every ingredient for this already existed and none of them were bound to a
TURN. The pulse (`attach_heartbeat`) rendered in the bottom toolbar — a
region that describes the ORGANISM, not your question; the `⏺` opener
(`chat_response_style`) was a static line the daemon printed once; and the
canvas is an append-only ring, so anything written there can only stack.
The result: you pressed Enter and the transcript went silent while
something pulsed in a corner. This module is the missing binding, not new
machinery.

Two properties make it a TURN spinner rather than a second toolbar:

* **Turn-scoped enrichment.** The heartbeat is global — if the organism
  was already synthesising an autonomous op when you hit Enter, printing
  its token count under your question would be a fabrication. The frame
  is adopted only when its own ``elapsed_s`` fits INSIDE this turn's age
  (plus slack): work that predates your question describes itself, never
  you. Otherwise the row falls back to an honest local clock.
* **In place, never stacked.** The row is a live region between the
  canvas and the prompt — Style Guide §07's "one in-place spinner, never
  six stacked log lines". It occupies zero rows while idle, so a resting
  cockpit is exactly as tall as it was.

Resolution is total. A turn closes on the first addressed reply, on an
interrupt, on daemon death, or on a hard ceiling — never on nothing. A
spinner that can outlive its answer is worse than no spinner, because the
operator reads it as "still working" forever.

Env:
  * ``JARVIS_TURN_SPINNER_ENABLED``     master (default true)
  * ``JARVIS_TURN_MAX_S``               ceiling before honest give-up (600)
  * ``JARVIS_TURN_TOMBSTONE_MIN_S``     duration worth recording (3.0)
  * ``JARVIS_TURN_VERBS``               extra flavour verbs, comma-separated

NEVER raises into a repaint, a key handler, or a frame callback.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable, List, Optional, Tuple

logger = logging.getLogger(__name__)

TURN_SPINNER_SCHEMA_VERSION: str = "turn_spinner.1"

MASTER_FLAG_ENV_VAR: str = "JARVIS_TURN_SPINNER_ENABLED"
MAX_TURN_ENV_VAR: str = "JARVIS_TURN_MAX_S"
TOMBSTONE_MIN_ENV_VAR: str = "JARVIS_TURN_TOMBSTONE_MIN_S"
VERBS_ENV_VAR: str = "JARVIS_TURN_VERBS"

#: Flavour verbs for the local (pre-heartbeat) phase — the organism's own
#: vocabulary, in the register of a colleague thinking out loud. DATA, like
#: the emoji table and the social acks: extending the voice is an edit here,
#: never a change to a renderer. Selection is a stable hash of the turn text,
#: so the same question always animates the same way (auditable, and it
#: stops the row flickering between words on consecutive repaints).
TURN_VERBS: Tuple[str, ...] = (
    "Thinking", "Considering", "Coiling", "Tracing", "Weighing",
    "Sensing", "Digesting", "Circling", "Unwinding", "Reckoning",
)

#: How far a heartbeat's own elapsed may exceed this turn's age before it
#: is judged to describe OTHER work. Generous enough to absorb IPC latency
#: and the ~1 Hz frame cadence; far tighter than any real autonomous op.
_ADOPTION_SLACK_S: float = 3.0


def is_turn_spinner_enabled() -> bool:
    """Master flag — default true. NEVER raises."""
    raw = os.environ.get(MASTER_FLAG_ENV_VAR, "true")
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _max_turn_s() -> float:
    try:
        return max(5.0, float(os.environ.get(MAX_TURN_ENV_VAR, "600")))
    except (TypeError, ValueError):
        return 600.0


def _tombstone_min_s() -> float:
    try:
        return max(0.0, float(os.environ.get(TOMBSTONE_MIN_ENV_VAR, "3")))
    except (TypeError, ValueError):
        return 3.0


def turn_verbs() -> Tuple[str, ...]:
    """Builtin vocabulary + operator extras. NEVER raises."""
    verbs = list(TURN_VERBS)
    try:
        extra = os.environ.get(VERBS_ENV_VAR, "").strip()
        for word in extra.split(","):
            word = word.strip()
            if word:
                verbs.append(word)
    except Exception:  # noqa: BLE001
        pass
    return tuple(verbs)


def _verb_for(text: str) -> str:
    """Stable per-question pick. NEVER raises."""
    try:
        import hashlib
        verbs = turn_verbs()
        digest = hashlib.sha256(str(text or "").encode("utf-8")).digest()
        return verbs[digest[0] % len(verbs)]
    except Exception:  # noqa: BLE001
        return "Thinking"


def _fmt_elapsed(seconds: float) -> str:
    """Reuses the heartbeat's own duration grammar so one turn cannot read
    `9s` in the row and `0m 9s` in the toolbar."""
    try:
        from backend.core.ouroboros.battle_test.attach_heartbeat import (
            _fmt_elapsed as _hb_fmt,
        )
        return _hb_fmt(seconds)
    except Exception:  # noqa: BLE001
        return f"{int(max(0.0, seconds))}s"


def _pulse(now: float) -> str:
    """O+V's identity spinner, from its ONE canonical definition — the same
    frame every surface shows at the same instant."""
    try:
        from backend.core.ouroboros.battle_test.attach_heartbeat import (
            _pulse_glyph,
        )
        return _pulse_glyph(now)
    except Exception:  # noqa: BLE001
        return "·"


class TurnSpinner:
    """One operator turn's live status. Single-slot by design: Claude Code
    shows one spinner, and so does a cockpit — a second concurrent question
    extends the open turn rather than opening a rival row.

    Injectables (``now_fn``, ``heartbeat_fn``) keep the whole state machine
    testable without a terminal or a daemon."""

    def __init__(
        self,
        *,
        heartbeat_fn: Optional[Callable[[], Any]] = None,
        now_fn: Optional[Callable[[], float]] = None,
        emit_fn: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._heartbeat_fn = heartbeat_fn or (lambda: None)
        self._now = now_fn or time.monotonic
        self._emit = emit_fn or (lambda _t: None)
        self._active = False
        self._started_at = 0.0
        self._text = ""
        self._verb = "Thinking"
        self._pending = 0
        self._last_reason = ""

    # -- state -------------------------------------------------------------

    @property
    def active(self) -> bool:
        return self._active

    @property
    def pending(self) -> int:
        """Submissions still unanswered — what makes a rapid second question
        extend the row instead of silently replacing the first."""
        return self._pending

    def age(self) -> float:
        return max(0.0, self._now() - self._started_at) if self._active else 0.0

    # -- lifecycle ---------------------------------------------------------

    def open(self, text: str = "") -> bool:
        """A line was submitted and an answer is expected. Returns True when
        the row is (now) live. NEVER raises."""
        try:
            if not is_turn_spinner_enabled():
                return False
            if self._active:
                # A second question while the first is open: one row, honest
                # count. Restarting the clock would erase how long the
                # operator has actually been waiting.
                self._pending += 1
                return True
            self._active = True
            self._started_at = self._now()
            self._text = str(text or "")
            self._verb = _verb_for(self._text)
            self._pending = 1
            self._last_reason = ""
            return True
        except Exception:  # noqa: BLE001
            logger.debug("[TurnSpinner] open degraded", exc_info=True)
            return False

    def note_reply(self) -> None:
        """An addressed frame landed — one pending question is answered.
        The row closes when the LAST one is. NEVER raises."""
        try:
            if not self._active:
                return
            self._pending = max(0, self._pending - 1)
            if self._pending == 0:
                self.close(reason="answered")
        except Exception:  # noqa: BLE001
            logger.debug("[TurnSpinner] note_reply degraded", exc_info=True)

    def close(self, *, reason: str = "") -> None:
        """Resolve the turn. Idempotent; safe from any path. Emits a
        tombstone only when the wait was long enough to be worth recording —
        a sub-second turn narrating its own duration is noise. NEVER
        raises."""
        try:
            if not self._active:
                return
            elapsed = self.age()
            self._active = False
            self._pending = 0
            self._last_reason = reason
            if reason in ("answered", "") and elapsed >= _tombstone_min_s():
                self._emit(f"  [dim]⎿ thought for {_fmt_elapsed(elapsed)}"
                           f"[/dim]")
            elif reason == "timeout":
                self._emit(
                    f"  [#e3b341]⎿ no answer after {_fmt_elapsed(elapsed)} — "
                    f"the organism went quiet (it may still be working; "
                    f"`/status` knows)[/#e3b341]"
                )
            elif reason == "interrupted":
                self._emit(f"  [dim]⎿ interrupted after "
                           f"{_fmt_elapsed(elapsed)}[/dim]")
        except Exception:  # noqa: BLE001
            logger.debug("[TurnSpinner] close degraded", exc_info=True)

    def tick(self) -> None:
        """Enforce the ceiling. Called from the render path, so it needs no
        timer of its own — a row nobody is drawing needs no expiry. NEVER
        raises."""
        try:
            if self._active and self.age() > _max_turn_s():
                self.close(reason="timeout")
        except Exception:  # noqa: BLE001
            pass

    # -- the row -----------------------------------------------------------

    def _adopted_heartbeat(self) -> Optional[dict]:
        """The heartbeat frame IF it describes this turn.

        The whole honesty of the row lives here. A frame whose own elapsed
        exceeds this turn's age is reporting work that started before the
        question was asked — an autonomous op the organism began on its own.
        Borrowing its token count would attribute someone else's work to the
        operator's question."""
        try:
            payload = self._heartbeat_fn()
            if not isinstance(payload, dict) or not payload.get("active"):
                return None
            hb_elapsed = float(payload.get("elapsed_s") or 0.0)
            if hb_elapsed > self.age() + _ADOPTION_SLACK_S:
                return None
            return payload
        except Exception:  # noqa: BLE001
            return None

    def render(self) -> str:
        """The live row as Rich markup, or "" when idle. NEVER raises."""
        try:
            if not is_turn_spinner_enabled():
                return ""
            self.tick()
            if not self._active:
                return ""
            now = self._now()
            glyph = _pulse(now)
            payload = self._adopted_heartbeat()
            verb = self._verb
            parts: List[str] = [_fmt_elapsed(self.age())]
            if payload is not None:
                verb = str(payload.get("verb") or verb)
                tokens = int(payload.get("tokens_total") or 0)
                if tokens > 0:
                    try:
                        from backend.core.ouroboros.battle_test.attach_heartbeat import (  # noqa: E501
                            _fmt_tokens,
                        )
                        parts.append(f"↓ {_fmt_tokens(tokens)} tokens")
                    except Exception:  # noqa: BLE001
                        parts.append(f"↓ {tokens} tokens")
                label = str(payload.get("provider_label")
                            or payload.get("provider") or "")
                if label:
                    parts.append(label)
            if self._pending > 1:
                parts.append(f"{self._pending} queued")
            return (f"[#43d6d0]{glyph}[/#43d6d0] "
                    f"[#6c7d77]{verb}… ({' · '.join(parts)})[/#6c7d77]")
        except Exception:  # noqa: BLE001
            logger.debug("[TurnSpinner] render degraded", exc_info=True)
            return ""


def _markup_to_ansi(markup: str, width: int = 200) -> str:
    """Rich markup → ANSI, the same way the canvas renders its own panel
    (a StringIO Console at the terminal's colour depth). One conversion
    idiom in this codebase, not two. NEVER raises — falls back to the
    markup stripped of its tags, which still reads."""
    try:
        import io
        from rich.console import Console
        buf = io.StringIO()
        Console(
            file=buf, width=width, color_system="truecolor",
            force_terminal=True, highlight=False, soft_wrap=True,
        ).print(markup, end="")
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        try:
            import re
            return re.sub(r"\[/?[^\]]*\]", "", markup)
        except Exception:  # noqa: BLE001
            return markup


def build_turn_row(spinner: TurnSpinner) -> Any:
    """A prompt_toolkit container for the live row, or None.

    ``ConditionalContainer`` rather than a zero-height Window: an idle
    cockpit must be EXACTLY as tall as it was before this feature existed,
    and a Window that renders an empty line still occupies a row. NEVER
    raises."""
    try:
        from prompt_toolkit.filters import Condition
        from prompt_toolkit.layout import ConditionalContainer, Window
        from prompt_toolkit.layout.controls import FormattedTextControl

        def _fragments() -> Any:
            try:
                markup = spinner.render()
                if not markup:
                    return []
                from prompt_toolkit.formatted_text import ANSI
                return ANSI(_markup_to_ansi(markup))
            except Exception:  # noqa: BLE001
                return []

        return ConditionalContainer(
            content=Window(
                content=FormattedTextControl(_fragments, focusable=False),
                height=1, wrap_lines=False,
            ),
            filter=Condition(lambda: bool(spinner.render())),
        )
    except Exception:  # noqa: BLE001
        logger.debug("[TurnSpinner] row unavailable", exc_info=True)
        return None


__all__ = [
    "MASTER_FLAG_ENV_VAR",
    "MAX_TURN_ENV_VAR",
    "TOMBSTONE_MIN_ENV_VAR",
    "TURN_SPINNER_SCHEMA_VERSION",
    "TURN_VERBS",
    "TurnSpinner",
    "VERBS_ENV_VAR",
    "build_turn_row",
    "is_turn_spinner_enabled",
    "turn_verbs",
]
