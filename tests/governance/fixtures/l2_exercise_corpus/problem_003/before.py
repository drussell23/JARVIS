"""Content sanitizer pipeline for a moderation feed.

The pipeline composes three primitives:

  * ``strip_html_tags(text)`` — strip ``<tag>`` markup, return plain
    text.
  * ``truncate(text, max_length)`` — truncate to ``max_length`` chars,
    appending ``"..."`` when truncation occurs.
  * ``sanitize_comment(comment, max_length=100)`` — composes the two
    primitives (strip then truncate) into a single per-comment
    sanitizer.

Plus the list-level entry point:

  * ``sanitize_thread(thread)`` — sanitize all comments in a
    moderation thread.  See its docstring for the None-handling
    contract (load-bearing for correctness).
"""
from __future__ import annotations

import re


def strip_html_tags(text):
    """Remove HTML/XML-style tags from ``text``.

    Returns the plain-text content with tags elided.  Whitespace is
    preserved verbatim (callers handle trim themselves).
    """
    # BUG: re.sub on None raises TypeError.
    return re.sub(r"<[^>]+>", "", text)


def truncate(text, max_length):
    """Truncate ``text`` to ``max_length`` characters.

    If ``text`` already fits within ``max_length``, return it
    unchanged.  Otherwise return the first ``max_length`` chars +
    ``"..."`` (so the displayed length is ``max_length + 3``).
    """
    # BUG: len() on None raises TypeError.
    if len(text) <= max_length:
        return text
    return text[:max_length] + "..."


def sanitize_comment(comment, max_length=100):
    """Sanitize a single user comment: strip HTML tags, then truncate.

    A return value of ``""`` means "the comment had no displayable
    content" (e.g. only tags, or empty input).
    """
    stripped = strip_html_tags(comment)
    return truncate(stripped, max_length)


def sanitize_thread(thread):
    """Sanitize all comments in a moderation thread.

    Contract:
      * Each non-None entry in ``thread`` is sanitized via
        :func:`sanitize_comment` and included in the output list,
        preserving insertion order.
      * ``None`` entries represent DELETED or MISSING comments and
        MUST BE EXCLUDED from the output.  We never surface a
        deleted comment as an empty row in the moderation feed —
        that would clutter the moderator's UI with rows that don't
        correspond to any actionable content.

    This is the LOAD-BEARING semantic invariant for the moderation
    feed UI; a naive fix that swallows None and returns ``""``
    would technically not crash but would silently violate the
    feed-density contract.
    """
    # BUG: iterates without filtering None entries.  sanitize_comment
    # crashes when called with None (via strip_html_tags inside).
    # Even if the leaf primitives are made None-safe, this loop
    # would emit "" entries for None comments, violating the
    # exclusion contract documented above.
    return [sanitize_comment(c) for c in thread]
