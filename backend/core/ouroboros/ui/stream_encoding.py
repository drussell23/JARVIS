"""Guarantee the standard streams can carry the design language's glyphs.

## What this does NOT fix

The reported symptom — ``≡ƒÆ¡`` where ``💭`` belongs — is NOT this. Measured on
the reporting host: ``sys.stdout.encoding`` is ``utf-8``, ``LANG`` is
``C.UTF-8``, and the bytes Python emits are ``f0 9f 92 ad``, which is correct
UTF-8 for ``U+1F4AD``. The same bytes are correct in the daemon log. They are
then DECODED as cp437 by the Windows console, which produces exactly those four
characters. Nothing on the Python side can change what a terminal does with
bytes it has already received; that is a console codepage setting, and
``reconfigure`` here would have been ceremony that fixed nothing.

## What it does fix

A stream that genuinely cannot ENCODE the output. The organism runs from cron,
from systemd units, from `docker exec`, and from shells where ``LANG`` is unset
or ``C`` — and in those the preferred encoding is ASCII. A single ``⏺`` then
raises ``UnicodeEncodeError`` inside a render path, and the operator loses the
line (or the process) to a glyph.

So the check is a QUESTION, not an assertion: can this stream represent the
characters the design language actually uses? Only a stream that answers no is
reconfigured, and a stream that cannot be reconfigured is left alone with its
errors handler softened instead. Nothing is forced on a stream that was already
correct, which is every POSIX host in normal operation.
"""
from __future__ import annotations

import logging
import sys
from typing import Any, List, Tuple

logger = logging.getLogger("Ouroboros.StreamEncoding")

__all__ = ["stream_can_carry_glyphs", "ensure_glyph_capable_streams"]

#: A representative sample of what the design language emits, drawn from the
#: glyphs already in use rather than an invented list: the tool-activity dot,
#: the box-drawing result arm, the thinking balloon, and the em dash every
#: status line uses. If a stream can carry these it can carry the rest.
_PROBE = "⏺⎿💭—·"


def stream_can_carry_glyphs(stream: Any) -> bool:
    """Whether *stream* can encode the design language. NEVER raises.

    A stream with no declared encoding is treated as capable: it is either a
    test double or an in-memory buffer, and refusing those would make this
    guard fire in exactly the places that do not need it.
    """
    try:
        enc = getattr(stream, "encoding", None)
        if not enc:
            return True
        _PROBE.encode(enc)
        return True
    except (LookupError, UnicodeEncodeError):
        return False
    except Exception:  # noqa: BLE001 — an unanswerable stream is left alone
        return True


def ensure_glyph_capable_streams(
    streams: Any = None, *, encoding: str = "utf-8",
) -> List[Tuple[str, str, str]]:
    """Reconfigure only the streams that cannot carry the glyphs.

    Returns ``[(name, was, now), …]`` for what it changed — empty on every
    host that was already correct, which makes "it did nothing" the visible,
    normal outcome rather than a silence.

    NEVER raises: a stream that refuses reconfiguration keeps working with
    whatever it had, because a guard that breaks stdout to protect stdout has
    inverted its own purpose.
    """
    changed: List[Tuple[str, str, str]] = []
    targets = streams if streams is not None else (
        ("stdout", sys.stdout), ("stderr", sys.stderr),
    )
    for name, stream in targets:
        try:
            if stream is None or stream_can_carry_glyphs(stream):
                continue
            was = str(getattr(stream, "encoding", "?"))
            reconfigure = getattr(stream, "reconfigure", None)
            if not callable(reconfigure):
                # Python < 3.7 or a wrapped stream. Nothing to do, and saying
                # so beats pretending the stream is fine.
                logger.debug(
                    "[StreamEncoding] %s cannot encode the design language "
                    "(%s) and offers no reconfigure()", name, was,
                )
                continue
            # `errors="replace"` alongside: a glyph that still cannot be
            # represented should degrade to a substitute character, never to
            # an exception raised from inside a render.
            reconfigure(encoding=encoding, errors="replace")
            now = str(getattr(stream, "encoding", encoding))
            changed.append((name, was, now))
            logger.info(
                "[StreamEncoding] %s reconfigured %s -> %s so the design "
                "language's glyphs survive", name, was, now,
            )
        except Exception:  # noqa: BLE001 — never break a stream to protect it
            logger.debug(
                "[StreamEncoding] %s left as found", name, exc_info=True,
            )
    return changed
