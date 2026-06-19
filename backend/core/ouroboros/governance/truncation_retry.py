"""Truncation-retry directive (pure) -- recover from generative output truncation
by retrying with a CHANGED output shape instead of re-yelling at the model.

When a provider elides/truncates its output (placeholder text -> the parser's
`all_candidates_syntax_error`), a blind full-content retry tends to truncate
again. Instead:
  * diff-capable model -> retry as 2b.1-diff (a unified diff is tiny; it cannot
    truncate a large file because the model never re-emits the whole file).
  * full-content-only model -> bump max_tokens for headroom + feedback that
    forbids elisions.
This module is the PURE decision layer; the orchestrator/provider act on it.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

_TRUE = {"1", "true", "yes", "on"}

# Failure-message markers that indicate generative truncation/elision.
_TRUNCATION_MARKERS = ("all_candidates_syntax_error", "placeholder")


def truncation_retry_enabled() -> bool:
    return os.environ.get("JARVIS_TRUNCATION_RETRY_ENABLED", "").strip().lower() in _TRUE


def is_truncation_failure(err_msg: Optional[str]) -> bool:
    """True iff the failure message indicates output truncation/elision."""
    if not err_msg:
        return False
    low = str(err_msg).lower()
    return any(m in low for m in _TRUNCATION_MARKERS)


@dataclass(frozen=True)
class RetryDirective:
    force_diff: bool
    new_max_tokens: int
    feedback: str


def _token_ceiling() -> int:
    try:
        return int(os.environ.get("JARVIS_TRUNCATION_RETRY_TOKEN_CEILING", "16384"))
    except Exception:
        return 16384


def build_truncation_retry_directive(*, diff_capable: bool, current_max_tokens: int) -> RetryDirective:
    """Produce the retry directive for a truncation failure.

    diff_capable -> force a 2b.1-diff retry (changes output shape; can't truncate).
    otherwise    -> bump max_tokens (min(2x, ceiling)) + an explicit no-elision prompt.
    """
    ceiling = _token_ceiling()
    if diff_capable:
        return RetryDirective(
            force_diff=True,
            new_max_tokens=max(int(current_max_tokens), 0) or ceiling,
            feedback=(
                "Your previous output was truncated or contained placeholders. "
                "Re-emit as a 2b.1-diff (unified diff) of ONLY the changed lines. "
                "Do NOT re-emit the entire file."
            ),
        )
    bumped = min(max(int(current_max_tokens), 1) * 2, ceiling)
    return RetryDirective(
        force_diff=False,
        new_max_tokens=bumped,
        feedback=(
            "Your previous output was truncated or contained placeholders such as "
            "'...' or '# rest of file unchanged'. Emit the COMPLETE file content with "
            "NO elisions and NO placeholders."
        ),
    )
