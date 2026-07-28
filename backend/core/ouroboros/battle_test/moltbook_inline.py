"""The room's commentary, attached to the work it is about.

Moltbook posts already carry `op_id` and `reply_to` — the schema was built
for attribution from the start. So a post is not ambient chatter that happens
to coexist with an operation; it BELONGS to one, and can render under it in
the same `⎿` grammar a tool result uses::

    ⏺ Validate(7759-86)
      ⎿ ✗ 3 failed · test_scoped_paths, test_sandbox_dir
      💬 @the-pit    three tests. you fixed the assert and broke the file.
         ▸ @the-builder   the containment check is right, the fixture is stale

Adjacency is the whole value. The same sentence in a side panel is
decoration; under the failure it is commentary, and scanning for `💬` finds
every moment something went sideways. When a persona speaks only on events,
their presence IS the error signal.

The race this has to survive
----------------------------
A reaction is formulated asynchronously. By the time it arrives, the
operation it is about may have scrolled out of the deck's window — or been
evicted from the ring entirely. Mutating a line that is no longer on screen
is how a TUI tears: the write lands in a buffer region the renderer has
already committed, and the frame is corrupt until something forces a full
repaint.

So placement is DECIDED before anything is written:

* **INLINE** — the parent's line is inside the current window. Inject under
  it, in the nested grammar, and invalidate normally.
* **GHOST** — the parent has scrolled away or been evicted. Do not touch it.
  Append a pointer at the live tail instead::

      💬 @cassandra commented on 7759-86 ↑

  which is honest about what happened and costs one line, versus a scroll
  jump that would yank the operator away from what they were reading.
* **MUTED** — posture says the room should be quiet.

Reading the room
----------------
Under `HARDEN` — an incident, a soak, a production repair — banter is
actively harmful: it competes for attention with the thing that is going
wrong. Only `⚔` conflict posts survive that gate, because a disagreement
between REVIEW and GENERATE is not banter; it is the system telling the
operator its own components do not agree.

This is what separates a team that reads the room from a channel you mute in
week two, and it is the reason the gate is on POSTURE rather than on a
verbosity flag: the organism already knows when it is in trouble.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.MoltbookInline")

__all__ = [
    "Placement", "decide_placement", "find_anchor", "inline_enabled",
    "is_conflict", "posture_allows", "render_ghost", "render_post",
    "render_thread", "thread_collapse_after",
    "INLINE", "GHOST", "MUTED",
]

#: Placements.
INLINE = "inline"
GHOST = "ghost"
MUTED = "muted"

#: Replies drawn before a thread folds. Two is enough to see that an argument
#: happened and who is on which side; the rest is available on demand and
#: must never bury a diff.
_COLLAPSE_AFTER = 2

#: Postures under which only conflict survives.
_QUIET_POSTURES = ("HARDEN",)


class Placement:
    """Where a post goes, and why — the reason is kept for the log."""

    __slots__ = ("kind", "anchor_index", "reason", "post")

    def __init__(self, kind: str, anchor_index: int = -1, reason: str = "",
                 post: Any = None) -> None:
        self.kind = kind
        #: Deck line index of the parent op, or -1.
        self.anchor_index = anchor_index
        self.reason = reason
        self.post = post

    @property
    def inline(self) -> bool:
        return self.kind == INLINE

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        return f"<Placement {self.kind} at={self.anchor_index} {self.reason!r}>"


def inline_enabled() -> bool:
    """Default ON. Off, posts stay out of the deck entirely."""
    return os.environ.get(
        "JARVIS_MOLTBOOK_INLINE_ENABLED", "1",
    ).strip().lower() not in ("0", "false", "no", "off")


def thread_collapse_after() -> int:
    try:
        return max(1, int(os.environ.get("JARVIS_MOLTBOOK_COLLAPSE_AFTER", "")
                          or _COLLAPSE_AFTER))
    except (TypeError, ValueError):
        return _COLLAPSE_AFTER


def is_conflict(post: Any) -> bool:
    """Does this post carry a disagreement?

    A `⚔` is REVIEW contesting GENERATE — the system reporting that its own
    components do not agree. That is not banter and must never be muted.
    """
    try:
        if isinstance(post, dict):
            kind = str(post.get("kind", "") or "")
            body = str(post.get("body", "") or "")
        else:
            kind = str(getattr(post, "kind", "") or "")
            body = str(getattr(post, "body", "") or "")
        return kind.lower() in ("conflict", "contest", "dispute") or "⚔" in body
    except Exception:  # noqa: BLE001
        return False


def posture_allows(post: Any, posture: str = "") -> bool:
    """May the room speak, given the organism's current posture?

    Gated on POSTURE rather than a verbosity flag because the organism
    already knows when it is in trouble — and an operator fighting an
    incident should not have to remember to turn the jokes off first.
    """
    try:
        if str(posture or "").strip().upper() not in _QUIET_POSTURES:
            return True
        return is_conflict(post)
    except Exception:  # noqa: BLE001
        return True


def _post_field(post: Any, name: str, default: str = "") -> str:
    try:
        if isinstance(post, dict):
            return str(post.get(name, default) or default)
        return str(getattr(post, name, default) or default)
    except Exception:  # noqa: BLE001
        return default


def find_anchor(deck_lines: Sequence[str], op_id: str) -> int:
    """Last line of *op_id*'s BLOCK, or -1.

    Not the header line. An op emits a header and then continuation lines —
    `⎿ ✗ 3 failed`, diff hunks — and only the header carries the id. Anchoring
    on the id alone puts a comment BETWEEN an operation and its own result,
    which reads as though the room interrupted the machine mid-sentence.

    So the block is walked forward from the header through everything
    indented under it, and the comment lands after the whole thing. A line
    starting a new top-level `⏺` ends the block.

    The short form is the op's TAIL PAIR (`7759-86`), never its last segment
    alone: UUIDv7 tails are two or three digits, and `86` matches `line 86`.
    """
    try:
        needle = str(op_id or "").strip()
        if not needle:
            return -1
        parts = [p for p in needle.replace(":", "-").split("-") if p]
        short = "-".join(parts[-2:]) if len(parts) >= 3 else ""
        header = -1
        for index, raw in enumerate(deck_lines or ()):
            text = str(raw)
            if needle in text or (len(short) >= 5 and short in text):
                header = index
        if header < 0:
            return -1
        # The block is everything INDENTED under the header. That is the
        # deck's actual grammar — `⏺` sits at column zero and `⎿`, diff hunks
        # and `💬` are all indented beneath it — and it terminates reliably,
        # which "until the next ⏺" does not: any unindented line at all ends
        # the block, so a comment can never be separated from its op by
        # unrelated output that happened to arrive between them.
        end = header
        for index in range(header + 1, len(deck_lines)):
            text = str(deck_lines[index])
            if not text.startswith((" ", "\t")) or not text.strip():
                break
            end = index
        return end
    except Exception:  # noqa: BLE001
        return -1


def decide_placement(
    post: Any,
    deck_lines: Sequence[str],
    *,
    window: Optional[Tuple[int, int]] = None,
    posture: str = "",
) -> Placement:
    """Where this post goes. NEVER raises, NEVER mutates anything.

    *window* is ``(start, end)`` — the absolute deck indices currently drawn,
    which the caller reads from the viewport rather than assuming. Deciding
    BEFORE writing is the whole safety property: an inline injection aimed at
    a line the renderer has already committed is what tears a frame.
    """
    try:
        if not inline_enabled():
            return Placement(MUTED, reason="disabled", post=post)
        if not posture_allows(post, posture):
            return Placement(MUTED, reason="posture_quiet", post=post)

        op_id = _post_field(post, "op_id")
        if not op_id:
            # No anchor at all. It cannot be commentary on anything, so it
            # would land beside work it has nothing to do with.
            return Placement(GHOST, reason="unattributed", post=post)

        anchor = find_anchor(deck_lines, op_id)
        if anchor < 0:
            # Evicted from the ring, or never rendered here.
            return Placement(GHOST, reason="anchor_evicted", post=post)

        if window is not None:
            start, end = int(window[0]), int(window[1])
            if not (start <= anchor < end):
                # Scrolled away. Touching it would write into a region the
                # renderer has already committed.
                return Placement(GHOST, anchor, "anchor_offscreen", post)

        return Placement(INLINE, anchor, "visible", post)
    except Exception:  # noqa: BLE001 — a comment must never break the deck
        logger.debug("[MoltbookInline] placement degraded", exc_info=True)
        return Placement(MUTED, reason="degraded", post=post)


def _tint(text: str, tone: str) -> str:
    """Colour the CHROME of a post — its glyph and its handle — and nothing else.

    Deliberately narrow. The leading pad stays literal because placement math
    upstream measures indentation with ``lstrip()``, and the body and ref stay
    literal because a post's text is operator-supplied content that must not
    be re-parsed as markup. Styling only the two tokens the eye scans for is
    what lets a thread carry hierarchy without the deck growing a second
    layout vocabulary.

    Degrades to the plain token on any failure — a post rendering uncoloured
    is a cosmetic loss; a post not rendering is a lost disagreement.
    """
    try:
        from backend.core.ouroboros.ui.theme import semantic
        style = semantic(tone)
        return f"[{style}]{text}[/{style}]" if style else text
    except Exception:  # noqa: BLE001
        return text


def render_post(post: Any, *, depth: int = 0, width: int = 78) -> List[str]:
    """The `💬` block for one post, in the nested `⎿` grammar.

    A reply is drawn with `▸` and one more indent, so threading falls out of
    the same column discipline tool results already use — no second layout
    vocabulary for the operator to learn.
    """
    try:
        handle = _post_field(post, "handle") or "@someone"
        body = " ".join(_post_field(post, "body").split())
        ref = _post_field(post, "ref")
        # `▸` already says "reply", so a second marker beside it is noise.
        # `⚔` is NOT noise at any depth — a contested reply is the one an
        # operator most needs to spot while scanning.
        conflict = is_conflict(post)
        if depth and not conflict:
            glyph = "▸"
        else:
            glyph = "⚔" if conflict else "💬"
            if depth:
                glyph = f"▸ {glyph}"
        pad = "  " + ("   " * max(0, int(depth)))
        lead = f"{pad}{glyph} {handle}"
        # Room is measured on the PLAIN lead, before any styling — a clip
        # computed against markup would count `[#3FB950]` as visible text and
        # truncate every post by the length of its own colour tags.
        room = max(24, width - len(lead) - len(ref) - 4)
        if len(body) > room:
            body = body[: room - 1].rstrip() + "…"
        line = f"{pad}{_tint(glyph, 'crit' if conflict else 'info')} " \
               f"{_tint(handle, 'ink')}  {body}"
        if ref:
            line = f"{line}  {ref}"
        return [line]
    except Exception:  # noqa: BLE001
        return []


def render_thread(posts: Sequence[Any], *, width: int = 78) -> List[str]:
    """A post and its replies, folding once an argument gets long.

    Two replies show that a disagreement happened and who is on each side.
    Beyond that it is available on demand — an argument must never bury the
    diff it is about.
    """
    try:
        rows = list(posts or ())
        if not rows:
            return []
        out = render_post(rows[0], depth=0, width=width)
        limit = thread_collapse_after()
        for reply in rows[1:1 + limit]:
            out.extend(render_post(reply, depth=1, width=width))
        hidden = max(0, len(rows) - 1 - limit)
        if hidden:
            out.append(f"    💬 {hidden} more repl{'y' if hidden == 1 else 'ies'}"
                       f"          ⏎ to expand")
        return out
    except Exception:  # noqa: BLE001
        return []


def render_ghost(post: Any) -> str:
    """``💬 @cassandra commented on 7759-86 ↑`` — one line, at the live tail.

    Honest about what happened, and it costs one line. The alternative — a
    scroll jump to the parent — would yank the operator away from whatever
    they were reading to show them a joke.
    """
    try:
        handle = _post_field(post, "handle") or "@someone"
        op_id = _post_field(post, "op_id")
        parts = [p for p in op_id.replace(":", "-").split("-") if p]
        ref = "-".join(parts[-2:]) if len(parts) >= 3 else (op_id or "an op")
        glyph = "⚔" if is_conflict(post) else "💬"
        where = f" on {ref}" if op_id else ""
        return f"  {glyph} {handle} commented{where} ↑"
    except Exception:  # noqa: BLE001
        return ""
