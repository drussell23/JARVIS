"""
Architecture Reasoning Agent — Design Prompt Builder
=====================================================

Builds the structured prompt fed to a large language model (Doubleword 397B or
Claude) for the Architecture Reasoning Agent.  The model is asked to return a
single JSON object conforming to :data:`ARCHITECTURAL_PLAN_JSON_SCHEMA` that
fully describes an :class:`~backend.core.ouroboros.architect.plan.ArchitecturalPlan`.

Design principles
-----------------
- Zero hardcoding: all content is derived from the ``hypothesis`` object
  (duck-typed) and the ``oracle_context`` string passed by the caller.
- The schema constant mirrors the :class:`~backend.core.ouroboros.architect.plan.ArchitecturalPlan`
  structure so that downstream plan parsers can validate model output without
  re-defining the shape here.
- ``build_design_prompt`` is a pure function — same inputs always yield the
  same output, enabling deterministic tests and caching.
"""

from __future__ import annotations

import json
import textwrap
from typing import Any


# ---------------------------------------------------------------------------
# JSON output schema
# ---------------------------------------------------------------------------

ARCHITECTURAL_PLAN_JSON_SCHEMA: dict = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "ArchitecturalPlan",
    "description": (
        "Structured output from the Architecture Reasoning Agent. "
        "Describes a complete multi-file feature plan for the JARVIS Trinity ecosystem."
    ),
    "type": "object",
    "required": [
        "title",
        "description",
        "repos_affected",
        "non_goals",
        "steps",
        "acceptance_checks",
    ],
    "properties": {
        "title": {
            "type": "string",
            "description": "Short human-readable name for the plan (< 80 chars).",
        },
        "description": {
            "type": "string",
            "description": "Detailed description of what the plan achieves and why.",
        },
        "repos_affected": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Repository keys from RepoRegistry that this plan touches. "
                "Use only repos discovered in the codebase context."
            ),
        },
        "non_goals": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Explicit scope boundaries — what the plan deliberately does NOT do. "
                "Must be non-empty. Declare at least one non-goal."
            ),
        },
        "steps": {
            "type": "array",
            "description": "Ordered sequence of atomic PlanStep objects.",
            "items": {
                "type": "object",
                "required": [
                    "step_index",
                    "description",
                    "intent_kind",
                    "target_paths",
                    "repo",
                    "depends_on",
                ],
                "properties": {
                    "step_index": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Zero-based ordinal within the plan.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Human-readable explanation of what this step achieves.",
                    },
                    "intent_kind": {
                        "type": "string",
                        "enum": ["create_file", "modify_file", "delete_file"],
                        "description": "The high-level file-system intent for this step.",
                    },
                    "target_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "description": (
                            "Primary file paths that will be created, modified, or deleted. "
                            "Must be non-empty. Use paths relative to the repo root."
                        ),
                    },
                    "repo": {
                        "type": "string",
                        "description": "The repository key that owns the target_paths.",
                    },
                    "ancillary_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Supporting paths read or lightly touched but not the primary target."
                        ),
                    },
                    "interface_contracts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Free-text descriptions of public API contracts that must remain stable.",
                    },
                    "tests_required": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Paths of test files that must exist and pass after this step. "
                            "Every step that creates or modifies runtime code must include at least one test."
                        ),
                    },
                    "risk_tier_hint": {
                        "type": "string",
                        "description": (
                            "Advisory risk label: 'safe_auto' (no human approval) or 'needs_review'. "
                            "Defaults to 'safe_auto'."
                        ),
                    },
                    "depends_on": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": (
                            "Indices of steps that must complete before this step starts. "
                            "Use empty list [] for steps with no dependencies. "
                            "Must form a valid DAG (no cycles)."
                        ),
                    },
                },
            },
        },
        "acceptance_checks": {
            "type": "array",
            "description": "Verifiable criteria that must pass before the plan is considered done.",
            "items": {
                "type": "object",
                "required": ["check_id", "check_kind", "command"],
                "properties": {
                    "check_id": {
                        "type": "string",
                        "description": "Unique identifier for this check (e.g. 'chk-001').",
                    },
                    "check_kind": {
                        "type": "string",
                        "enum": ["exit_code", "regex_stdout", "import_check"],
                        "description": "The mechanism used to evaluate pass/fail.",
                    },
                    "command": {
                        "type": "string",
                        "description": (
                            "Shell command to execute (or import path for import_check). "
                            "Must be runnable from the repo root without additional setup."
                        ),
                    },
                    "expected": {
                        "type": "string",
                        "description": (
                            "For exit_code: the expected numeric exit code as a string (e.g. '0'). "
                            "For regex_stdout: a regex pattern that must match stdout. "
                            "For import_check: leave empty."
                        ),
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Working directory for command execution. Defaults to '.'.",
                    },
                    "timeout_s": {
                        "type": "number",
                        "description": "Maximum seconds before the check is considered failed.",
                    },
                    "run_after_step": {
                        "type": ["integer", "null"],
                        "description": (
                            "If set, run this check immediately after the referenced step index "
                            "rather than at plan completion."
                        ),
                    },
                    "sandbox_required": {
                        "type": "boolean",
                        "description": "Whether the check must run inside an isolated sandbox. Default: true.",
                    },
                },
            },
        },
    },
}


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------


def build_design_prompt(
    hypothesis: Any,
    oracle_context: str,
    max_steps: int = 10,
) -> str:
    """Build the design prompt for the Architecture Reasoning Agent.

    Parameters
    ----------
    hypothesis:
        Duck-typed hypothesis object.  Must expose ``.description``,
        ``.evidence_fragments``, ``.gap_type``, and ``.suggested_scope``.
        Accepts both :class:`~backend.core.ouroboros.roadmap.hypothesis.FeatureHypothesis`
        instances and lightweight mocks.
    oracle_context:
        Free-text codebase context string produced by TheOracle (file
        neighbourhood, structural topology, recent changes, etc.).  Embedded
        verbatim in the CODEBASE CONTEXT section.
    max_steps:
        Upper bound on the number of ``steps`` the model may produce.
        Enforced as a constraint in the CONSTRAINTS section.

    Returns
    -------
    str
        The fully assembled prompt string ready to be sent to the model.
        Always a non-empty string.
    """
    # --- Render evidence fragments ---
    evidence_fragments = getattr(hypothesis, "evidence_fragments", ())
    if evidence_fragments:
        evidence_lines = "\n".join(
            f"  - {frag}" for frag in evidence_fragments
        )
    else:
        evidence_lines = "  (none)"

    # --- Render JSON schema (compact but human-readable) ---
    schema_block = json.dumps(ARCHITECTURAL_PLAN_JSON_SCHEMA, indent=2)

    # --- Assemble prompt ---
    prompt = textwrap.dedent(f"""\
        You are designing a multi-file feature for the JARVIS Trinity ecosystem.

        Your task is to produce a complete architectural plan that resolves the
        capability gap described below.  Return a single JSON object that
        conforms exactly to the schema provided at the end of this prompt.
        Do not include any text outside the JSON object.

        ════════════════════════════════════════════════════════════════════
        CAPABILITY GAP
        ════════════════════════════════════════════════════════════════════
        description     : {hypothesis.description}
        gap_type        : {hypothesis.gap_type}
        suggested_scope : {hypothesis.suggested_scope}
        evidence_fragments:
{evidence_lines}

        ════════════════════════════════════════════════════════════════════
        CODEBASE CONTEXT (from TheOracle — structural graph topology)
        ════════════════════════════════════════════════════════════════════
        {oracle_context if oracle_context else "(no oracle context provided)"}

        ════════════════════════════════════════════════════════════════════
        CONSTRAINTS
        ════════════════════════════════════════════════════════════════════
        1. STEP LIMIT — produce at most {max_steps} steps. Prefer fewer, focused steps.

        2. FILE PATH RULES
           - All target_paths and ancillary_paths must be relative to the repo root.
           - Do not invent paths that are not implied by the codebase context or gap type.
           - Every new Python module path must end in '.py'.
           - Never use absolute paths; never reference paths outside the repo.

        3. TEST REQUIREMENTS
           - Every step that creates or modifies runtime code MUST include at least
             one test file in tests_required.
           - Test paths must mirror the source path under tests/ (e.g.
             backend/core/x.py → tests/core/test_x.py).
           - acceptance_checks must include at least one exit_code check that runs
             pytest on the new/modified test paths.

        4. NON-GOALS
           - Declare at least one item in non_goals to make scope boundaries explicit.
           - non_goals must be honest: do not list things the plan actually does.

        5. DAG / DEPENDENCY ORDERING
           - depends_on must reference only step_index values that appear earlier
             in the steps array (i.e. lower index numbers).
           - The dependency graph must be a valid DAG — no cycles allowed.
           - Steps that have no dependencies must set depends_on to [].

        6. REPO KEYS
           - repos_affected must only contain repo keys discoverable from the
             codebase context. Do not invent new repo names.

        ════════════════════════════════════════════════════════════════════
        OUTPUT JSON SCHEMA
        ════════════════════════════════════════════════════════════════════
        {schema_block}
    """)

    return prompt
