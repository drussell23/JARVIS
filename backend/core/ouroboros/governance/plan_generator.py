"""
Plan Generator — Model-Reasoned Implementation Planning
========================================================

Inserted between CONTEXT_EXPANSION and GENERATE, the PlanGenerator asks the
model to **reason about HOW** to implement a change before writing code.  The
output is a structured JSON plan (schema ``plan.1``) that is injected into the
GENERATE prompt, giving the code-generation model a coherent strategy rather
than ad-hoc patching.

This is Gap #3 from the O+V vs CC comparative analysis: Claude Code internally
builds a mental execution plan before emitting any edits.  PlanGenerator
replicates that reasoning step explicitly.

Design Principles (Manifesto alignment)
----------------------------------------
- **§5 Intelligence-driven routing**: The model reasons about complexity and
  chooses the implementation approach — not a hardcoded heuristic.
- **§7 Absolute observability**: The plan is captured in ``ctx.implementation_plan``
  and emitted via CommProtocol heartbeats so SerpentFlow can render it.
- **Bounded execution**: Hard governor limits on plan prompt size and response
  tokens.  Planning failures are soft — the pipeline falls through to GENERATE.
- **Trivial-op fast path**: Single-file, low-complexity ops skip planning
  entirely (configurable via ``JARVIS_PLAN_MIN_FILES``).

Schema: plan.1
--------------
.. code-block:: json

    {
        "schema_version": "plan.1",
        "approach": "Description of the implementation strategy",
        "complexity": "trivial|moderate|complex|architectural",
        "ordered_changes": [
            {
                "file_path": "path/to/file.py",
                "change_type": "modify|create|delete",
                "description": "What to change and why",
                "dependencies": ["path/to/other.py"],
                "estimated_scope": "small|medium|large"
            }
        ],
        "risk_factors": ["Risk description"],
        "test_strategy": "How to verify the changes",
        "architectural_notes": "Cross-cutting concerns, invariants to preserve"
    }
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.core.ouroboros.governance.op_context import OperationContext

logger = logging.getLogger("Ouroboros.PlanGenerator")

_TRUTHY = frozenset({"1", "true", "yes", "on"})

# ---------------------------------------------------------------------------
# Governor limits (hardcoded — Manifesto §6 Iron Gate)
# ---------------------------------------------------------------------------

# Maximum chars from each source file included in planning context
_PLAN_FILE_CONTEXT_CHARS = int(os.environ.get("JARVIS_PLAN_FILE_CONTEXT_CHARS", "6000"))

# Planning timeout — used by orchestrator when calling generate_plan()
PLAN_TIMEOUT_S = float(os.environ.get("JARVIS_PLAN_TIMEOUT_S", "45"))

# Schema version
_PLAN_SCHEMA_VERSION = "plan.1"

# Complexity thresholds for skip decision
_TRIVIAL_MAX_FILES = int(os.environ.get("JARVIS_PLAN_TRIVIAL_MAX_FILES", "1"))
_TRIVIAL_MAX_DESCRIPTION_LEN = 200


def _plan_review_required() -> bool:
    """Return True when the session requires a pre-execution plan review."""
    return (
        os.environ.get("JARVIS_SHOW_PLAN_BEFORE_EXECUTE", "").strip().lower()
        in _TRUTHY
    )


# ---------------------------------------------------------------------------
# Plan result
# ---------------------------------------------------------------------------


class PlanResult:
    """Outcome of the planning phase.

    Attributes
    ----------
    plan_json : str
        Raw JSON string of the structured plan (schema plan.1).
    approach : str
        Human-readable summary of the implementation strategy.
    complexity : str
        Model-assessed complexity: trivial|moderate|complex|architectural.
    ordered_changes : list
        Ordered list of file change descriptors.
    risk_factors : list
        Identified risks.
    test_strategy : str
        How the model recommends verifying changes.
    architectural_notes : str
        Cross-cutting concerns.
    planning_duration_s : float
        Wall-clock seconds spent planning.
    skipped : bool
        True if planning was skipped (trivial op).
    skip_reason : str
        Why planning was skipped.
    """

    __slots__ = (
        "plan_json", "approach", "complexity", "ordered_changes",
        "risk_factors", "test_strategy", "architectural_notes",
        "planning_duration_s", "skipped", "skip_reason",
    )

    def __init__(
        self,
        plan_json: str = "",
        approach: str = "",
        complexity: str = "moderate",
        ordered_changes: Optional[List[Dict[str, Any]]] = None,
        risk_factors: Optional[List[str]] = None,
        test_strategy: str = "",
        architectural_notes: str = "",
        planning_duration_s: float = 0.0,
        skipped: bool = False,
        skip_reason: str = "",
    ) -> None:
        self.plan_json = plan_json
        self.approach = approach
        self.complexity = complexity
        self.ordered_changes = ordered_changes or []
        self.risk_factors = risk_factors or []
        self.test_strategy = test_strategy
        self.architectural_notes = architectural_notes
        self.planning_duration_s = planning_duration_s
        self.skipped = skipped
        self.skip_reason = skip_reason

    @classmethod
    def skipped_result(cls, reason: str) -> "PlanResult":
        """Factory for a skipped-planning result."""
        return cls(skipped=True, skip_reason=reason, complexity="trivial")

    def to_prompt_section(self) -> str:
        """Format the plan as a prompt section for injection into GENERATE.

        Returns an empty string if planning was skipped.
        """
        if self.skipped or not self.approach:
            return ""

        parts = [
            "## Implementation Plan (model-reasoned — follow this strategy)\n",
            f"**Approach**: {self.approach}\n",
            f"**Complexity**: {self.complexity}\n",
        ]

        if self.ordered_changes:
            parts.append("**Ordered Changes** (implement in this order):\n")
            for i, change in enumerate(self.ordered_changes, 1):
                fp = change.get("file_path", "?")
                ct = change.get("change_type", "modify")
                desc = change.get("description", "")
                deps = change.get("dependencies", [])
                scope = change.get("estimated_scope", "medium")
                dep_str = f" (depends on: {', '.join(deps)})" if deps else ""
                parts.append(
                    f"  {i}. `{fp}` [{ct}, {scope}]{dep_str}\n"
                    f"     {desc}\n"
                )

        if self.risk_factors:
            parts.append("**Risk Factors**:\n")
            for risk in self.risk_factors:
                parts.append(f"  - {risk}\n")

        if self.test_strategy:
            parts.append(f"**Test Strategy**: {self.test_strategy}\n")

        if self.architectural_notes:
            parts.append(f"**Architectural Notes**: {self.architectural_notes}\n")

        return "\n".join(parts)


# ---------------------------------------------------------------------------
# PlanGenerator
# ---------------------------------------------------------------------------


class PlanGenerator:
    """Generates structured implementation plans before code generation.

    Uses the CandidateGenerator's ``plan()`` method (lightweight prompt path)
    to ask the model to reason about implementation strategy before writing code.

    Parameters
    ----------
    generator:
        CandidateGenerator instance (must have ``plan(prompt, deadline) -> str``).
    repo_root:
        Project root for resolving file paths.
    """

    def __init__(
        self,
        generator: Any,
        repo_root: Path,
    ) -> None:
        self._generator = generator
        self._repo_root = repo_root

    async def generate_plan(
        self,
        ctx: OperationContext,
        deadline: datetime,
    ) -> PlanResult:
        """Generate an implementation plan for the given operation.

        Returns a :class:`PlanResult` — either a full plan or a skipped result
        for trivial operations.

        This method never raises; planning failures are logged and return a
        skipped result so the pipeline can fall through to GENERATE.
        """
        # ── Fast-path: skip planning for trivial ops unless the user has
        # explicitly asked to review a plan before any execution. ──
        forced_plan_review = _plan_review_required()
        skip_reason = "" if forced_plan_review else self._should_skip(ctx)
        if skip_reason:
            logger.info(
                "[PlanGenerator] Skipping plan for op=%s: %s",
                ctx.op_id, skip_reason,
            )
            return PlanResult.skipped_result(skip_reason)
        if forced_plan_review:
            skipped_without_override = self._should_skip(ctx)
            if skipped_without_override:
                logger.info(
                    "[PlanGenerator] Plan review required for op=%s; bypassing skip: %s",
                    ctx.op_id, skipped_without_override,
                )

        t0 = time.monotonic()

        try:
            prompt = self._build_plan_prompt(ctx)
            raw_response = await self._generator.plan(prompt, deadline)
            result = self._parse_plan_response(raw_response)
            result.planning_duration_s = time.monotonic() - t0

            # Validate plan coherence — planned files should overlap with targets
            self._validate_plan_coherence(ctx, result)

            logger.info(
                "[PlanGenerator] Plan generated for op=%s: complexity=%s, "
                "%d changes, %.1fs",
                ctx.op_id, result.complexity,
                len(result.ordered_changes), result.planning_duration_s,
            )
            return result

        except Exception as exc:
            duration = time.monotonic() - t0
            logger.warning(
                "[PlanGenerator] Planning failed for op=%s (%.1fs): %s; "
                "falling through to GENERATE without plan",
                ctx.op_id, duration, exc,
            )
            return PlanResult.skipped_result(f"planning_failed: {type(exc).__name__}")

    # ------------------------------------------------------------------
    # Skip logic
    # ------------------------------------------------------------------

    def _should_skip(self, ctx: OperationContext) -> str:
        """Return a skip reason string, or empty string if planning should proceed."""
        n_files = len(ctx.target_files)

        # Single-file trivial ops with short descriptions
        if (
            n_files <= _TRIVIAL_MAX_FILES
            and len(ctx.description) <= _TRIVIAL_MAX_DESCRIPTION_LEN
        ):
            return f"trivial_op: {n_files} file(s), short description"

        # No target files (e.g. documentation-only or exploratory)
        if n_files == 0:
            return "no_target_files"

        return ""

    # ------------------------------------------------------------------
    # Prompt construction
    # ------------------------------------------------------------------

    def _build_plan_prompt(self, ctx: OperationContext) -> str:
        """Build the planning prompt from operation context.

        The prompt gives the model:
        1. The operation goal
        2. Target file list with size/structure hints
        3. Expanded context summary (if available)
        4. Strategic memory (if available)
        5. Session lessons (if available)
        6. The structured output schema (plan.1)
        """
        parts: List[str] = []

        # ── 1. System framing ──
        parts.append(
            "You are an expert software architect planning an implementation strategy.\n"
            "Your job is to THINK about HOW to implement the requested change before "
            "any code is written. Reason about:\n"
            "- The correct order of file modifications (dependency-aware)\n"
            "- Which files need to change and what each change involves\n"
            "- Risks, edge cases, and invariants that must be preserved\n"
            "- How to verify the changes work correctly\n"
            "- Whether this is a simple tweak or an architectural change\n\n"
            "Do NOT write any code. Only plan the implementation strategy."
        )

        # ── 2. Operation goal ──
        parts.append(
            f"## Goal\n\nOp-ID: {ctx.op_id}\n{ctx.description}"
        )

        # ── 3. Target files with structure hints ──
        file_hints: List[str] = []
        for raw_path in ctx.target_files:
            abs_path = (
                Path(raw_path) if Path(raw_path).is_absolute()
                else (self._repo_root / raw_path).resolve()
            )
            hint = self._build_file_hint(abs_path, raw_path)
            file_hints.append(hint)

        if file_hints:
            parts.append(
                "## Target Files\n\n" + "\n\n".join(file_hints)
            )

        # ── 4. Expanded context summary ──
        if ctx.expanded_context_files:
            exp_list = "\n".join(
                f"  - `{f}`" for f in ctx.expanded_context_files[:10]
            )
            parts.append(
                f"## Related Context Files (read-only, for understanding)\n\n{exp_list}"
            )

        # ── 5. Strategic memory ──
        strategic = getattr(ctx, "strategic_memory_prompt", "")
        if isinstance(strategic, str) and strategic.strip():
            parts.append(strategic)

        # ── 6. Session lessons ──
        lessons = getattr(ctx, "session_lessons", "")
        if isinstance(lessons, str) and lessons.strip():
            parts.append(
                "## Session Lessons\n\n" + lessons.strip()
            )

        # ── 7. Human instructions ──
        human_instr = getattr(ctx, "human_instructions", "")
        if isinstance(human_instr, str) and human_instr.strip():
            parts.append(
                "## Human Instructions\n\n" + human_instr.strip()
            )

        # ── 8. Output schema ──
        parts.append(self._plan_schema_instruction())

        return "\n\n".join(parts)

    def _build_file_hint(self, abs_path: Path, rel_path: str) -> str:
        """Build a structural hint for a target file.

        Includes: path, size, line count, top-level symbols (classes/functions),
        and a truncated preview of the file content.
        """
        if not abs_path.is_file():
            return f"### `{rel_path}` [NEW FILE — does not exist yet]"

        try:
            content = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return f"### `{rel_path}` [UNREADABLE]"

        line_count = content.count("\n") + 1
        size_bytes = len(content.encode("utf-8"))

        # Extract top-level symbols for Python files
        symbols_str = ""
        if abs_path.suffix == ".py":
            symbols = self._extract_symbols(content)
            if symbols:
                symbols_str = f"\nSymbols: {', '.join(symbols[:20])}"

        # Truncated preview
        preview = content[:_PLAN_FILE_CONTEXT_CHARS]
        if len(content) > _PLAN_FILE_CONTEXT_CHARS:
            preview += "\n... [truncated]"

        return (
            f"### `{rel_path}` [{size_bytes:,} bytes, {line_count} lines]"
            f"{symbols_str}\n"
            f"```\n{preview}\n```"
        )

    @staticmethod
    def _extract_symbols(source: str) -> List[str]:
        """Extract top-level class and function names from Python source."""
        import ast as _ast
        try:
            tree = _ast.parse(source)
        except SyntaxError:
            return []
        symbols: List[str] = []
        for node in _ast.iter_child_nodes(tree):
            if isinstance(node, _ast.ClassDef):
                symbols.append(f"class {node.name}")
            elif isinstance(node, _ast.FunctionDef):
                symbols.append(f"def {node.name}")
            elif isinstance(node, _ast.AsyncFunctionDef):
                symbols.append(f"async def {node.name}")
        return symbols

    @staticmethod
    def _plan_schema_instruction() -> str:
        """Return the structured output schema for plan.1."""
        return f"""## Output Schema

Return a JSON object matching **exactly** this structure (schema_version: "{_PLAN_SCHEMA_VERSION}"):

```json
{{
  "schema_version": "{_PLAN_SCHEMA_VERSION}",
  "approach": "<1-3 sentences describing the implementation strategy>",
  "complexity": "<trivial|moderate|complex|architectural>",
  "ordered_changes": [
    {{
      "file_path": "<repo-relative path>",
      "change_type": "<modify|create|delete>",
      "description": "<what to change in this file and why>",
      "dependencies": ["<paths of files that must be changed first>"],
      "estimated_scope": "<small|medium|large>"
    }}
  ],
  "risk_factors": [
    "<description of a specific risk or edge case>"
  ],
  "test_strategy": "<how to verify the changes — specific test commands or manual checks>",
  "architectural_notes": "<cross-cutting concerns, invariants to preserve, or empty string>"
}}
```

Rules:
- `ordered_changes` must list files in dependency order (change dependencies first).
- `dependencies` within each change refers to OTHER files in the list that must be modified first.
- `complexity` assessment: trivial = typo/config, moderate = single-concern change, complex = multi-file coordinated change, architectural = cross-cutting refactor.
- `risk_factors` should be specific and actionable, not generic warnings.
- Return ONLY the JSON object. No explanation outside the JSON."""

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _parse_plan_response(self, raw: str) -> PlanResult:
        """Parse the model's JSON response into a PlanResult.

        Handles common model output quirks: markdown code fences, preamble text,
        and malformed JSON.
        """
        # Strip markdown code fences
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            # Remove opening fence (possibly with language tag)
            first_newline = cleaned.index("\n") if "\n" in cleaned else len(cleaned)
            cleaned = cleaned[first_newline + 1:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

        # Try to find JSON object boundaries
        json_start = cleaned.find("{")
        json_end = cleaned.rfind("}")
        if json_start == -1 or json_end == -1:
            raise ValueError(f"No JSON object found in plan response ({len(raw)} chars)")

        json_str = cleaned[json_start:json_end + 1]

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in plan response: {exc}") from exc

        # Validate schema version
        sv = data.get("schema_version", "")
        if sv and sv != _PLAN_SCHEMA_VERSION:
            logger.warning(
                "[PlanGenerator] Unexpected schema_version %r (expected %r)",
                sv, _PLAN_SCHEMA_VERSION,
            )

        # Extract fields with safe defaults
        approach = data.get("approach", "")
        complexity = data.get("complexity", "moderate")
        if complexity not in ("trivial", "moderate", "complex", "architectural"):
            complexity = "moderate"

        ordered_changes = data.get("ordered_changes", [])
        if not isinstance(ordered_changes, list):
            ordered_changes = []

        # Normalize each change entry
        normalized_changes: List[Dict[str, Any]] = []
        for change in ordered_changes:
            if not isinstance(change, dict):
                continue
            normalized_changes.append({
                "file_path": str(change.get("file_path", "")),
                "change_type": str(change.get("change_type", "modify")),
                "description": str(change.get("description", "")),
                "dependencies": [
                    str(d) for d in change.get("dependencies", [])
                    if isinstance(d, str)
                ],
                "estimated_scope": str(change.get("estimated_scope", "medium")),
            })

        risk_factors = [
            str(r) for r in data.get("risk_factors", [])
            if isinstance(r, str)
        ]

        test_strategy = str(data.get("test_strategy", ""))
        architectural_notes = str(data.get("architectural_notes", ""))

        return PlanResult(
            plan_json=json_str,
            approach=approach,
            complexity=complexity,
            ordered_changes=normalized_changes,
            risk_factors=risk_factors,
            test_strategy=test_strategy,
            architectural_notes=architectural_notes,
        )

    # ------------------------------------------------------------------
    # Coherence validation
    # ------------------------------------------------------------------

    def _validate_plan_coherence(
        self,
        ctx: OperationContext,
        result: PlanResult,
    ) -> None:
        """Validate that the plan is coherent with the operation context.

        Checks:
        1. Planned files should overlap with target files (warns if not)
        2. Dependency references should point to files in the change list
        3. No circular dependencies in the ordered_changes
        """
        if not result.ordered_changes:
            return

        target_set = set(ctx.target_files)
        planned_files = {c["file_path"] for c in result.ordered_changes}

        # Check overlap — plan might legitimately suggest additional files
        overlap = target_set & planned_files
        if target_set and not overlap:
            logger.warning(
                "[PlanGenerator] Plan files %s have zero overlap with targets %s — "
                "possible model hallucination",
                planned_files, target_set,
            )

        # Check internal dependency references
        for change in result.ordered_changes:
            for dep in change.get("dependencies", []):
                if dep not in planned_files:
                    logger.debug(
                        "[PlanGenerator] Change %s depends on %s which is not in "
                        "the change list",
                        change["file_path"], dep,
                    )

        # Check for circular dependencies (simplified — just ensure DAG)
        visited: set = set()
        rec_stack: set = set()
        adj: Dict[str, List[str]] = {
            c["file_path"]: c.get("dependencies", [])
            for c in result.ordered_changes
        }

        def _has_cycle(node: str) -> bool:
            visited.add(node)
            rec_stack.add(node)
            for neighbor in adj.get(node, []):
                if neighbor not in visited:
                    if _has_cycle(neighbor):
                        return True
                elif neighbor in rec_stack:
                    return True
            rec_stack.discard(node)
            return False

        for node in adj:
            if node not in visited:
                if _has_cycle(node):
                    logger.warning(
                        "[PlanGenerator] Circular dependency detected in plan — "
                        "model output may have ordering errors"
                    )
                    break
