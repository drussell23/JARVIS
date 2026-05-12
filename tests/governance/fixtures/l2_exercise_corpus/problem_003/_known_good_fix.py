"""Known-good fix for problem_003 v2.

Reference solution used by the spine to sanity-check that the test
suite IS solvable.  Not loaded by the canonical
``list_corpus_problems`` walker (underscore-prefixed → skipped).

Three coordinated changes:

  1. ``clean`` returns ``""`` on None (no re.sub call).
  2. ``shorten`` returns ``""`` on None (no len() call).
  3. ``collect`` filters None entries BEFORE calling ``process``.

The third change is the load-bearing semantic decision inferred
from the ``collect`` test examples — e.g.,
``collect(["<p>alpha</p>", None, "<b>beta</b>"]) == ["alpha", "beta"]``
shows that None entries get DROPPED from the output (not mapped
to empty strings).  A fix that only adds None-safety at the leaf
primitives produces ``["alpha", "", "beta"]`` and fails this test.
"""
from __future__ import annotations

import re


_TAG = re.compile(r"<[^>]+>")


def clean(raw):
    if raw is None:
        return ""
    return _TAG.sub("", raw)


def shorten(value, limit):
    if value is None:
        return ""
    if len(value) <= limit:
        return value
    return value[:limit] + "..."


def process(item, limit=100):
    return shorten(clean(item), limit)


def collect(items):
    # Load-bearing: filter None entries BEFORE delegating to process.
    return [process(i) for i in items if i is not None]
