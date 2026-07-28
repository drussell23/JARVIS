"""Speech becomes text in the prompt, without ever eating what you typed.

Karen already hears whole utterances and answers them. What she could not do
is put the words where the operator could see and change them first — so a
misheard sentence was sent, not corrected. `audio_state_ipc` has streamed the
right shape all along::

    {"type":"transcript","role":"user","chunk":"…","final":bool,
     "utterance_id":"u-…"}

partial chunks refining toward a final. This turns that stream into text in
the prompt buffer, editable before it is sent.

The buffer is shared, and that is the whole problem
---------------------------------------------------
The prompt is not a transcript display. The operator may be typing into it at
the same moment chunks arrive, and both are writing to one string. Every
naive implementation of this feature destroys typing:

* "replace the last N characters" deletes whatever the operator typed after
  the last chunk;
* "replace from a stored offset" writes over their words the moment they add
  or delete a character before that offset;
* "append everything" leaves earlier partials stranded in the text.

So the provisional span is verified by CONTENT, never trusted by position.
Before replacing, the composer confirms the buffer still holds exactly the
text it last wrote, and re-locates it if the operator's edits moved it. If
that text is gone, changed, or ambiguous, the composer does not guess: it
lets go — commits whatever is there and starts a fresh span for what comes
next.

Never overwriting a byte the operator touched is worth more than a tidy
transcript. A dictation feature that occasionally eats a typed sentence is one
nobody trusts twice.

Refusing to guess
-----------------
Chunks can arrive out of order, twice, or after their own final — a socket
does not promise otherwise. Each utterance is therefore identified, its
chunks are sequenced, and anything stale is dropped rather than applied.
Karen's OWN speech (`role="karen"`) is never composed: she is talking, not
dictating, and putting her words in the operator's prompt would be absurd.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("Ouroboros.TranscriptComposer")

__all__ = [
    "TranscriptComposer", "TranscriptResult", "parse_transcript_frame",
    "live_transcript_enabled",
]

#: A single utterance past this is a runaway recogniser, not a sentence.
_MAX_CHARS = 4000


def live_transcript_enabled() -> bool:
    """Default ON. Off, an utterance is sent whole as it was before."""
    return os.environ.get(
        "JARVIS_LIVE_TRANSCRIPT_ENABLED", "1",
    ).strip().lower() not in ("0", "false", "no", "off")


class TranscriptResult:
    """What the caller should do to the buffer. All fields are optional."""

    __slots__ = ("text", "start", "end", "provisional", "released", "reason")

    def __init__(
        self,
        text: str = "",
        start: int = -1,
        end: int = -1,
        provisional: bool = True,
        released: bool = False,
        reason: str = "",
    ) -> None:
        #: Replacement text for the span.
        self.text = text
        #: Span to replace, in the CURRENT buffer. -1 means "no edit".
        self.start = start
        self.end = end
        #: Still refining — render it dimmed.
        self.provisional = provisional
        #: The composer stopped managing this span (final, or the operator
        #: edited inside it). Whatever is in the buffer now belongs to them.
        self.released = released
        #: Why, for the log and for tests. Never shown to the operator.
        self.reason = reason

    @property
    def edits(self) -> bool:
        return self.start >= 0 and self.end >= self.start

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        return (f"<TranscriptResult {self.reason!r} "
                f"span=({self.start},{self.end}) prov={self.provisional}>")


def parse_transcript_frame(frame: Any) -> Optional[Dict[str, Any]]:
    """Normalise a wire frame, or None if it is not a usable transcript.

    Karen's own speech is refused here rather than downstream — the one place
    it can be forgotten is the place it must not be.
    """
    try:
        if not isinstance(frame, dict):
            return None
        if str(frame.get("role", "user")) != "user":
            return None
        utterance = str(frame.get("utterance_id", "") or "").strip()
        if not utterance:
            # Unidentifiable: it cannot be sequenced against anything, so
            # applying it risks interleaving two utterances into one.
            return None
        return {
            "utterance_id": utterance,
            "text": str(frame.get("text", frame.get("chunk", "")) or ""),
            "final": bool(frame.get("final", False)),
            "seq": int(frame.get("seq", 0) or 0),
        }
    except Exception:  # noqa: BLE001
        return None


class TranscriptComposer:
    """Owns one provisional span in a buffer it does not exclusively control."""

    def __init__(self, max_chars: int = _MAX_CHARS) -> None:
        self._utterance: str = ""
        self._written: str = ""
        self._anchor: int = -1
        self._seq: int = -1
        self._max = max(1, int(max_chars))
        self.released_by_edit = 0
        self.dropped_stale = 0

    # -- state -------------------------------------------------------------

    @property
    def active(self) -> bool:
        return bool(self._utterance) and self._anchor >= 0

    @property
    def span(self) -> Tuple[int, int]:
        if not self.active:
            return (-1, -1)
        return (self._anchor, self._anchor + len(self._written))

    def reset(self) -> None:
        self._utterance = ""
        self._written = ""
        self._anchor = -1
        self._seq = -1

    # -- the span, located by CONTENT ---------------------------------------

    def _locate(self, buffer_text: str) -> Optional[int]:
        """Where our last-written text is now, or None if we must let go.

        Position is a hint, never the authority. The operator may have typed
        before the span (shifting it), after it (not), or inside it (in which
        case it is theirs now and we stop touching it).
        """
        if not self._written:
            return self._anchor if 0 <= self._anchor <= len(buffer_text) else None
        start, end = self._anchor, self._anchor + len(self._written)
        if 0 <= start and end <= len(buffer_text):
            if buffer_text[start:end] == self._written:
                return start                      # unmoved — the common case
        # Moved. Accept a relocation ONLY when it is unambiguous: two copies
        # of the same words mean we cannot tell which one is ours, and
        # guessing would rewrite a sentence the operator typed themselves.
        first = buffer_text.find(self._written)
        if first < 0 or buffer_text.find(self._written, first + 1) >= 0:
            return None
        return first

    # -- input -------------------------------------------------------------

    def on_chunk(
        self, frame: Any, buffer_text: str, cursor: int,
    ) -> TranscriptResult:
        """Fold one transcript frame into the buffer. NEVER raises.

        *buffer_text* and *cursor* are the LIVE buffer at this instant —
        passed in rather than read from a global, so the rule is provable
        without an Application and cannot consult a stale copy.
        """
        try:
            if not live_transcript_enabled():
                return TranscriptResult(reason="disabled")
            parsed = parse_transcript_frame(frame)
            if parsed is None:
                return TranscriptResult(reason="not_a_transcript")

            utterance = parsed["utterance_id"]
            text = parsed["text"][: self._max]

            # A different utterance: the previous one is finished as far as
            # this buffer is concerned. Commit it and anchor afresh at the
            # cursor, so a second hold appends where the operator is looking.
            if utterance != self._utterance:
                self._utterance = utterance
                self._written = ""
                self._seq = -1
                self._anchor = max(0, min(int(cursor), len(buffer_text)))

            seq = parsed["seq"]
            if seq and seq <= self._seq:
                # Out of order or duplicated. A socket does not promise
                # otherwise, and applying it would rewind the sentence.
                self.dropped_stale += 1
                return TranscriptResult(reason="stale_seq")
            if seq:
                self._seq = seq

            start = self._locate(buffer_text)
            if start is None:
                # Our words are gone, changed, or ambiguous — the operator
                # has been editing. Let go rather than guess.
                self.released_by_edit += 1
                self.reset()
                return TranscriptResult(released=True, reason="operator_edit")

            end = start + len(self._written)
            self._anchor = start
            self._written = text
            if parsed["final"]:
                # Let go HERE, not just in the returned flag. Staying active
                # after a final leaves the composer believing it still owns a
                # span the operator is now editing as ordinary text — the
                # next utterance would then splice into the middle of it.
                self.reset()
                return TranscriptResult(
                    text=text, start=start, end=end,
                    provisional=False, released=True, reason="final",
                )
            return TranscriptResult(
                text=text, start=start, end=end,
                provisional=True, released=False, reason="partial",
            )
        except Exception:  # noqa: BLE001 — a bad frame must not eat the prompt
            logger.debug("[TranscriptComposer] chunk degraded", exc_info=True)
            return TranscriptResult(reason="degraded")

    def commit(self) -> None:
        """The utterance is done; the text belongs to the operator now."""
        self.reset()

    def cancel(self, buffer_text: str) -> TranscriptResult:
        """Dictation was abandoned — take the provisional words back out.

        Only what we wrote, and only if it is still exactly ours. A cancel
        that deletes an edited sentence is worse than one that leaves a few
        stray words behind.
        """
        try:
            if not self.active or not self._written:
                self.reset()
                return TranscriptResult(reason="nothing_to_cancel")
            start = self._locate(buffer_text)
            if start is None:
                self.reset()
                return TranscriptResult(released=True, reason="edited_keep")
            end = start + len(self._written)
            self.reset()
            return TranscriptResult(
                text="", start=start, end=end,
                provisional=False, released=True, reason="cancelled",
            )
        except Exception:  # noqa: BLE001
            self.reset()
            return TranscriptResult(reason="degraded")
