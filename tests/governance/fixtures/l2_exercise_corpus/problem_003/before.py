"""String-processing pipeline."""
from __future__ import annotations

import re


_TAG = re.compile(r"<[^>]+>")


def clean(raw):
    """Return ``raw`` with HTML-style tags removed."""
    return _TAG.sub("", raw)


def shorten(value, limit):
    """Return ``value`` truncated to ``limit`` characters (appending
    ``"..."`` when truncation occurs)."""
    if len(value) <= limit:
        return value
    return value[:limit] + "..."


def process(item, limit=100):
    """Apply :func:`clean` and :func:`shorten` to ``item``."""
    return shorten(clean(item), limit)


def collect(items):
    """Return a list with :func:`process` applied to each entry of
    ``items``."""
    return [process(i) for i in items]
