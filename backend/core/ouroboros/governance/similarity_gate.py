"""Ouroboros GATE diff-aware similarity check.

Detects when added code in a candidate patch has high n-gram overlap
with existing source, suggesting copy-paste with minimal modification.
Escalates to APPROVAL_REQUIRED, does not hard-block.

This is a deterministic gate -- no LLM calls, pure text computation.
"""
import io
import os
import tokenize as _tokenize
from difflib import SequenceMatcher
from typing import List, Optional, Set

_SIMILARITY_THRESHOLD = float(os.environ.get("JARVIS_GATE_SIMILARITY_THRESHOLD", "0.7"))
_NGRAM_SIZE = 3
_MIN_ADDED_LINES = 3


def check_similarity(
    candidate_content: str,
    source_content: str,
    threshold: Optional[float] = None,
) -> Optional[str]:
    """Check if added code in candidate has high overlap with source.

    Returns reason string if similarity too high, None if acceptable.
    """
    thresh = threshold if threshold is not None else _SIMILARITY_THRESHOLD

    source_lines = _normalize_lines(source_content)
    candidate_lines = _normalize_lines(candidate_content)

    added_lines = _extract_added_lines(source_lines, candidate_lines)

    if len(added_lines) < _MIN_ADDED_LINES:
        return None

    added_ngrams = _build_ngrams(added_lines, _NGRAM_SIZE)
    source_ngrams = _build_ngrams(source_lines, _NGRAM_SIZE)

    if not added_ngrams:
        return None

    overlap = len(added_ngrams & source_ngrams) / len(added_ngrams)

    if overlap > thresh:
        return (
            f"High similarity between added code and existing source "
            f"(overlap: {overlap:.2f}, threshold: {thresh:.2f})"
        )
    return None


def _normalize_lines(content: str) -> List[str]:
    """Normalize content: strip comments, whitespace, blank lines."""
    lines = []
    for line in content.splitlines():
        normalized = _strip_comment(line).strip()
        if normalized:
            lines.append(normalized)
    return lines


def _strip_comment(line: str) -> str:
    """Strip inline Python comments."""
    try:
        tokens = list(_tokenize.generate_tokens(io.StringIO(line + "\n").readline))
        result_parts = []
        for tok in tokens:
            if tok.type == _tokenize.COMMENT:
                break
            if tok.type not in (_tokenize.NEWLINE, _tokenize.NL, _tokenize.ENDMARKER):
                result_parts.append(tok.string)
        return " ".join(result_parts)
    except _tokenize.TokenError:
        return line.split("#")[0]


def _extract_added_lines(source_lines: List[str], candidate_lines: List[str]) -> List[str]:
    """Extract lines present in candidate but not in source."""
    sm = SequenceMatcher(None, source_lines, candidate_lines)
    added: List[str] = []
    for op, _, _, j1, j2 in sm.get_opcodes():
        if op in ("insert", "replace"):
            added.extend(candidate_lines[j1:j2])
    return added


def _build_ngrams(lines: List[str], n: int) -> Set[tuple]:
    """Build a set of n-grams from a list of normalized lines."""
    if len(lines) < n:
        return set()
    return {tuple(lines[i:i + n]) for i in range(len(lines) - n + 1)}
