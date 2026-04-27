"""Phase 7.1 — SemanticGuardian adapted-pattern boot-time loader.

Per `OUROBOROS_VENOM_PRD.md` §3.6 (brutal review) + §9 Phase 7.1:

  > Pass C Slice 2 mines patterns; Slice 6 REPL approves them. But
  > `semantic_guardian.py` doesn't read `.jarvis/adapted_guardian_
  > patterns.yaml` at boot. Without that, Pass C is theater —
  > `/adapt approve` writes APPROVED to the ledger but NOTHING in
  > the actual cage changes. P7.1 closes that gap for the highest-
  > impact surface (SemanticGuardian).

This module is the **bridge** between Pass C's AdaptationLedger
(operator-approved adaptation proposals) and the live SemanticGuardian
detector registry. Default-off + best-effort: when the env flag is
off OR the YAML is missing/malformed, the loader returns an empty
dict and SemanticGuardian behaves exactly as it did pre-Phase-7.1.

## Design constraints (load-bearing)

  * **Adapted patterns are ADDITIVE, never substitutive** (per Pass C
    §6.3). The loader merges into `_PATTERNS` via assignment of new
    keys; existing hand-written keys are NEVER overwritten. A
    pattern name collision with a hand-written pattern causes the
    adapted entry to be SKIPPED (with a logged warning), never the
    other way around.
  * **YAML schema is the operator-approved source of truth.** The
    file is written by Slice 6's `/adapt approve` flow (in a future
    sub-slice); this module READS it. We do not write here.
  * **Per-pattern source attribution** (`source="hand_written"` vs
    `source="adapted:<proposal_id>"`) is exposed via a parallel
    registry so `/cognitive`, `/adapt`, and tests can answer "where
    did this detector come from?"
  * **Stdlib + adaptation.ledger import surface only.** Same
    cage discipline as the rest of `adaptation/`. Does NOT import
    semantic_guardian.py (one-way dependency: semantic_guardian
    imports THIS module, not the reverse).
  * **Fail-open**: every error path returns an empty dict. The
    SemanticGuardian behaves identically to today on any failure.

## Default-off

`JARVIS_SEMANTIC_GUARDIAN_LOAD_ADAPTED_PATTERNS` (default false).
When off, the loader is a no-op.

## YAML schema

The file `.jarvis/adapted_guardian_patterns.yaml` is the canonical
storage:

```yaml
schema_version: 1
patterns:
  - name: adapted_critical_xyz_pattern_a1b2c3
    regex: "CRITICAL_PATTERN_XYZ"
    severity: "soft"        # "soft" or "hard" (default soft)
    message: "Adapted from POSTMORTEM mining"
    proposal_id: "adapt-sg-..."
    approved_at: "2026-..."
    approved_by: "alice"
```

Each entry:
  * `name`: unique pattern identifier (collision with hand-written
    causes SKIP).
  * `regex`: the synthesized longest-common-substring pattern from
    Slice 2's miner.
  * `severity`: optional, default "soft".
  * `message`: optional, default a generic adapted-pattern message.
  * `proposal_id` + `approved_at` + `approved_by`: provenance.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


_TRUTHY = ("1", "true", "yes", "on")


# Soft cap on the number of adapted patterns the loader will merge.
# Defends against a corrupt YAML file with thousands of entries
# slowing every SemanticGuardian inspection.
MAX_ADAPTED_PATTERNS: int = 256

# Hard cap on the regex length per adapted pattern. Mirrors Slice 2's
# MAX_SYNTHESIZED_PATTERN_CHARS so we never load a pattern stricter
# than what the miner can produce.
MAX_ADAPTED_REGEX_CHARS: int = 256

# Bound the rendered detection message.
MAX_ADAPTED_MESSAGE_CHARS: int = 240

# Bound the YAML file size we'll attempt to load. 4 MiB is generous;
# anything larger is treated as a malformed file.
MAX_YAML_BYTES: int = 4 * 1024 * 1024


def is_loader_enabled() -> bool:
    """Master flag — ``JARVIS_SEMANTIC_GUARDIAN_LOAD_ADAPTED_PATTERNS``
    (default false until Phase 7.1 graduation).

    When off, :func:`load_adapted_patterns` returns an empty dict
    without reading the YAML. SemanticGuardian behaves identically
    to pre-Phase-7.1."""
    return os.environ.get(
        "JARVIS_SEMANTIC_GUARDIAN_LOAD_ADAPTED_PATTERNS", "",
    ).strip().lower() in _TRUTHY


def adapted_patterns_path() -> Path:
    """Return the YAML path. Env-overridable via
    ``JARVIS_ADAPTED_GUARDIAN_PATTERNS_PATH``; defaults to
    ``.jarvis/adapted_guardian_patterns.yaml`` under cwd."""
    raw = os.environ.get("JARVIS_ADAPTED_GUARDIAN_PATTERNS_PATH")
    if raw:
        return Path(raw)
    return Path(".jarvis") / "adapted_guardian_patterns.yaml"


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdaptedPatternEntry:
    """One adapted-pattern record loaded from YAML. Frozen for
    audit-trail integrity."""

    name: str
    regex: str
    severity: str
    message: str
    proposal_id: str
    approved_at: str
    approved_by: str


# ---------------------------------------------------------------------------
# Detector function builder
# ---------------------------------------------------------------------------


def _build_detector(
    entry: AdaptedPatternEntry,
) -> Callable[..., Optional[Any]]:
    """Build a closure that matches the SemanticGuardian detector
    signature: ``(file_path, old_content, new_content) -> Optional
    [Detection]``.

    Strategy: compile the regex once at load time; on each inspect
    call, run `re.search` against `new_content` AND check it's NEW
    (not present in `old_content`) — same diff-aware discipline the
    hand-written detectors use.
    """
    # Defensive compilation: if the regex doesn't compile, the
    # builder returns a no-op detector so SemanticGuardian doesn't
    # crash mid-inspect.
    try:
        compiled = re.compile(entry.regex, flags=re.MULTILINE)
    except re.error as exc:
        logger.warning(
            "[AdaptedGuardianLoader] pattern %s regex compile failed: "
            "%s — detector will return None for all inputs",
            entry.name, exc,
        )
        compiled = None

    # Lazy-import Detection from semantic_guardian when the detector
    # actually fires. This keeps the loader's static import surface
    # stdlib-only AND avoids the circular dependency
    # (semantic_guardian imports THIS module).
    name = entry.name
    severity = entry.severity
    message = entry.message
    proposal_id = entry.proposal_id

    def detector(
        *,
        file_path: str = "",
        old_content: str = "",
        new_content: str = "",
    ) -> Optional[Any]:
        if compiled is None:
            return None
        m = compiled.search(new_content or "")
        if m is None:
            return None
        # Diff-aware: only fire if the match is NEW in this op (not
        # already present in old_content). Same discipline as the
        # hand-written detectors.
        if compiled.search(old_content or ""):
            return None
        try:
            from backend.core.ouroboros.governance.semantic_guardian import (
                Detection,
            )
        except Exception:  # noqa: BLE001
            return None
        # Compute the line number of the match for operator visibility.
        line_no = (new_content or "").count("\n", 0, m.start()) + 1
        snippet = m.group(0)[:200]
        return Detection(
            pattern=name,
            severity=severity,
            message=f"{message} [adapted from proposal {proposal_id}]",
            file_path=file_path,
            lines=(line_no,),
            snippet=snippet,
        )

    return detector


# ---------------------------------------------------------------------------
# YAML reader
# ---------------------------------------------------------------------------


def _parse_entry(
    raw: Dict[str, Any], idx: int,
) -> Optional[AdaptedPatternEntry]:
    """Parse one YAML entry. Returns None on missing required fields."""
    name = str(raw.get("name") or "").strip()
    if not name:
        logger.debug(
            "[AdaptedGuardianLoader] entry %d missing name — skip", idx,
        )
        return None
    regex = str(raw.get("regex") or "")[:MAX_ADAPTED_REGEX_CHARS]
    if not regex:
        logger.debug(
            "[AdaptedGuardianLoader] entry %d (name=%s) missing regex — skip",
            idx, name,
        )
        return None
    severity = str(raw.get("severity") or "soft").strip().lower()
    if severity not in ("soft", "hard"):
        severity = "soft"
    message = str(raw.get("message") or "Adapted SemanticGuardian pattern")
    message = message[:MAX_ADAPTED_MESSAGE_CHARS]
    proposal_id = str(raw.get("proposal_id") or "")
    approved_at = str(raw.get("approved_at") or "")
    approved_by = str(raw.get("approved_by") or "")
    return AdaptedPatternEntry(
        name=name, regex=regex, severity=severity, message=message,
        proposal_id=proposal_id, approved_at=approved_at,
        approved_by=approved_by,
    )


def load_adapted_patterns(
    yaml_path: Optional[Path] = None,
    *,
    hand_written_names: Tuple[str, ...] = (),
) -> Dict[str, Callable[..., Optional[Any]]]:
    """Read the adapted-pattern YAML and return a `{name: detector}`
    dict ready to merge into SemanticGuardian's `_PATTERNS`.

    Returns empty dict when:
      * Master flag off
      * YAML file missing
      * YAML parse fails (PyYAML import OR `yaml.safe_load`)
      * File exceeds MAX_YAML_BYTES
      * Top-level not a mapping or `patterns` key missing/non-list

    Per-entry SKIP (logged at debug) when:
      * Missing required field (name, regex)
      * Regex doesn't compile (detector still returned but no-ops)
      * Name collides with a hand-written pattern (cage rule:
        adapted patterns are additive, never substitutive)

    NEVER raises into the caller — all failure paths return empty
    dict so SemanticGuardian behaves identically to pre-Phase-7.1
    on any failure.
    """
    if not is_loader_enabled():
        return {}

    path = yaml_path if yaml_path is not None else adapted_patterns_path()
    if not path.exists():
        logger.debug(
            "[AdaptedGuardianLoader] no adapted-patterns yaml at %s — "
            "no patterns to merge", path,
        )
        return {}
    try:
        size = path.stat().st_size
    except OSError as exc:
        logger.warning(
            "[AdaptedGuardianLoader] stat failed for %s: %s", path, exc,
        )
        return {}
    if size > MAX_YAML_BYTES:
        logger.warning(
            "[AdaptedGuardianLoader] %s exceeds MAX_YAML_BYTES=%d "
            "(was %d) — refusing to load",
            path, MAX_YAML_BYTES, size,
        )
        return {}
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning(
            "[AdaptedGuardianLoader] read failed for %s: %s", path, exc,
        )
        return {}
    if not raw_text.strip():
        return {}
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        logger.warning(
            "[AdaptedGuardianLoader] PyYAML not available — cannot "
            "load adapted patterns",
        )
        return {}
    try:
        doc = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        logger.warning(
            "[AdaptedGuardianLoader] YAML parse failed at %s: %s",
            path, exc,
        )
        return {}
    if not isinstance(doc, dict):
        logger.warning(
            "[AdaptedGuardianLoader] %s top-level is not a mapping — skip",
            path,
        )
        return {}
    raw_entries = doc.get("patterns")
    if not isinstance(raw_entries, list):
        return {}

    out: Dict[str, Callable[..., Optional[Any]]] = {}
    hand_written_set = set(hand_written_names)
    seen_names: set = set()
    skipped_collisions: List[str] = []

    for i, raw_entry in enumerate(raw_entries):
        if len(out) >= MAX_ADAPTED_PATTERNS:
            logger.warning(
                "[AdaptedGuardianLoader] reached MAX_ADAPTED_PATTERNS="
                "%d — truncating remaining entries",
                MAX_ADAPTED_PATTERNS,
            )
            break
        if not isinstance(raw_entry, dict):
            continue
        entry = _parse_entry(raw_entry, i)
        if entry is None:
            continue
        # Cage rule: adapted patterns are ADDITIVE, never
        # substitutive (Pass C §6.3). Collision with hand-written
        # → SKIP the adapted entry.
        if entry.name in hand_written_set:
            skipped_collisions.append(entry.name)
            logger.warning(
                "[AdaptedGuardianLoader] adapted pattern name=%s "
                "collides with hand-written detector — SKIP (cage rule: "
                "adapted patterns are additive, never substitutive)",
                entry.name,
            )
            continue
        if entry.name in seen_names:
            # Duplicate within the YAML itself; first occurrence wins
            logger.debug(
                "[AdaptedGuardianLoader] duplicate adapted name=%s in "
                "yaml — keeping first", entry.name,
            )
            continue
        seen_names.add(entry.name)
        out[entry.name] = _build_detector(entry)

    if out:
        logger.info(
            "[AdaptedGuardianLoader] loaded %d adapted SemanticGuardian "
            "patterns from %s (%d collisions skipped)",
            len(out), path, len(skipped_collisions),
        )
    return out


__all__ = [
    "AdaptedPatternEntry",
    "MAX_ADAPTED_MESSAGE_CHARS",
    "MAX_ADAPTED_PATTERNS",
    "MAX_ADAPTED_REGEX_CHARS",
    "MAX_YAML_BYTES",
    "adapted_patterns_path",
    "is_loader_enabled",
    "load_adapted_patterns",
]
