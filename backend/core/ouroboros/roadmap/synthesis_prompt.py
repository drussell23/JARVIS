"""
Synthesis Prompt Builder & Context Shedding
============================================

Builds the structured prompt fed to the Doubleword 397B model (or any
compatible LLM) for Feature Synthesis Engine runs.

Responsibilities
----------------
- Assemble P0 fragment summaries, Tier 0 hints, and an oracle summary into a
  single coherent prompt that instructs the model to return a JSON object
  matching :data:`SYNTHESIS_JSON_SCHEMA`.
- Enforce a token budget via :func:`shed_context` so callers never send an
  over-sized payload to the model.

Design principles
-----------------
- Zero hardcoding: prompt structure is driven entirely by the types it
  receives; callers own content.
- Conservative token estimate: ``_CHARS_PER_TOKEN = 4`` keeps us safely under
  any model's context window even for Unicode-heavy text.
- Fail loudly: :func:`shed_context` raises :class:`ContextBudgetExceededError`
  when truncation alone cannot bring the text within budget, so callers can
  take appropriate action (e.g. skip a synthesis run or reduce inputs).
"""

from __future__ import annotations

import json
import textwrap
from typing import List

from backend.core.ouroboros.roadmap.hypothesis import FeatureHypothesis
from backend.core.ouroboros.roadmap.snapshot import RoadmapSnapshot


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Conservative characters-per-token estimate used by :func:`shed_context`.
_CHARS_PER_TOKEN: int = 4


# ---------------------------------------------------------------------------
# JSON output schema
# ---------------------------------------------------------------------------

SYNTHESIS_JSON_SCHEMA: dict = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "FeatureSynthesisOutput",
    "description": (
        "Structured output from the Feature Synthesis Engine. "
        "Contains an array of gap hypotheses inferred from the roadmap snapshot."
    ),
    "type": "object",
    "required": ["gaps"],
    "properties": {
        "gaps": {
            "type": "array",
            "description": "Array of identified gaps or opportunities in the codebase.",
            "items": {
                "type": "object",
                "required": [
                    "description",
                    "evidence_fragments",
                    "gap_type",
                    "confidence",
                    "urgency",
                    "suggested_scope",
                    "suggested_repos",
                ],
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "Human-readable description of the gap or opportunity.",
                    },
                    "evidence_fragments": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "List of source_id values from snapshot fragments "
                            "that support this hypothesis."
                        ),
                    },
                    "gap_type": {
                        "type": "string",
                        "enum": [
                            "missing_capability",
                            "incomplete_wiring",
                            "stale_implementation",
                            "manifesto_violation",
                        ],
                        "description": "Category of the gap.",
                    },
                    "confidence": {
                        "type": "number",
                        "minimum": 0.0,
                        "maximum": 1.0,
                        "description": "Confidence score in [0, 1].",
                    },
                    "urgency": {
                        "type": "string",
                        "description": "Qualitative urgency: critical, high, medium, or low.",
                    },
                    "suggested_scope": {
                        "type": "string",
                        "description": (
                            "Short label for the change type, e.g. "
                            "new-agent, wire-existing, refactor."
                        ),
                    },
                    "suggested_repos": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Repository names that should be touched.",
                    },
                },
            },
        }
    },
}


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------

class ContextBudgetExceededError(Exception):
    """Raised when :func:`shed_context` cannot meet the requested token budget.

    This happens when ``max_tokens`` is so small that even an empty string
    would exceed the budget, i.e. ``max_tokens < 1``.
    """


# ---------------------------------------------------------------------------
# Context shedding
# ---------------------------------------------------------------------------

def shed_context(text: str, max_tokens: int) -> str:
    """Truncate *text* to fit within *max_tokens* tokens.

    Token count is estimated as ``len(text) / _CHARS_PER_TOKEN`` (ceiling).

    Parameters
    ----------
    text:
        The text to potentially truncate.
    max_tokens:
        Maximum number of tokens allowed.  Must be >= 1.

    Returns
    -------
    str
        The original string if it already fits, otherwise a truncated copy
        with a ``" …[truncated]"`` suffix appended.

    Raises
    ------
    ContextBudgetExceededError
        When ``max_tokens < 1``, i.e. the budget is too small to hold even
        a single character.
    """
    if max_tokens < 1:
        raise ContextBudgetExceededError(
            f"max_tokens={max_tokens!r} is too small to hold any content "
            f"(minimum is 1 token = {_CHARS_PER_TOKEN} chars)"
        )

    max_chars = max_tokens * _CHARS_PER_TOKEN

    if len(text) <= max_chars:
        return text

    # Reserve space for the truncation marker.
    suffix = " …[truncated]"
    available = max_chars - len(suffix)
    if available <= 0:
        # Budget is barely enough for the suffix — return what we can.
        return (suffix[:max_chars]).lstrip()

    return text[:available] + suffix


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def build_synthesis_prompt(
    snapshot: RoadmapSnapshot,
    tier0_hints: List[FeatureHypothesis],
    oracle_summary: str,
) -> str:
    """Build the synthesis prompt from snapshot P0 fragments, Tier 0 hints, and
    an oracle code-graph summary.

    Only fragments at tier 0 (authoritative design specs) are included in the
    prompt body.  Higher-tier fragments are excluded to stay within token
    budgets and to keep the model focused on authoritative sources.

    Parameters
    ----------
    snapshot:
        Current roadmap snapshot.  P0 (tier=0) fragments are extracted and
        rendered as numbered sections.
    tier0_hints:
        Pre-computed deterministic hints from Tier 0 analysis.  Included as
        structured context so the model can corroborate or extend them.
    oracle_summary:
        Free-text summary from TheOracle (code-graph neighbourhood).

    Returns
    -------
    str
        The fully assembled prompt string ready to be sent to the model.
    """
    # --- Extract P0 fragments ---
    p0_fragments = [f for f in snapshot.fragments if f.tier == 0]

    # --- Build fragment block ---
    fragment_lines: list[str] = []
    for idx, frag in enumerate(p0_fragments, start=1):
        fragment_lines.append(
            f"[{idx}] source_id={frag.source_id!r}  type={frag.fragment_type!r}\n"
            f"    Title: {frag.title}\n"
            f"    Summary: {frag.summary}"
        )
    fragment_block = "\n\n".join(fragment_lines) if fragment_lines else "(no P0 fragments)"

    # --- Build tier0 hints block ---
    hint_lines: list[str] = []
    for hint in tier0_hints:
        evidence_str = ", ".join(hint.evidence_fragments) if hint.evidence_fragments else "none"
        hint_lines.append(
            f"- [{hint.gap_type}] {hint.description}\n"
            f"  evidence: {evidence_str}\n"
            f"  confidence: {hint.confidence:.2f}  urgency: {hint.urgency}"
        )
    hint_block = "\n".join(hint_lines) if hint_lines else "(no tier0 hints)"

    # --- Build schema block (compact JSON) ---
    schema_block = json.dumps(SYNTHESIS_JSON_SCHEMA, indent=2)

    # --- Assemble prompt ---
    prompt = textwrap.dedent(f"""\
        You are the Feature Synthesis Engine for the JARVIS AI Agent.

        Your task is to analyse the roadmap source material below and identify
        gaps, missing capabilities, incomplete wiring, stale implementations,
        and manifesto violations in the codebase.

        Return your findings as a single JSON object that conforms to the
        schema provided at the end of this prompt.  Do not include any text
        outside the JSON object.

        ════════════════════════════════════════════════════════════════════
        SNAPSHOT METADATA
        ════════════════════════════════════════════════════════════════════
        snapshot_version : {snapshot.version}
        content_hash     : {snapshot.content_hash}
        p0_fragment_count: {len(p0_fragments)}

        ════════════════════════════════════════════════════════════════════
        P0 SOURCE FRAGMENTS (authoritative specs & design docs)
        ════════════════════════════════════════════════════════════════════
        {fragment_block}

        ════════════════════════════════════════════════════════════════════
        TIER 0 DETERMINISTIC HINTS (pre-computed, use as corroboration)
        ════════════════════════════════════════════════════════════════════
        {hint_block}

        ════════════════════════════════════════════════════════════════════
        ORACLE CODE-GRAPH SUMMARY
        ════════════════════════════════════════════════════════════════════
        {oracle_summary}

        ════════════════════════════════════════════════════════════════════
        OUTPUT JSON SCHEMA
        ════════════════════════════════════════════════════════════════════
        {schema_block}
    """)

    return prompt
