"""Known-good fix for problem_003 content sanitizer pipeline.

Reference solution used by the Phase 1.5.D.2 spine to sanity-check
that the test suite IS solvable when the coherent multi-site fix
is applied.

Bug summary:
  * Two type errors at the leaf primitives (strip_html_tags +
    truncate) when called with None.
  * One semantic contract violation at the LIST level
    (sanitize_thread) — None entries must be EXCLUDED from the
    output, not surfaced as empty strings.

Coherent three-site fix:
  1. strip_html_tags returns "" on None (no re.sub call).
  2. truncate returns "" on None (no len() call).
  3. sanitize_thread filters None entries BEFORE delegating to
     sanitize_comment.  This is the LOAD-BEARING semantic decision
     — type-error-suppression alone would emit empty strings,
     failing the exclusion contract.

This file is NOT loaded by the canonical ``list_corpus_problems``
walker (underscore-prefixed → skipped per Phase 1.5.A convention).
"""
from __future__ import annotations

import re


def strip_html_tags(text):
    # FIX: None-safe at the leaf primitive.
    if text is None:
        return ""
    return re.sub(r"<[^>]+>", "", text)


def truncate(text, max_length):
    # FIX: None-safe at the leaf primitive.
    if text is None:
        return ""
    if len(text) <= max_length:
        return text
    return text[:max_length] + "..."


def sanitize_comment(comment, max_length=100):
    stripped = strip_html_tags(comment)
    return truncate(stripped, max_length)


def sanitize_thread(thread):
    # FIX: filter None entries BEFORE sanitizing.  This honors the
    # documented exclusion contract (deleted comments don't surface
    # as empty rows in the moderation feed).
    return [sanitize_comment(c) for c in thread if c is not None]
