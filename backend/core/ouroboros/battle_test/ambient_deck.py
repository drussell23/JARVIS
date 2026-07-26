"""The ambient deck — what the organism is doing, below the prompt.

Addressed output (you typed ``/moltbook``) belongs in the scrollback: you
asked for it, you keep it. Ambient output — a worker spawning, a provider
failing over, a persona arguing with you — belongs in a live region that
redraws in place and ages out. That distinction already exists on the wire
as of the Omni-Channel bus; this is the surface that consumes it.

Why not a deque
---------------
A FIFO ring treats every event as equally worth a slot, so the loudest
producer wins. The agora posts on its own initiative and never stops, which
means a five-row FIFO is a five-row feed of personas — and the row that said
"DoubleWord failed over" is gone in under a second. That is not a cosmetic
problem: it is a monitoring surface silently losing the only line that
mattered, to a joke.

So slots are allocated by SEVERITY, not arrival:

  * ``FATAL`` / ``WARN`` are OPERATIONAL. They pin. Nothing of lower severity
    can evict them, and they leave only by explicit resolution or their own
    TTL — an alert that scrolls itself away has not been seen, it has been
    lost.
  * ``INFO`` fills what remains, newest first.
  * ``SOCIAL`` gets whatever is left, and when there is nothing left it
    COMPACTS: the whole agora collapses to one rolling line carrying a count.
    It never takes a slot from an operational row, and it is never silently
    discarded either — the count is the evidence that something happened.

Keys, not rows
--------------
Entries are keyed. A provider flapping publishes the same key repeatedly and
updates one row rather than filling the deck with itself, which is the other
way a chatty producer takes the screen.
"""
from __future__ import annotations

import enum
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

_TRUTHY = ("1", "true", "yes", "on")


class Severity(enum.IntEnum):
    """Ordered so comparison is meaningful — higher wins a slot."""

    SOCIAL = 0
    INFO = 1
    WARN = 2
    FATAL = 3


#: Severities that pin. These describe the ORGANISM's health, and the
#: operator's decision to keep working depends on seeing them.
OPERATIONAL: Tuple[Severity, ...] = (Severity.FATAL, Severity.WARN)


def deck_enabled() -> bool:
    """``JARVIS_AMBIENT_DECK`` (default ON). OFF returns the single-line
    toolbar, which is the pre-deck behaviour and the only honest A/B."""
    return os.environ.get(
        "JARVIS_AMBIENT_DECK", "1",
    ).strip().lower() in _TRUTHY


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(os.environ.get(name, "").strip() or default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    try:
        return max(lo, min(hi, float(os.environ.get(name, "").strip() or default)))
    except (TypeError, ValueError):
        return default


def max_rows() -> int:
    """Deck height, excluding the pulse line the toolbar always shows."""
    return _env_int("JARVIS_AMBIENT_DECK_ROWS", 4, 1, 20)


def social_ttl_s() -> float:
    return _env_float("JARVIS_AMBIENT_DECK_SOCIAL_TTL_S", 25.0, 1.0, 3600.0)


def info_ttl_s() -> float:
    return _env_float("JARVIS_AMBIENT_DECK_INFO_TTL_S", 60.0, 1.0, 3600.0)


def operational_ttl_s() -> float:
    """Deliberately long. An operational alert is not noise that should fade
    while the operator is reading their screen; it is state that stopped being
    true or was acknowledged."""
    return _env_float("JARVIS_AMBIENT_DECK_OP_TTL_S", 900.0, 1.0, 86400.0)


@dataclass
class DeckEntry:
    key: str
    text: str
    severity: Severity
    ts: float
    hits: int = 1

    @property
    def pinned(self) -> bool:
        return self.severity in OPERATIONAL


@dataclass
class _Social:
    """The compaction bucket. Social events that could not get a slot are
    counted here rather than dropped, so the deck can always say how much it
    is not showing."""

    hidden: int = 0
    last_text: str = ""
    last_ts: float = 0.0
    authors: List[str] = field(default_factory=list)

    def note(self, text: str, author: str, now: float) -> None:
        self.hidden += 1
        self.last_text = text
        self.last_ts = now
        if author and author not in self.authors:
            self.authors.append(author)
            del self.authors[:-3]

    def clear(self) -> None:
        self.hidden = 0
        self.authors.clear()


class DeckManager:
    """Severity-ordered ambient rows for the bottom toolbar.

    Pure logic: no prompt_toolkit, no Rich, no I/O. The toolbar asks it for
    lines; tests ask it the same question. NEVER raises from ``push`` or
    ``render`` — this sits on a background receive path and on the render
    hook, and an exception in either detaches the cockpit or blanks it.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        rows: Optional[int] = None,
    ) -> None:
        self._clock = clock
        self._rows = rows
        self._entries: Dict[str, DeckEntry] = {}
        self._social = _Social()
        self.dropped_social = 0

    # -- ingest ----------------------------------------------------------

    def push(
        self,
        text: str,
        *,
        severity: Severity = Severity.INFO,
        key: Optional[str] = None,
        author: str = "",
    ) -> None:
        """Offer one ambient event to the deck. NEVER raises."""
        try:
            text = str(text or "").strip()
            if not text:
                return
            now = self._clock()
            self._expire(now)

            if severity is Severity.SOCIAL:
                self._push_social(text, author, now)
                return

            k = key or f"{severity.name}:{text[:60]}"
            existing = self._entries.get(k)
            if existing is not None:
                existing.text = text
                existing.ts = now
                existing.hits += 1
                existing.severity = max(existing.severity, severity)
                return
            self._entries[k] = DeckEntry(
                key=k, text=text, severity=severity, ts=now,
            )
            self._evict_if_needed()
        except Exception:  # noqa: BLE001 — the deck must never break the UI
            pass

    def _push_social(self, text: str, author: str, now: float) -> None:
        """Social gets a real row only if one is free AFTER operational and
        info rows have taken theirs. Otherwise it compacts."""
        limit = self._rows if self._rows is not None else max_rows()
        non_social = [e for e in self._entries.values()
                      if e.severity is not Severity.SOCIAL]
        # One slot is reserved for the compaction line whenever anything is
        # hidden, so the operator always learns that chatter exists.
        if len(non_social) >= limit:
            self._social.note(text, author, now)
            self.dropped_social += 1
            return
        # A single social row, replaced in place: the agora is ONE voice on
        # this surface no matter how many residents are talking. Without this
        # a lively conversation still evicts info rows one by one.
        self._entries["__social__"] = DeckEntry(
            key="__social__", text=text, severity=Severity.SOCIAL, ts=now,
            hits=self._entries.get("__social__", DeckEntry(
                "", "", Severity.SOCIAL, now, 0)).hits + 1,
        )
        if author:
            self._social.authors.append(author)
            del self._social.authors[:-3]
        self._evict_if_needed()

    def resolve(self, key: str) -> bool:
        """Clear a pinned row because the condition ended. Returns True if
        something was removed. This is the intended exit for an operational
        alert — TTL is the backstop, not the mechanism."""
        try:
            return self._entries.pop(key, None) is not None
        except Exception:  # noqa: BLE001
            return False

    # -- eviction --------------------------------------------------------

    def _expire(self, now: float) -> None:
        for k, e in list(self._entries.items()):
            ttl = (
                operational_ttl_s() if e.pinned
                else social_ttl_s() if e.severity is Severity.SOCIAL
                else info_ttl_s()
            )
            if now - e.ts > ttl:
                del self._entries[k]
        if self._social.hidden and now - self._social.last_ts > social_ttl_s():
            self._social.clear()

    def _evict_if_needed(self) -> None:
        """Drop the least important row when over capacity.

        Ordering is (severity, recency) ASCENDING, so the victim is the least
        severe and, among equals, the oldest. A pinned row is never the victim
        while any unpinned row exists — that is the whole point."""
        limit = self._rows if self._rows is not None else max_rows()
        if len(self._entries) <= limit:
            return
        ordered = sorted(
            self._entries.values(), key=lambda e: (int(e.severity), e.ts),
        )
        for victim in ordered:
            if len(self._entries) <= limit:
                break
            if victim.severity is Severity.SOCIAL:
                self._social.note(victim.text, "", victim.ts)
                self.dropped_social += 1
            self._entries.pop(victim.key, None)

    # -- render ----------------------------------------------------------

    def rows(self) -> List[Tuple[Severity, str]]:
        """Deck contents, most important first. NEVER raises."""
        try:
            now = self._clock()
            self._expire(now)
            limit = self._rows if self._rows is not None else max_rows()
            ordered = sorted(
                self._entries.values(),
                key=lambda e: (-int(e.severity), -e.ts),
            )
            out: List[Tuple[Severity, str]] = [
                (e.severity, e.text) for e in ordered[:limit]
            ]
            if self._social.hidden:
                line = self._compaction_line()
                if len(out) < limit:
                    out.append((Severity.SOCIAL, line))
                else:
                    # Every slot is operational. Rather than evict one, the
                    # count rides on the least-important visible row so the
                    # operator still learns chatter is being withheld.
                    sev, last = out[-1]
                    if sev is not Severity.SOCIAL:
                        out[-1] = (sev, f"{last}   [dim]· {line}[/dim]")
            return out
        except Exception:  # noqa: BLE001
            return []

    def _compaction_line(self) -> str:
        who = ", ".join(self._social.authors[-2:])
        who = f" {who}" if who else ""
        return f"🐍 agora{who} ({self._social.hidden} hidden)"

    # -- introspection (for /deck and tests) ------------------------------

    @property
    def hidden_social(self) -> int:
        return self._social.hidden

    def pinned_keys(self) -> List[str]:
        return [e.key for e in self._entries.values() if e.pinned]


#: Glyph per severity — the deck's whole visual vocabulary.
GLYPHS: Dict[Severity, str] = {
    Severity.FATAL: "✖",
    Severity.WARN: "▲",
    Severity.INFO: "▸",
    Severity.SOCIAL: "·",
}

#: Rich style per severity. Named rather than literal so the theme owns them.
STYLES: Dict[Severity, str] = {
    Severity.FATAL: "bold red",
    Severity.WARN: "yellow",
    Severity.INFO: "cyan",
    Severity.SOCIAL: "dim",
}


def classify(text: str, *, kind: str = "") -> Tuple[Severity, str]:
    """Best-effort severity + key for an untyped ambient line.

    The daemon does not yet tag frames with severity, so the client infers it.
    That is a stopgap and it is marked as one: when the typed event spine
    carries severity, this function should be deleted rather than tuned.
    NEVER raises."""
    try:
        low = str(text or "").lower()
        k = str(kind or "").lower()
        if k == "molt_post" or "🐍" in str(text):
            return Severity.SOCIAL, "__social__"
        for token in ("fatal", "exhausted", "cannot continue", "aborted",
                      "circuit tripped", "wedged"):
            if token in low:
                return Severity.FATAL, f"fatal:{token}"
        for token in ("failover", "degraded", "warning", "retry", "throttl",
                      "quarantin", "budget", "stale", "timeout"):
            if token in low:
                return Severity.WARN, f"warn:{token}"
        return Severity.INFO, ""
    except Exception:  # noqa: BLE001
        return Severity.INFO, ""


__all__ = [
    "GLYPHS",
    "OPERATIONAL",
    "STYLES",
    "DeckEntry",
    "DeckManager",
    "Severity",
    "classify",
    "deck_enabled",
    "max_rows",
]
