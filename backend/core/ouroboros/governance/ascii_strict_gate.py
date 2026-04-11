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

Auto-repair (punctuation safe-list)
-----------------------------------
Before hard-rejecting, the gate applies a deterministic repair pass
over a safe-list of common Unicode punctuation → ASCII equivalents
(em-dash → ``-``, curly quotes → straight, ellipsis → ``...`` etc).
This fixes the deterministic training-data artifact where Claude emits
U+2014 in a ``requirements.txt`` comment on every generation for the
same file, without relaxing the ``rapidفuzz``-class letter guard —
Unicode *letters* (Arabic, Cyrillic, Greek etc) still hard-fail
because they can occupy identifier positions. Gated by
``JARVIS_ASCII_GATE_AUTO_REPAIR`` (default ``true``).
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


def is_auto_repair_enabled() -> bool:
    """Return whether the punctuation auto-repair pass is active.

    Default ``true``. Set ``JARVIS_ASCII_GATE_AUTO_REPAIR=false`` to
    disable and restore pure reject-only behaviour (useful for tests
    that want to observe the raw rejection path).
    """
    return os.environ.get(
        "JARVIS_ASCII_GATE_AUTO_REPAIR", "true",
    ).lower() not in ("false", "0", "no", "off")


# ─────────────────────────────────────────────────────────────────────
# Unicode punctuation auto-repair table
# ─────────────────────────────────────────────────────────────────────
#
# Safe-list only. The mapping rule is: "would a human reading this as
# plain text accept the ASCII substitute as visually-equivalent?". We
# DO NOT repair Unicode letters (Arabic/Cyrillic/Greek/etc) because
# those can occupy Python identifier positions and silently make a
# package unresolvable (the ``rapidفuzz``-class failure). Letters must
# hard-fail the gate so the model re-generates.
#
# Every entry here is punctuation, whitespace, or a symbol that has an
# unambiguous ASCII substitute. Order within a codepoint doesn't matter
# — this is a lookup table, applied via ``str.translate``.

_UNICODE_REPAIR_MAP: Dict[int, str] = {
    # Dashes / minus
    0x2010: "-",   # HYPHEN
    0x2011: "-",   # NON-BREAKING HYPHEN
    0x2012: "-",   # FIGURE DASH
    0x2013: "-",   # EN DASH
    0x2014: "-",   # EM DASH  ← the main offender in requirements.txt
    0x2015: "-",   # HORIZONTAL BAR
    0x2212: "-",   # MINUS SIGN
    # Single quotes / apostrophe
    0x2018: "'",   # LEFT SINGLE QUOTATION MARK
    0x2019: "'",   # RIGHT SINGLE QUOTATION MARK (also "apostrophe")
    0x201A: "'",   # SINGLE LOW-9 QUOTATION MARK
    0x201B: "'",   # SINGLE HIGH-REVERSED-9 QUOTATION MARK
    0x2032: "'",   # PRIME
    0x02B9: "'",   # MODIFIER LETTER PRIME
    0x02BC: "'",   # MODIFIER LETTER APOSTROPHE
    # Double quotes
    0x201C: '"',   # LEFT DOUBLE QUOTATION MARK
    0x201D: '"',   # RIGHT DOUBLE QUOTATION MARK
    0x201E: '"',   # DOUBLE LOW-9 QUOTATION MARK
    0x201F: '"',   # DOUBLE HIGH-REVERSED-9 QUOTATION MARK
    0x2033: '"',   # DOUBLE PRIME
    0x2036: '"',   # REVERSED DOUBLE PRIME
    0x00AB: '"',   # LEFT-POINTING DOUBLE ANGLE QUOTATION MARK
    0x00BB: '"',   # RIGHT-POINTING DOUBLE ANGLE QUOTATION MARK
    # Ellipsis
    0x2026: "...",  # HORIZONTAL ELLIPSIS
    # Spaces
    0x00A0: " ",   # NO-BREAK SPACE
    0x2002: " ",   # EN SPACE
    0x2003: " ",   # EM SPACE
    0x2004: " ",   # THREE-PER-EM SPACE
    0x2005: " ",   # FOUR-PER-EM SPACE
    0x2006: " ",   # SIX-PER-EM SPACE
    0x2007: " ",   # FIGURE SPACE
    0x2008: " ",   # PUNCTUATION SPACE
    0x2009: " ",   # THIN SPACE
    0x200A: " ",   # HAIR SPACE
    0x202F: " ",   # NARROW NO-BREAK SPACE
    0x205F: " ",   # MEDIUM MATHEMATICAL SPACE
    0x3000: " ",   # IDEOGRAPHIC SPACE
    # Zero-width / invisible (strip — these are notorious for code pollution)
    0x200B: "",    # ZERO WIDTH SPACE
    0x200C: "",    # ZERO WIDTH NON-JOINER
    0x200D: "",    # ZERO WIDTH JOINER
    0x2060: "",    # WORD JOINER
    0xFEFF: "",    # ZERO WIDTH NO-BREAK SPACE (BOM)
    # Bullets / list markers (only inside comments/prose — safe to ASCII-ize)
    0x2022: "*",   # BULLET
    0x2023: ">",   # TRIANGULAR BULLET
    0x25E6: "o",   # WHITE BULLET
    0x2043: "-",   # HYPHEN BULLET
    # Common typographic symbols with clear ASCII equivalents
    0x00D7: "x",   # MULTIPLICATION SIGN
    0x00F7: "/",   # DIVISION SIGN
    0x00B7: ".",   # MIDDLE DOT
    0x2044: "/",   # FRACTION SLASH
    # Section / paragraph marks — Claude emits these in Manifesto-style
    # comments (§5, §6, ¶3) and they're harmless inside prose. Single-
    # char substitutes keep file offsets stable for downstream tools.
    0x00A7: "S",   # SECTION SIGN (§)  — main offender in §6 references
    0x00B6: "P",   # PILCROW SIGN  (¶)
    # Daggers / footnote markers (used in academic prose, never in code)
    0x2020: "+",   # DAGGER
    0x2021: "++",  # DOUBLE DAGGER
    # Math comparison symbols Claude emits in comment-side commentary.
    # These have unambiguous ASCII operator forms; we never expect to see
    # them inside actual Python code (where != >= <= already exist).
    0x2260: "!=",  # NOT EQUAL TO
    0x2264: "<=",  # LESS-THAN OR EQUAL TO
    0x2265: ">=",  # GREATER-THAN OR EQUAL TO
    0x2248: "~=",  # ALMOST EQUAL TO
    0x00B1: "+/-", # PLUS-MINUS SIGN
    # Arrows — Claude leans on these heavily in flow diagrams in
    # docstrings (CLASSIFY → ROUTE → GENERATE …). Map to ASCII arrow forms.
    0x2190: "<-",  # LEFTWARDS ARROW
    0x2192: "->",  # RIGHTWARDS ARROW
    0x2191: "^",   # UPWARDS ARROW
    0x2193: "v",   # DOWNWARDS ARROW
    0x21D0: "<=",  # LEFTWARDS DOUBLE ARROW
    0x21D2: "=>",  # RIGHTWARDS DOUBLE ARROW
    0x2194: "<->", # LEFT RIGHT ARROW
    # Trademark / copyright marks — common in license headers
    0x00A9: "(c)", # COPYRIGHT SIGN
    0x00AE: "(r)", # REGISTERED SIGN
    0x2122: "(tm)", # TRADE MARK SIGN
    # Check / cross marks — often slip into TUI/banner strings
    0x2713: "v",   # CHECK MARK
    0x2714: "v",   # HEAVY CHECK MARK
    0x2717: "x",   # BALLOT X
    0x2718: "x",   # HEAVY BALLOT X
    # Degree / per mille
    0x00B0: "deg", # DEGREE SIGN
    0x2030: "/1000", # PER MILLE SIGN
}


def repair_content(content: str) -> Tuple[str, int]:
    """Apply the punctuation safe-list to a content string.

    Returns ``(repaired_content, num_repairs)``. Fast-paths when the
    content has no non-ASCII codepoints (the common success case) by
    skipping the translate call.

    Does NOT repair Unicode letters — those will still show up in
    :func:`scan_content` after this pass runs and will hard-fail the
    gate. This is intentional per Manifesto §6 (Iron Gate structural
    fixes): punctuation drift gets auto-healed, identifier corruption
    gets the model re-running.
    """
    if not isinstance(content, str) or not content:
        return content, 0

    # Fast path — common case where content is already clean ASCII.
    if content.isascii():
        return content, 0

    # str.translate accepts int-keyed mapping directly. Collect count
    # first so we can report telemetry without a second pass.
    repairs = 0
    for cp in _UNICODE_REPAIR_MAP:
        if chr(cp) in content:
            repairs += content.count(chr(cp))

    if repairs == 0:
        return content, 0

    return content.translate(_UNICODE_REPAIR_MAP), repairs


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
_REPAIR_COUNT = 0
_REPAIRED_CANDIDATES = 0


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


def record_repair(num_codepoints: int) -> None:
    """Increment the repair counters.

    Records both the total codepoints repaired (``num_codepoints``) and
    a "candidates with at least one repair" counter so callers can
    distinguish "one big repair" from "many small repairs".
    """
    global _REPAIR_COUNT, _REPAIRED_CANDIDATES
    if num_codepoints <= 0:
        return
    _REPAIR_COUNT += int(num_codepoints)
    _REPAIRED_CANDIDATES += 1


def get_repair_count() -> int:
    """Return the total number of codepoints repaired since process start."""
    return _REPAIR_COUNT


def get_repaired_candidate_count() -> int:
    """Return the number of distinct candidates that had at least one repair."""
    return _REPAIRED_CANDIDATES


def reset_repair_count() -> None:
    """Reset the repair counters. Primarily for tests."""
    global _REPAIR_COUNT, _REPAIRED_CANDIDATES
    _REPAIR_COUNT = 0
    _REPAIRED_CANDIDATES = 0


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
            # Fallback to raw_content when full_content is missing or
            # empty — matches the single-file shape handling below.
            if not isinstance(fc, str) or not fc:
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
        auto_repair: Optional[bool] = None,
    ) -> None:
        self.max_samples = max_samples
        # Explicit ``enabled`` / ``auto_repair`` params override env
        # vars — useful for tests that need to force-toggle without
        # monkey-patching the environment.
        self._enabled_override = enabled
        self._auto_repair_override = auto_repair

    @property
    def enabled(self) -> bool:
        if self._enabled_override is not None:
            return self._enabled_override
        return is_enabled()

    @property
    def auto_repair_enabled(self) -> bool:
        if self._auto_repair_override is not None:
            return self._auto_repair_override
        return is_auto_repair_enabled()

    def scan(self, candidate: Dict[str, Any]) -> List[BadCodepoint]:
        """Scan a candidate; empty list when gate is disabled."""
        if not self.enabled:
            return []
        return scan_candidate(candidate, max_samples=self.max_samples)

    def repair(self, candidate: Dict[str, Any]) -> int:
        """Apply the punctuation auto-repair to a candidate **in-place**.

        Walks both the multi-file (``candidate["files"]``) and
        single-file (``candidate["full_content"]`` / ``raw_content``)
        shapes, substituting each string via :func:`repair_content` and
        mutating the candidate dict in place.

        Returns
        -------
        int
            Total number of codepoints repaired across all content
            fields of the candidate. ``0`` when the candidate is
            already clean or auto-repair is disabled.

        Notes
        -----
        Does not mutate the candidate when ``auto_repair_enabled`` is
        ``False``. Does not mutate fields that aren't strings. Records
        telemetry exactly once per candidate (not once per field) so
        the "repaired candidates" counter tracks real candidates, not
        multi-file sub-entries.
        """
        if not self.auto_repair_enabled or not isinstance(candidate, dict):
            return 0

        total_repairs = 0

        # Multi-file shape: mutate each entry's full_content + raw_content.
        files_field = candidate.get("files")
        if isinstance(files_field, list) and files_field:
            for entry in files_field:
                if not isinstance(entry, dict):
                    continue
                for key in ("full_content", "raw_content"):
                    val = entry.get(key)
                    if isinstance(val, str) and val:
                        repaired, n = repair_content(val)
                        if n > 0:
                            entry[key] = repaired
                            total_repairs += n

        # Single-file shape: mutate the top-level full_content/raw_content.
        for key in ("full_content", "raw_content"):
            val = candidate.get(key)
            if isinstance(val, str) and val:
                repaired, n = repair_content(val)
                if n > 0:
                    candidate[key] = repaired
                    total_repairs += n

        if total_repairs > 0:
            record_repair(total_repairs)

        return total_repairs

    def check(
        self, candidate: Dict[str, Any],
    ) -> Tuple[bool, Optional[str], List[BadCodepoint]]:
        """Run the gate and return ``(ok, reason, samples)``.

        ``ok=True`` means the candidate passed (no offenders or gate
        disabled). ``ok=False`` means the candidate must be rejected;
        ``reason`` is the pre-formatted error string for raising as
        ``RuntimeError`` and ``samples`` is the offender list for
        telemetry / retry feedback.

        When auto-repair is enabled, punctuation offenders are healed
        in-place **before** the hard-reject scan. Only Unicode letters
        (and other unlisted codepoints) survive the repair pass and
        trigger rejection. The candidate dict is mutated in place with
        repaired content so the orchestrator can feed it straight into
        APPLY without a second handoff.

        The candidate may be annotated with ``_ascii_repair_count`` for
        observability so the orchestrator can log the repair delta.
        """
        if not self.enabled:
            return True, None, []

        # Phase 1 — punctuation auto-repair (mutating). Does nothing
        # when JARVIS_ASCII_GATE_AUTO_REPAIR=false.
        repairs = self.repair(candidate)
        if repairs > 0 and isinstance(candidate, dict):
            # Annotate for orchestrator logging. Non-load-bearing — any
            # downstream consumer that doesn't know about this key just
            # ignores it.
            candidate["_ascii_repair_count"] = repairs

        # Phase 2 — hard-reject scan over what's left. Letters + any
        # codepoint not in the safe-list will show up here.
        offenders = scan_candidate(candidate, max_samples=self.max_samples)
        if not offenders:
            return True, None, []
        record_rejection(len(offenders))
        return False, format_rejection_reason(offenders), offenders
