"""
SemanticTriageEngine — DW-powered pre-generation file intelligence.

Slot: GLS.submit() → after ContextMemoryLoader, before preflight check.

Uses Doubleword's cheap 35B model (via prompt_only()) to semantically
analyze target files BEFORE committing to the expensive generation pipeline.

Decisions:
  - NO_OP:     Change already present or file doesn't need modification → early exit
  - REDIRECT:  The real problem is in a different file → update target_files
  - ENRICH:    Triage found actionable insights → inject into context
  - PROCEED:   No special findings → let the pipeline run normally
  - SKIP:      Triage unavailable/failed → proceed without triage (graceful degradation)

Cost model:
  - Uses 35B model for triage (~fraction of 397B cost)
  - 397B reserved for actual generation (heavy lifting)
  - Integrates with existing DW cost tracking (daily budget, per-op limits)
  - Prompt caching: stable system prefix maximizes cache hits

Manifesto alignment:
  - Deterministic: triage decision parsing, cost gating, timeout enforcement
  - Agentic: semantic analysis of file content via LLM inference
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger("Ouroboros.SemanticTriage")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Model for triage — cheap 35B instead of expensive 397B
_TRIAGE_MODEL = os.environ.get(
    "OUROBOROS_TRIAGE_MODEL", "Qwen/Qwen3.5-35B-A3B-FP8"
)
# Maximum tokens for triage response (keep it tight — triage, not generation)
_TRIAGE_MAX_TOKENS = int(os.environ.get("OUROBOROS_TRIAGE_MAX_TOKENS", "1500"))
# Timeout for the entire triage call (seconds)
_TRIAGE_TIMEOUT_S = float(os.environ.get("OUROBOROS_TRIAGE_TIMEOUT_S", "60"))
# Maximum file size to include in triage prompt (chars)
_TRIAGE_MAX_FILE_CHARS = int(os.environ.get("OUROBOROS_TRIAGE_MAX_FILE_CHARS", "12000"))
# Maximum number of target files to triage per operation
_TRIAGE_MAX_FILES = int(os.environ.get("OUROBOROS_TRIAGE_MAX_FILES", "3"))
# Enable/disable triage (master switch)
_TRIAGE_ENABLED = os.environ.get("OUROBOROS_TRIAGE_ENABLED", "true").lower() == "true"


# ---------------------------------------------------------------------------
# Triage decision types
# ---------------------------------------------------------------------------

class TriageDecision(Enum):
    NO_OP = auto()       # Change already present — skip generation entirely
    REDIRECT = auto()    # Wrong file — real problem is elsewhere
    ENRICH = auto()      # Insights found — inject into generation context
    PROCEED = auto()     # No special findings — run pipeline normally
    SKIP = auto()        # Triage failed/unavailable — proceed without triage


@dataclass
class TriageResult:
    """Result of semantic triage analysis."""
    decision: TriageDecision
    confidence: float = 0.0          # 0.0–1.0 confidence in the decision
    insights: str = ""               # Human-readable analysis for context injection
    redirect_files: List[str] = field(default_factory=list)  # For REDIRECT decisions
    no_op_reason: str = ""           # Why it's a no-op
    triage_duration_s: float = 0.0   # Wall-clock time for triage
    triage_model: str = ""           # Which model was used
    triage_cost_usd: float = 0.0     # Estimated cost of triage call
    raw_response: str = ""           # Full LLM response for debugging


# ---------------------------------------------------------------------------
# System prompt (stable prefix — maximizes prompt cache hits)
# ---------------------------------------------------------------------------

_TRIAGE_SYSTEM_PROMPT = """\
You are a semantic code triage engine for the JARVIS Trinity AI ecosystem.
Your job is to quickly analyze Python source files and determine:
1. Whether a proposed change is already present (NO_OP)
2. Whether the real problem is in a different file (REDIRECT)
3. What specific issues exist and how they should be addressed (ENRICH)
4. Whether the file looks fine and no change is needed (PROCEED)

You must respond with ONLY a valid JSON object (no markdown, no explanation outside JSON):
{
  "decision": "NO_OP" | "REDIRECT" | "ENRICH" | "PROCEED",
  "confidence": 0.0-1.0,
  "reasoning": "Brief explanation of your analysis",
  "issues_found": [
    {
      "type": "complexity|duplication|coupling|debt|bug|design",
      "location": "function/class name or line range",
      "severity": "low|medium|high|critical",
      "description": "What's wrong and why"
    }
  ],
  "redirect_files": ["path/to/real/target.py"],
  "suggested_approach": "How the generation model should tackle this"
}

Analysis priorities (in order):
1. Is the described change already implemented? (→ NO_OP)
2. Is this file the right target, or is the root cause elsewhere? (→ REDIRECT)
3. What are the MOST IMPACTFUL improvements possible? (→ ENRICH)
4. Are there hidden issues the static analyzer couldn't detect? (→ ENRICH)
   - Broken error handling, missing edge cases, race conditions
   - API misuse, deprecated patterns, security concerns
   - Dead code, unreachable branches, misleading names
"""


# ---------------------------------------------------------------------------
# SemanticTriageEngine
# ---------------------------------------------------------------------------

class SemanticTriageEngine:
    """Pre-generation semantic analysis using Doubleword's cheap 35B model.

    Usage::

        engine = SemanticTriageEngine(
            dw_provider=doubleword_provider,
            project_root=Path("/path/to/repo"),
        )
        result = await engine.triage(op_context)

        if result.decision == TriageDecision.NO_OP:
            # Skip generation entirely
        elif result.decision == TriageDecision.ENRICH:
            # Inject result.insights into generation context
    """

    def __init__(
        self,
        dw_provider: Any,
        project_root: Path,
    ) -> None:
        self._dw = dw_provider
        self._project_root = project_root
        # Stats
        self._total_triages: int = 0
        self._no_ops_caught: int = 0
        self._redirects: int = 0
        self._enrichments: int = 0
        self._failures: int = 0
        self._total_cost_usd: float = 0.0
        self._total_time_s: float = 0.0

    @property
    def is_available(self) -> bool:
        """Check if triage can run (DW available + feature enabled)."""
        if not _TRIAGE_ENABLED:
            return False
        return self._dw is not None and getattr(self._dw, "is_available", False)

    async def triage(self, ctx: Any) -> TriageResult:
        """Run semantic triage on an operation's target files.

        Parameters
        ----------
        ctx:
            OperationContext with target_files and description.

        Returns
        -------
        TriageResult
            The triage decision with supporting evidence.
            Returns SKIP on any failure (graceful degradation).
        """
        if not self.is_available:
            return TriageResult(decision=TriageDecision.SKIP)

        t0 = time.monotonic()
        self._total_triages += 1

        try:
            # Build the triage prompt from target files
            prompt = self._build_triage_prompt(ctx)
            if not prompt:
                return TriageResult(decision=TriageDecision.SKIP)

            # Call DW with the cheap 35B model
            raw_response = await asyncio.wait_for(
                self._dw.prompt_only(
                    prompt=prompt,
                    model=_TRIAGE_MODEL,
                    caller_id=f"triage_{ctx.op_id[:12]}",
                    response_format={"type": "json_object"},
                    max_tokens=_TRIAGE_MAX_TOKENS,
                ),
                timeout=_TRIAGE_TIMEOUT_S,
            )

            if not raw_response:
                self._failures += 1
                return TriageResult(decision=TriageDecision.SKIP)

            # Parse the response
            result = self._parse_response(raw_response, t0)
            result.triage_model = _TRIAGE_MODEL

            # Track stats
            elapsed = time.monotonic() - t0
            result.triage_duration_s = elapsed
            self._total_time_s += elapsed

            if result.decision == TriageDecision.NO_OP:
                self._no_ops_caught += 1
            elif result.decision == TriageDecision.REDIRECT:
                self._redirects += 1
            elif result.decision == TriageDecision.ENRICH:
                self._enrichments += 1

            logger.info(
                "[SemanticTriage] op=%s decision=%s confidence=%.2f "
                "model=%s elapsed=%.1fs files=%d",
                ctx.op_id[:12], result.decision.name, result.confidence,
                _TRIAGE_MODEL.split("/")[-1], elapsed,
                len(ctx.target_files),
            )

            return result

        except asyncio.TimeoutError:
            self._failures += 1
            elapsed = time.monotonic() - t0
            logger.warning(
                "[SemanticTriage] Timeout after %.1fs for op=%s — proceeding without triage",
                elapsed, ctx.op_id[:12],
            )
            return TriageResult(
                decision=TriageDecision.SKIP,
                triage_duration_s=elapsed,
            )
        except Exception as exc:
            self._failures += 1
            elapsed = time.monotonic() - t0
            logger.warning(
                "[SemanticTriage] Error for op=%s: %s — proceeding without triage",
                ctx.op_id[:12], exc,
            )
            return TriageResult(
                decision=TriageDecision.SKIP,
                triage_duration_s=elapsed,
            )

    def _build_triage_prompt(self, ctx: Any) -> str:
        """Build the user prompt with file contents and operation description."""
        target_files = list(ctx.target_files)[:_TRIAGE_MAX_FILES]
        if not target_files:
            return ""

        file_sections = []
        for rel_path in target_files:
            abs_path = self._project_root / rel_path
            if not abs_path.exists():
                file_sections.append(
                    f"### {rel_path}\n[FILE NOT FOUND — may have been moved or deleted]\n"
                )
                continue
            try:
                content = abs_path.read_text(encoding="utf-8")
                if len(content) > _TRIAGE_MAX_FILE_CHARS:
                    # Truncate with head + tail strategy (keep structure visible)
                    head_chars = _TRIAGE_MAX_FILE_CHARS * 2 // 3
                    tail_chars = _TRIAGE_MAX_FILE_CHARS // 3
                    content = (
                        content[:head_chars]
                        + f"\n\n... [{len(content) - head_chars - tail_chars} chars truncated] ...\n\n"
                        + content[-tail_chars:]
                    )
                file_sections.append(f"### {rel_path}\n```python\n{content}\n```\n")
            except (OSError, UnicodeDecodeError) as exc:
                file_sections.append(f"### {rel_path}\n[UNREADABLE: {exc}]\n")

        # Include evidence from the sensor if available
        evidence_section = ""
        evidence = getattr(ctx, "evidence", None) or {}
        if evidence:
            evidence_parts = []
            for key in ("cyclomatic_complexity", "max_function_length",
                        "cognitive_complexity", "duplicate_block_count",
                        "import_fan_out", "todo_fixme_count", "composite_score",
                        "strategy"):
                if key in evidence:
                    evidence_parts.append(f"  - {key}: {evidence[key]}")
            if evidence_parts:
                evidence_section = (
                    "\n## Static Analysis Evidence\n" + "\n".join(evidence_parts) + "\n"
                )

        prompt = (
            f"## Instructions\n{_TRIAGE_SYSTEM_PROMPT}\n\n"
            f"## Operation Goal\n{ctx.description}\n\n"
            f"## Target Files ({len(target_files)})\n"
            + "\n".join(file_sections)
            + evidence_section
            + "\n## Task\n"
            "Analyze the target files and determine the best course of action. "
            "Focus on whether the described change is already done, whether "
            "these are the right files to modify, and what specific issues "
            "the generation model should address. Respond with ONLY valid JSON.\n"
        )

        return prompt

    def _parse_response(self, raw: str, t0: float) -> TriageResult:
        """Parse the LLM's JSON response into a TriageResult."""
        try:
            # Strip markdown fences if present
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                lines = cleaned.split("\n")
                # Remove first and last fence lines
                lines = [l for l in lines if not l.strip().startswith("```")]
                cleaned = "\n".join(lines)

            data = json.loads(cleaned)
        except (json.JSONDecodeError, ValueError):
            logger.debug("[SemanticTriage] Failed to parse JSON response: %s", raw[:200])
            return TriageResult(
                decision=TriageDecision.PROCEED,
                raw_response=raw,
                triage_duration_s=time.monotonic() - t0,
            )

        decision_str = data.get("decision", "PROCEED").upper()
        decision_map = {
            "NO_OP": TriageDecision.NO_OP,
            "NOOP": TriageDecision.NO_OP,
            "REDIRECT": TriageDecision.REDIRECT,
            "ENRICH": TriageDecision.ENRICH,
            "PROCEED": TriageDecision.PROCEED,
        }
        decision = decision_map.get(decision_str, TriageDecision.PROCEED)

        # Build enrichment insights from structured response
        insights_parts = []
        reasoning = data.get("reasoning", "")
        if reasoning:
            insights_parts.append(f"Triage analysis: {reasoning}")

        issues = data.get("issues_found", [])
        if issues:
            insights_parts.append("Issues identified by semantic triage:")
            for issue in issues[:10]:  # Cap at 10 issues
                severity = issue.get("severity", "medium")
                itype = issue.get("type", "unknown")
                location = issue.get("location", "unknown")
                desc = issue.get("description", "")
                insights_parts.append(
                    f"  [{severity.upper()}] {itype} at {location}: {desc}"
                )

        suggested = data.get("suggested_approach", "")
        if suggested:
            insights_parts.append(f"Suggested approach: {suggested}")

        return TriageResult(
            decision=decision,
            confidence=min(1.0, max(0.0, float(data.get("confidence", 0.5)))),
            insights="\n".join(insights_parts),
            redirect_files=data.get("redirect_files", []),
            no_op_reason=reasoning if decision == TriageDecision.NO_OP else "",
            raw_response=raw,
        )

    def get_stats(self) -> Dict[str, Any]:
        """Return triage statistics for observability."""
        return {
            "total_triages": self._total_triages,
            "no_ops_caught": self._no_ops_caught,
            "redirects": self._redirects,
            "enrichments": self._enrichments,
            "failures": self._failures,
            "proceeds": (
                self._total_triages
                - self._no_ops_caught
                - self._redirects
                - self._enrichments
                - self._failures
            ),
            "total_cost_usd": round(self._total_cost_usd, 4),
            "total_time_s": round(self._total_time_s, 1),
            "avg_time_s": (
                round(self._total_time_s / self._total_triages, 1)
                if self._total_triages > 0
                else 0.0
            ),
            "no_op_rate": (
                round(self._no_ops_caught / self._total_triages, 3)
                if self._total_triages > 0
                else 0.0
            ),
            "triage_model": _TRIAGE_MODEL,
        }

    def format_for_prompt(self, result: TriageResult) -> str:
        """Format triage insights for injection into generation prompt context."""
        if not result.insights:
            return ""
        return (
            "\n## Semantic Triage Pre-Analysis\n"
            f"(Analyzed by {result.triage_model.split('/')[-1]}, "
            f"confidence={result.confidence:.0%})\n\n"
            f"{result.insights}\n"
        )
