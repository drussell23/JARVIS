"""Ouroboros GATE — ASCII/Unicode strictness for generated candidates.

Iron Gate (Manifesto §6) hard-reject for any non-ASCII codepoint in
generated code content. Prevents the ``rapidفuzz``-class failure mode
where the model emits a visually-similar Unicode glyph in an identifier
or module name position — a single Arabic ``ف`` or Cyrillic ``а`` can
make a package unresolvable while looking correct in most editors.

This is a deterministic gate — no LLM calls, pure text computation.

Coverage
--------
Single-file candidates
    Scan ``candidate["full_content"]`` (and ``raw_content`` as fallback).

Multi-file candidates (``JARVIS_MULTI_FILE_GEN_ENABLED``)
    Scan every entry in ``candidate["files"]`` — each ``file_path`` /
    ``full_content`` pair is validated independently so Unicode smuggled
    into file N cannot bypass the gate by hiding behind a clean file 1.

On rejection
------------
* A structured list of ``BadCodepoint`` samples is returned.
* ``format_rejection_reason`` produces an error string suitable for
  raising as ``RuntimeError`` — prefixed with ``ascii_corruption:`` so
  the orchestrator's retry-feedback loop recognises the failure class.
* ``build_retry_feedback`` produces a human/model-readable correction
  prompt, including the specific offenders (file, offset, codepoint,
  line, column) so the model can self-correct on retry.
* ``record_rejection`` increments an in-process telemetry counter
  (``ascii_gate_rejections_total``) accessible via
  ``get_rejection_count()`` for tests + dashboards.

The gate is gated by ``JARVIS_ASCII_GATE`` (default ``true``). Setting
it to ``false`` bypasses the gate entirely — scan functions still work
when called directly, but :func:`is_enabled` returns ``False`` so the
orchestrator's caller can skip the scan loop.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ─────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────

#: Default maximum number of offenders to collect before short-circuiting.
#: The model only needs a handful of samples to understand what it broke —
#: scanning past ~10 wastes CPU on a candidate that's already rejected.
_DEFAULT_MAX_SAMPLES = 5

#: Cap on total scan length per file to prevent pathological O(n) scans
#: on adversarial inputs. 10 MiB is ~2× the largest real file we ever
#: generate. Beyond this, truncate the scan and mark the result as
#: potentially incomplete.
_MAX_SCAN_BYTES = 10 * 1024 * 1024


def is_enabled() -> bool:
    """Return whether the ASCII gate is active (env-var controlled)."""
    return os.environ.get("JARVIS_ASCII_GATE", "true").lower() not in (
        "false", "0", "no", "off",
    )


# ─────────────────────────────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BadCodepoint:
    """A single non-ASCII codepoint found during scanning.

    Attributes
    ----------
    file_path:
        Candidate file path where the offender was found. May be
        ``"?"`` when the candidate shape is unknown.
    offset:
        Zero-based character offset within ``full_content``.
    char:
        The offending glyph (single codepoint).
    codepoint:
        Integer codepoint value (same as ``ord(char)``). Stored
        separately so formatters don't need to re-compute it.
    line:
        One-based line number containing the offender. ``1`` for the
        first line.
    column:
        One-based column number within ``line``.
    """

    file_path: str
    offset: int
    char: str
    codepoint: int
    line: int
    column: int

    def format_sample(self) -> str:
        """Format as ``path@offset:U+XXXX (line:col)`` for log/error strings."""
        return (
            f"{self.file_path}@{self.offset}:U+{self.codepoint:04X} "
            f"(L{self.line}:C{self.column})"
        )


# ─────────────────────────────────────────────────────────────────────
# Telemetry (process-local counter)
# ─────────────────────────────────────────────────────────────────────

_REJECTION_COUNT = 0


def record_rejection(num_samples: int = 1) -> None:
    """Increment the process-local rejection counter.

    ``num_samples`` lets callers record "how many offenders this
    rejection contained" in a single call, without leaking the counter
    internals into call sites.
    """
    global _REJECTION_COUNT
    _REJECTION_COUNT += max(1, int(num_samples))


def get_rejection_count() -> int:
    """Return the process-local rejection counter. Monotonic per process."""
    return _REJECTION_COUNT


def reset_rejection_count() -> None:
    """Reset the counter to zero. Primarily for tests."""
    global _REJECTION_COUNT
    _REJECTION_COUNT = 0


# ─────────────────────────────────────────────────────────────────────
# Core scan primitives
# ─────────────────────────────────────────────────────────────────────


def scan_content(
    content: str,
    file_path: str = "?",
    max_samples: int = _DEFAULT_MAX_SAMPLES,
) -> List[BadCodepoint]:
    """Scan a single string for non-ASCII codepoints.

    Parameters
    ----------
    content:
        The file content to scan. Non-string inputs return an empty
        list (the orchestrator may pass dict/None shapes on malformed
        candidates — don't crash, just report no offenders).
    file_path:
        Label attached to any :class:`BadCodepoint` found. Used in the
        error message when the orchestrator reports the rejection.
    max_samples:
        Stop scanning after this many offenders. ``0`` means "scan
        everything, collect nothing" (no-op). ``-1`` means "unbounded"
        — use with care on large candidates.

    Returns
    -------
    list[BadCodepoint]
        Ordered list of offenders (earliest first). Empty on success.
    """
    if not isinstance(content, str) or not content:
        return []

    # Cap scan length for adversarial inputs.
    scan_len = len(content)
    if scan_len > _MAX_SCAN_BYTES:
        scan_len = _MAX_SCAN_BYTES

    if max_samples == 0:
        return []

    offenders: List[BadCodepoint] = []
    line = 1
    col = 0
    for idx in range(scan_len):
        ch = content[idx]
        col += 1
        if ch == "\n":
            # Newline itself is ASCII — just advance the counter.
            line += 1
            col = 0
            continue
        cp = ord(ch)
        if cp > 127:
            offenders.append(
                BadCodepoint(
                    file_path=file_path,
                    offset=idx,
                    char=ch,
                    codepoint=cp,
                    line=line,
                    column=col,
                )
            )
            if max_samples > 0 and len(offenders) >= max_samples:
                break
    return offenders


def scan_candidate(
    candidate: Dict[str, Any],
    max_samples: int = _DEFAULT_MAX_SAMPLES,
) -> List[BadCodepoint]:
    """Scan every file in a candidate dict.

    Handles both candidate shapes emitted by the providers:

    * **Single-file**: ``{"file_path": "x.py", "full_content": "…"}``
      (``raw_content`` is used as fallback when ``full_content`` is
      missing — some providers round-trip raw text.)

    * **Multi-file**: ``{"files": [{"file_path": …,
      "full_content": …}, …]}`` — per-file scanning in list order so
      file N can't hide Unicode behind a clean file 1. Gated by
      ``JARVIS_MULTI_FILE_GEN_ENABLED`` at the orchestrator level; the
      gate itself is shape-agnostic and will scan whatever files list
      the candidate has.

    Returns
    -------
    list[BadCodepoint]
        All offenders from all files, in scan order, capped at
        ``max_samples`` total (not per-file) so a pathological
        200-file candidate doesn't produce a 1000-line error message.
    """
    if not isinstance(candidate, dict) or max_samples == 0:
        return []

    offenders: List[BadCodepoint] = []
    remaining = max_samples

    files_field = candidate.get("files")
    if isinstance(files_field, list) and files_field:
        for entry in files_field:
            if not isinstance(entry, dict):
                continue
            fp = str(entry.get("file_path", "") or "?")
            fc = entry.get("full_content", "")
            if not isinstance(fc, str):
                # Fallback for providers that only set raw_content.
                fc = entry.get("raw_content", "") or ""
            if not isinstance(fc, str):
                continue
            found = scan_content(fc, fp, max_samples=remaining)
            offenders.extend(found)
            if max_samples > 0:
                remaining = max_samples - len(offenders)
                if remaining <= 0:
                    break
        if offenders:
            return offenders
        # If the files list was well-formed but empty of content, fall
        # through to the single-file shape so degenerate cases still scan.

    # Single-file shape.
    primary_content = candidate.get("full_content", "")
    if not isinstance(primary_content, str) or not primary_content:
        primary_content = candidate.get("raw_content", "") or ""
    if isinstance(primary_content, str) and primary_content:
        fp = str(candidate.get("file_path", "") or "?")
        offenders.extend(scan_content(primary_content, fp, max_samples=remaining))

    return offenders


# ─────────────────────────────────────────────────────────────────────
# Rejection formatting (used by orchestrator + retry feedback loop)
# ─────────────────────────────────────────────────────────────────────


def format_rejection_reason(bad_chars: Sequence[BadCodepoint]) -> str:
    """Build the ``ascii_corruption: …`` error string.

    The ``ascii_corruption:`` prefix is load-bearing — the orchestrator
    retry-feedback loop classifies by this prefix (``orchestrator.py``
    matches ``_err_str.startswith("ascii_corruption")``). Do not change
    the prefix without updating that matcher.
    """
    if not bad_chars:
        return "ascii_corruption: (no samples — scan produced empty list)"

    samples = ", ".join(bc.format_sample() for bc in bad_chars)
    return (
        f"ascii_corruption: non-ASCII codepoint(s) in generated "
        f"content [{samples}]. ALL identifiers, keywords, and "
        f"module names MUST be 7-bit ASCII. String literals may "
        f"contain Unicode only inside explicit quotes. Re-emit "
        f"the file with correct ASCII spellings."
    )


def build_retry_feedback(bad_chars: Sequence[BadCodepoint]) -> str:
    """Build the GENERATE_RETRY feedback prompt fragment.

    Called from the orchestrator's retry loop when an ``ascii_corruption``
    rejection needs to be turned into an instructive correction prompt.
    """
    samples_block = "\n".join(
        f"  • {bc.file_path} line {bc.line}:{bc.column} — "
        f"U+{bc.codepoint:04X} '{bc.char}'"
        for bc in bad_chars[:5]
    ) or "  (no samples)"

    return (
        "## PREVIOUS GENERATION REJECTED — UNICODE CORRUPTION\n\n"
        "The previous candidate contained non-ASCII codepoints in code\n"
        "positions (identifiers, imports, keywords, module names):\n\n"
        f"{samples_block}\n\n"
        "INSTRUCTIONS FOR RETRY:\n"
        "- All identifiers, imports, and keywords MUST be 7-bit ASCII.\n"
        "- Re-emit the file using only ASCII for code tokens.\n"
        "- Common culprits: Arabic 'ف' for 'f', Cyrillic 'а' for 'a',\n"
        "  smart quotes for straight quotes, em-dash for hyphen.\n"
        "- Double-check package names (rapidfuzz, not rapidفuzz).\n"
        "- String literals MAY contain Unicode, but only inside quotes.\n"
    )


# ─────────────────────────────────────────────────────────────────────
# Class wrapper (policy holder for future extensions)
# ─────────────────────────────────────────────────────────────────────


class AsciiStrictGate:
    """Stateful facade over the scan primitives.

    The module-level functions are sufficient for most callers, but a
    class wrapper lets the orchestrator (and tests) hold a single
    policy object with configured sample cap and enable flag, rather
    than threading env vars and magic numbers through every call site.
    """

    def __init__(
        self,
        max_samples: int = _DEFAULT_MAX_SAMPLES,
        enabled: Optional[bool] = None,
    ) -> None:
        self.max_samples = max_samples
        # Explicit ``enabled`` param overrides env var — useful for
        # tests that need to force-enable or force-disable the gate
        # without monkey-patching the environment.
        self._enabled_override = enabled

    @property
    def enabled(self) -> bool:
        if self._enabled_override is not None:
            return self._enabled_override
        return is_enabled()

    def scan(self, candidate: Dict[str, Any]) -> List[BadCodepoint]:
        """Scan a candidate; empty list when gate is disabled."""
        if not self.enabled:
            return []
        return scan_candidate(candidate, max_samples=self.max_samples)

    def check(
        self, candidate: Dict[str, Any],
    ) -> Tuple[bool, Optional[str], List[BadCodepoint]]:
        """Run the gate and return ``(ok, reason, samples)``.

        ``ok=True`` means the candidate passed (no offenders or gate
        disabled). ``ok=False`` means the candidate must be rejected;
        ``reason`` is the pre-formatted error string for raising as
        ``RuntimeError`` and ``samples`` is the offender list for
        telemetry / retry feedback.
        """
        offenders = self.scan(candidate)
        if not offenders:
            return True, None, []
        record_rejection(len(offenders))
        return False, format_rejection_reason(offenders), offenders
