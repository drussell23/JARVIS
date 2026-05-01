"""Move 5 Slice 2 — Confidence Probe Question generator.

Synthesizes ``ProbeQuestion`` tuples from an ``AmbiguityContext``.
Slice 3's runner consumes these → prober resolves them →
convergence detector classifies the answers.

Two operator-tunable modes:

  * ``templates`` (default — $0 cost) — deterministic template
    expansion. Same input produces same questions; reproducible
    + auditable; no LLM calls.

  * ``llm`` (operator opt-in via env knob) — small auxiliary
    model call to synthesize questions. Slice 5 graduation will
    decide whether to ship the LLM mode (for now Slice 2 only
    implements ``templates``; ``llm`` mode falls through to
    ``templates`` with a logged warning).

Direct-solve principles:

  * **Asynchronous-ready** — pure-sync generator; Slice 3's
    runner can call this from any async context safely.

  * **Dynamic** — generator mode env-tunable; max-questions
    env-tunable (Slice 1 knob); template synthesis adapts to
    available context fields.

  * **Adaptive** — when AmbiguityContext lacks a target_symbol,
    falls back to file-level questions; when both lack, falls
    back to project-level discovery questions.

  * **Intelligent** — each template carries a
    ``resolution_method`` hint matching one of
    ``READONLY_TOOL_ALLOWLIST`` so the prober's tool plan is
    deterministic.

  * **Robust** — never raises. AmbiguityContext can have any/all
    fields empty; generator produces a sensible fallback set.

  * **No hardcoding** — template strings are module-level
    tuples; mode is env-driven; max_questions is env-driven via
    Slice 1 helper.

Authority invariants (AST-pinned):

  * Imports stdlib + verification.confidence_probe_bridge (Slice
    1 types) ONLY.
  * NEVER imports orchestrator / phase_runners /
    candidate_generator / iron_gate / change_engine / policy /
    semantic_guardian / semantic_firewall / providers /
    doubleword_provider / urgency_router / auto_action_router /
    subagent_scheduler / tool_executor.
  * NEVER references mutation tool names (edit_file / write_file
    / delete_file / run_tests / bash) in code (AST walk verifies).
  * No LLM client imports anywhere — Slice 2 ships templates only.
  * No async functions.
  * No disk writes.
"""
from __future__ import annotations

import enum
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from backend.core.ouroboros.governance.verification.confidence_probe_bridge import (  # noqa: E501
    ProbeQuestion,
    max_questions,
    max_tool_rounds_per_question,
)

logger = logging.getLogger(__name__)


CONFIDENCE_PROBE_GENERATOR_SCHEMA_VERSION: str = (
    "confidence_probe_generator.1"
)


# ---------------------------------------------------------------------------
# Generator mode (closed taxonomy)
# ---------------------------------------------------------------------------


class GeneratorMode(str, enum.Enum):
    """Closed 2-value taxonomy of question-generation modes.

    ``TEMPLATES``  — deterministic template expansion ($0 cost,
                     auditable, reproducible). Slice 2 default
                     and only implementation.
    ``LLM``        — auxiliary-model synthesis (deferred to a
                     post-graduation slice; currently falls
                     through to TEMPLATES with a logged warning)."""

    TEMPLATES = "templates"
    LLM = "llm"


def generator_mode() -> GeneratorMode:
    """``JARVIS_CONFIDENCE_PROBE_GENERATOR_MODE`` (default
    ``templates``). Asymmetric env semantics: empty/whitespace =
    unset = default; ``llm`` opts in (currently no-op, falls
    back to templates); other values fall back to templates."""
    raw = os.environ.get(
        "JARVIS_CONFIDENCE_PROBE_GENERATOR_MODE", "",
    ).strip().lower()
    if raw == GeneratorMode.LLM.value:
        return GeneratorMode.LLM
    return GeneratorMode.TEMPLATES


# ---------------------------------------------------------------------------
# AmbiguityContext — generator input shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AmbiguityContext:
    """Frozen input for the generator. Every field optional;
    generator produces sensible fallbacks when fields are empty.

    Production callers (Slice 3+) populate from
    ``ConfidenceMonitor.snapshot()`` + the op being executed:

      * ``op_id`` — current op identifier (passed through to
        ProbeAnswer + telemetry)
      * ``target_symbol`` — symbol the op is uncertain about
        (e.g., function/class name)
      * ``target_file`` — file containing target_symbol
      * ``claim`` — the claim being verified
        (e.g., "foo is a function returning int")
      * ``posture`` — current posture string for context
      * ``rolling_margin`` — confidence_monitor's rolling margin
        (lower = more uncertain → more probes)
    """

    op_id: str = ""
    target_symbol: str = ""
    target_file: str = ""
    claim: str = ""
    posture: str = ""
    rolling_margin: Optional[float] = None
    schema_version: str = CONFIDENCE_PROBE_GENERATOR_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "op_id": self.op_id,
            "target_symbol": self.target_symbol,
            "target_file": self.target_file,
            "claim": self.claim,
            "posture": self.posture,
            "rolling_margin": self.rolling_margin,
            "schema_version": self.schema_version,
        }


# ---------------------------------------------------------------------------
# Template tuples — module-level, deterministic, no hardcoded behavior
# ---------------------------------------------------------------------------
#
# Each template is a (question_template, resolution_method) pair.
# question_template uses {symbol} / {file} / {claim} placeholders.
# resolution_method MUST match a name in
# READONLY_TOOL_ALLOWLIST (verified by Slice 5 graduation pin).


_SYMBOL_TEMPLATES: Tuple[Tuple[str, str], ...] = (
    ("Where is {symbol} defined?", "search_code"),
    ("Who calls {symbol}?", "get_callers"),
    ("List symbols around {symbol}", "list_symbols"),
)


_FILE_TEMPLATES: Tuple[Tuple[str, str], ...] = (
    ("Read structure of {file}", "read_file"),
    ("List symbols in {file}", "list_symbols"),
    ("Recent history of {file}", "git_log"),
)


_SYMBOL_AND_FILE_TEMPLATES: Tuple[Tuple[str, str], ...] = (
    ("Find {symbol} in {file}", "search_code"),
    ("Read structure of {file} (looking for {symbol})", "read_file"),
    ("Who calls {symbol}?", "get_callers"),
)


_FALLBACK_TEMPLATES: Tuple[Tuple[str, str], ...] = (
    ("List project root", "list_dir"),
    ("Search for likely targets", "search_code"),
    ("Recent git activity", "git_log"),
)


# ---------------------------------------------------------------------------
# Public generator entry point
# ---------------------------------------------------------------------------


def generate_probes(
    ambiguity_context: AmbiguityContext,
    *,
    max_questions_override: Optional[int] = None,
    mode_override: Optional[GeneratorMode] = None,
) -> Tuple[ProbeQuestion, ...]:
    """Synthesize a bounded tuple of ProbeQuestion. NEVER raises.

    Decision logic:

      1. Mode ``LLM`` requested but not implemented → log warning,
         fall through to TEMPLATES.
      2. Compute effective max_questions: override > env knob.
      3. Pick template set based on context fields:
         * symbol + file → ``_SYMBOL_AND_FILE_TEMPLATES``
         * symbol only → ``_SYMBOL_TEMPLATES``
         * file only → ``_FILE_TEMPLATES``
         * neither → ``_FALLBACK_TEMPLATES``
      4. Expand templates against context (defensive on missing
         placeholders).
      5. Cap at effective max_questions.

    Each ProbeQuestion carries ``max_tool_rounds=0`` (defers to
    bridge env knob) and the matching ``resolution_method`` hint."""
    try:
        if not isinstance(ambiguity_context, AmbiguityContext):
            return ()

        mode = (
            mode_override if mode_override is not None
            else generator_mode()
        )
        if mode is GeneratorMode.LLM:
            logger.warning(
                "[ConfidenceProbeGenerator] LLM mode requested "
                "but not implemented in Slice 2; falling back to "
                "TEMPLATES",
            )
            mode = GeneratorMode.TEMPLATES
        del mode  # only TEMPLATES is implemented

        if max_questions_override is not None and \
                max_questions_override > 0:
            cap = int(max_questions_override)
        else:
            cap = max_questions()
        cap = max(1, cap)

        templates = _select_templates(ambiguity_context)
        questions: List[ProbeQuestion] = []
        for tmpl, method in templates:
            if len(questions) >= cap:
                break
            q_text = _expand_template(tmpl, ambiguity_context)
            if not q_text:
                continue
            questions.append(
                ProbeQuestion(
                    question=q_text,
                    resolution_method=method,
                    max_tool_rounds=0,  # defer to bridge env knob
                ),
            )

        # Pad with fallback if we didn't fill the quota and there's
        # still room
        if len(questions) < cap:
            for tmpl, method in _FALLBACK_TEMPLATES:
                if len(questions) >= cap:
                    break
                q_text = _expand_template(tmpl, ambiguity_context)
                if not q_text:
                    continue
                # Avoid exact-text duplicates
                if any(q.question == q_text for q in questions):
                    continue
                questions.append(
                    ProbeQuestion(
                        question=q_text,
                        resolution_method=method,
                        max_tool_rounds=0,
                    ),
                )

        return tuple(questions)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[ConfidenceProbeGenerator] generate_probes raised: %s",
            exc,
        )
        return ()


def _select_templates(
    ctx: AmbiguityContext,
) -> Tuple[Tuple[str, str], ...]:
    """Choose template set based on which context fields are
    populated. Pure function. NEVER raises."""
    has_symbol = bool((ctx.target_symbol or "").strip())
    has_file = bool((ctx.target_file or "").strip())
    if has_symbol and has_file:
        return _SYMBOL_AND_FILE_TEMPLATES
    if has_symbol:
        return _SYMBOL_TEMPLATES
    if has_file:
        return _FILE_TEMPLATES
    return _FALLBACK_TEMPLATES


def _expand_template(
    template: str, ctx: AmbiguityContext,
) -> str:
    """Substitute {symbol} / {file} / {claim} placeholders.
    Empty values produce empty result (caller skips). NEVER
    raises."""
    try:
        symbol = (ctx.target_symbol or "").strip()
        file_ = (ctx.target_file or "").strip()
        claim = (ctx.claim or "").strip()
        # Required-field check: if template names a placeholder
        # but context lacks the value, skip.
        if "{symbol}" in template and not symbol:
            return ""
        if "{file}" in template and not file_:
            return ""
        if "{claim}" in template and not claim:
            return ""
        return template.format(
            symbol=symbol, file=file_, claim=claim,
        )
    except (KeyError, IndexError, ValueError):
        return ""
    except Exception:  # noqa: BLE001 — defensive
        return ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "AmbiguityContext",
    "CONFIDENCE_PROBE_GENERATOR_SCHEMA_VERSION",
    "GeneratorMode",
    "generate_probes",
    "generator_mode",
]
