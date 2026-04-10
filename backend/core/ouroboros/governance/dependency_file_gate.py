"""Iron Gate 3 — Dependency file integrity.

Deterministic check that catches model hallucinations / truncations of
package names in dependency files. Engineered in response to battle test
bt-2026-04-10-184157, where Claude produced a valid-looking requirements.txt
patch that renamed ``anthropic`` → ``anthropichttp`` and ``rapidfuzz`` →
``rapidfu`` — two pure-ASCII corruptions that slipped past the ASCII strict
gate, the validator, and the (crashed) similarity gate, landing in APPLY.

Core invariant: an autonomous edit to a dependency file MUST NOT delete
an existing package name and replace it with a near-identical one. Such
pairs (Levenshtein distance ≤ 3, or prefix/substring relation on the
package name) are almost certainly hallucinated or truncated names, not
real upgrades.

This gate does NOT reach out to PyPI — it is pure deterministic text
analysis, runs in O(n²) on the SYMMETRIC difference of package names
(typically tiny), and returns in microseconds.

Currently supports:
    - ``requirements.txt`` / ``*-requirements.txt`` / ``requirements-*.txt``

Not yet supported (TODO): package.json, Cargo.toml, pyproject.toml, go.mod.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# Python package names per PEP 508: letters, digits, ., -, _ (case-insensitive,
# hyphen/underscore/dot equivalent). We extract the raw name token up to the
# first space / [ / ; / < / > / = / ~ / !.
_REQ_LINE_RE = re.compile(
    r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*"
    r"(?P<rest>.*)$"
)

_MAX_RENAME_LEVENSHTEIN = int(os.environ.get("JARVIS_DEP_GATE_MAX_RENAME_EDIT", "3"))
_GATE_ENABLED = os.environ.get("JARVIS_DEP_FILE_GATE_ENABLED", "true").lower() != "false"


def is_dependency_file(file_path: str) -> bool:
    """Return True iff the path names a supported dependency file."""
    if not file_path:
        return False
    base = file_path.rsplit("/", 1)[-1].lower()
    if base == "requirements.txt":
        return True
    if base.endswith("-requirements.txt") or base.startswith("requirements-"):
        return True
    return False


def _parse_requirements(content: str) -> Dict[str, str]:
    """Parse requirements.txt content into ``{normalized_name: raw_line}``.

    Normalization: lowercase, collapse ``-``/``_``/``.`` to ``-`` (per PEP 503
    name canonicalization). Blank and comment-only lines are ignored.
    """
    out: Dict[str, str] = {}
    for raw in content.splitlines():
        line = raw.rstrip("\r\n")
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("-"):
            # Skip empty / pure-comment lines and -r/-e/--option directives.
            continue
        # Trim trailing comment
        code = stripped.split("#", 1)[0].strip()
        if not code:
            continue
        m = _REQ_LINE_RE.match(code)
        if not m:
            continue
        name = m.group(1)
        canonical = _canonical_name(name)
        if canonical and canonical not in out:
            out[canonical] = line
    return out


def _canonical_name(name: str) -> str:
    """PEP 503 name normalization."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _levenshtein(a: str, b: str) -> int:
    """Classic edit distance. Only called on the symmetric difference of
    package-name sets, which is typically empty or single-digit in size."""
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i]
        for j, cb in enumerate(b, 1):
            ins = curr[j - 1] + 1
            dele = prev[j] + 1
            sub = prev[j - 1] + (0 if ca == cb else 1)
            curr.append(min(ins, dele, sub))
        prev = curr
    return prev[-1]


def _suspicious_rename(removed: str, added: str) -> bool:
    """True iff ``removed`` → ``added`` looks like an unintended edit.

    Triggers on:
      - Small edit distance (≤ _MAX_RENAME_LEVENSHTEIN)
      - One is a strict prefix of the other (truncation or suffix-append)
      - Substring containment with length difference < 5
    """
    if not removed or not added:
        return False
    # Prefix match — truncation like ``rapidfuzz`` → ``rapidfu``
    if added.startswith(removed) or removed.startswith(added):
        return True
    # Substring containment (e.g. ``anthropic`` inside ``anthropichttp``)
    if (added in removed or removed in added) and abs(len(added) - len(removed)) < 5:
        return True
    # Edit distance ≤ threshold
    if _levenshtein(added, removed) <= _MAX_RENAME_LEVENSHTEIN:
        return True
    return False


def check_requirements_integrity(
    candidate_content: str,
    source_content: str,
) -> Optional[Tuple[str, List[str]]]:
    """Check that a candidate requirements.txt preserves existing package names.

    Returns:
        None if the candidate is safe.
        ``(reason, offender_descriptions)`` otherwise, where ``offender_descriptions``
        is a list of ``"removed_name -> added_name"`` strings the orchestrator's
        retry feedback builder can surface to the model.
    """
    if not _GATE_ENABLED:
        return None
    if not candidate_content or not source_content:
        return None

    old = _parse_requirements(source_content)
    new = _parse_requirements(candidate_content)
    if not old or not new:
        return None

    old_names = set(old.keys())
    new_names = set(new.keys())

    removed = old_names - new_names
    added = new_names - old_names

    if not removed:
        # Pure additions — allowed. (Deletions require explicit intent but
        # that's a separate concern; we focus on HALLUCINATED renames here.)
        return None

    offenders: List[str] = []
    for rem in sorted(removed):
        for add in sorted(added):
            if _suspicious_rename(rem, add):
                offenders.append(f"{rem} -> {add}")
                break  # one match per removed entry is enough to flag

    if not offenders:
        return None

    reason = (
        f"Dependency file rename/truncation suspected: {len(offenders)} "
        f"package(s) deleted and replaced with near-identical name(s). "
        f"These look like model hallucinations or typos, not legitimate "
        f"upgrades. Offenders: {', '.join(offenders[:5])}"
        f"{'…' if len(offenders) > 5 else ''}"
    )
    return reason, offenders


def check_candidate(
    candidate: Dict[str, Any],
    repo_root: Any,
) -> Optional[Tuple[str, List[str]]]:
    """Orchestrator entry point — walks a candidate dict and dispatches per file.

    Handles both the legacy single-file shape (``full_content`` + ``file_path``)
    and the multi-file shape (``files: [{file_path, full_content}, ...]``).
    Returns the FIRST offense found, or None.
    """
    if not _GATE_ENABLED or not isinstance(candidate, dict):
        return None

    try:
        from pathlib import Path
        _repo = Path(repo_root) if not isinstance(repo_root, type(Path(""))) else repo_root
    except Exception:
        return None

    def _check_pair(file_path: str, cand_content: str) -> Optional[Tuple[str, List[str]]]:
        if not is_dependency_file(file_path):
            return None
        src_path = _repo / file_path
        if not src_path.exists():
            # New file — no baseline to compare against, safe.
            return None
        try:
            src_content = src_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        return check_requirements_integrity(cand_content, src_content)

    # Legacy single-file
    fp = candidate.get("file_path", "")
    fc = candidate.get("full_content", "") or ""
    if fp and fc:
        res = _check_pair(fp, fc)
        if res is not None:
            return res

    # Multi-file
    files = candidate.get("files")
    if isinstance(files, list):
        for entry in files:
            if not isinstance(entry, dict):
                continue
            res = _check_pair(
                entry.get("file_path", "") or "",
                entry.get("full_content", "") or "",
            )
            if res is not None:
                return res

    return None
