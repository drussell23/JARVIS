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
                "estimated_scope": "small|medium|large",
                "expected_outcome": "Falsifiable predicate the model "
                                    "commits to (e.g., 'auth.py exists "
                                    "and login() returns bool')"
            }
        ],
        "risk_factors": ["Risk description"],
        "test_strategy": "How to verify the changes",
        "architectural_notes": "Cross-cutting concerns, invariants to preserve",
        "ui_affected": false
    }

The ``ui_affected`` field (added by Task 5 of the VisionSensor + Visual
VERIFY arc) is stamped *deterministically* by ``classify_ui_affected``
after parse — not produced by the model. It routes Visual VERIFY (see
``docs/superpowers/specs/2026-04-18-vision-sensor-verify-design.md``
§VERIFY Extension → Trigger conditions).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from backend.core.ouroboros.governance.op_context import OperationContext
from backend.core.ouroboros.governance.plan_falsification import (
    PlanStepHypothesis,
    pair_plan_step_with_hypothesis,
)

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


def _plan_hypothesis_emit_enabled() -> bool:
    """``JARVIS_PLAN_HYPOTHESIS_EMIT_ENABLED`` (default ``true``).

    Sub-flag for PlanFalsificationDetector Slice 3 prompt + parser
    extension. When **on**, the planning prompt asks the model for a
    falsifiable ``expected_outcome`` predicate per change, the parser
    keeps it, and ``PlanResult.to_plan_step_hypotheses()``
    materializes :class:`PlanStepHypothesis` instances for the
    detector. When **off**, the schema field is silently dropped at
    parse time and ``to_plan_step_hypotheses()`` returns ``()`` —
    the detector then sees zero hypotheses and short-circuits to
    ``INSUFFICIENT_EVIDENCE`` (legacy DynamicRePlanner reactive path
    remains the backstop). Asymmetric env semantics: empty/whitespace
    = unset = default true; explicit truthy/falsy overrides.

    Independent of ``JARVIS_PLAN_FALSIFICATION_ENABLED`` (the
    detector master flag) so operators can disable hypothesis emission
    without disabling all detector code paths and vice-versa.
    """
    raw = os.environ.get("JARVIS_PLAN_HYPOTHESIS_EMIT_ENABLED", "")
    raw = raw.strip().lower()
    if raw == "":
        return True
    return raw in _TRUTHY


# ---------------------------------------------------------------------------
# UI-affected classification (feeds Visual VERIFY trigger — D2 decision)
# ---------------------------------------------------------------------------
#
# Design spec: docs/superpowers/specs/2026-04-18-vision-sensor-verify-design.md
#   §VERIFY Extension → Trigger conditions (structured > prose).
#
# Primary authoritative signal = target_files extension classification.
# Prose (plan approach text) is used *only* when the structured signal is
# absent or ambiguous. This keeps deterministic routing anchored on file
# scope and never lets a keyword in a prompt hijack UI-mode detection
# when the actual target files say otherwise (e.g. a backend refactor
# whose description happens to say "layout the migration").

_FRONTEND_EXTENSIONS: frozenset = frozenset({
    ".tsx", ".jsx", ".vue", ".svelte", ".css", ".scss", ".html", ".htm",
})

# Language extensions that constitute an "unambiguous" signal — if a target
# file matches any of these, we have a structured classification and do NOT
# fall through to the prose keyword check. Frontend ∪ backend ∪ shared.
# Deliberately conservative: listed languages must be ones where we trust
# the extension conveys a stable scope signal. Anything unlisted (e.g.
# .md / .yaml / .json / .txt / binary) is treated as ambiguous.
_CLASSIFIABLE_EXTENSIONS: frozenset = frozenset(
    _FRONTEND_EXTENSIONS
    | {
        # Python / Ruby / PHP families
        ".py", ".rb", ".php",
        # Systems + statically typed
        ".go", ".rs", ".java", ".kt", ".scala",
        # JVM / dotnet
        ".cs", ".fs", ".fsx", ".clj", ".cljs", ".cljc",
        # C family
        ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx",
        # Apple platforms
        ".swift", ".m", ".mm",
        # Shared JS/TS (not inherently frontend — treated as classifiable but
        # not frontend-exclusive)
        ".ts", ".js", ".mjs", ".cjs",
        # Other
        ".dart", ".elm", ".ex", ".exs", ".erl", ".hrl",
        ".hs", ".lua", ".pl", ".pm", ".r", ".jl", ".nim", ".zig",
    }
)

# Secondary-fallback keyword set. Only consulted when target_files is empty
# or entirely unclassifiable. Word-boundary + case-insensitive — "restyle"
# as a raw substring does NOT match "style" (would need the full word),
# but typical descriptions mentioning "component" or "layout" do.
_UI_APPROACH_KEYWORD_PATTERN: re.Pattern = re.compile(
    r"\b(UI|render|style|component|viewport|layout)\b",
    re.IGNORECASE,
)


def _ext(path: str) -> str:
    """Return lowercase extension including the dot, or empty string."""
    if not path:
        return ""
    # Normalise path separators so a Windows-style "src\\Button.tsx" works.
    norm = path.replace("\\", "/")
    base = norm.rsplit("/", 1)[-1]
    dot = base.rfind(".")
    if dot < 0 or dot == 0:          # no extension or leading-dot dotfile
        return ""
    return base[dot:].lower()


def classify_ui_affected(
    target_files: Sequence[str],
    approach: str = "",
) -> bool:
    """Deterministically classify whether an op touches the UI surface.

    Decision order (primary wins; secondary only when primary is silent):

    1. **Primary** — any file in ``target_files`` has a frontend extension
       (``.tsx`` / ``.jsx`` / ``.vue`` / ``.svelte`` / ``.css`` / ``.scss`` /
       ``.html`` / ``.htm``) → ``True``.
    2. **Structured-negative** — ``target_files`` is non-empty AND every
       file has a *classifiable* extension (frontend or backend or shared),
       but none is frontend → ``False``. Prose hints are ignored; the
       structured signal is authoritative.
    3. **Secondary fallback** — ``target_files`` is empty OR every entry
       is unclassifiable (e.g. ``.md`` / ``.yaml`` / binaries): scan the
       ``approach`` text for UI keywords (case-insensitive, word-boundary
       regex). Any hit → ``True``.
    4. Else → ``False``.

    Pure function. No filesystem IO. Used both inside ``PlanGenerator``
    (stamps ``PlanResult.ui_affected``) and directly by the Visual VERIFY
    trigger (Task 17) for ops that skipped planning.
    """
    # 1. Primary — frontend extension wins unconditionally.
    for f in target_files:
        if _ext(f) in _FRONTEND_EXTENSIONS:
            return True

    # 2. Structured-negative — if we have any classifiable evidence at
    #    all, trust the structure and ignore the prose.
    if target_files:
        any_classifiable = any(_ext(f) in _CLASSIFIABLE_EXTENSIONS for f in target_files)
        if any_classifiable:
            return False

    # 3. Secondary fallback — prose keyword scan.
    if approach and _UI_APPROACH_KEYWORD_PATTERN.search(approach):
        return True

    # 4. No structured, no prose signal → not UI-affected.
    return False


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
        "planning_duration_s", "skipped", "skip_reason", "ui_affected",
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
        ui_affected: bool = False,
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
        # Stamped deterministically by ``generate_plan`` via
        # ``classify_ui_affected(ctx.target_files, result.approach)``.
        # The parser and skipped_result paths leave this at ``False`` by
        # default; the Visual VERIFY trigger (Task 17) re-runs the same
        # classification on ``ctx.target_files`` directly for ops whose
        # plan was skipped.
        self.ui_affected = ui_affected

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
                expected = str(change.get("expected_outcome", "")).strip()
                expected_line = (
                    f"     Expected outcome: {expected}\n" if expected else ""
                )
                parts.append(
                    f"  {i}. `{fp}` [{ct}, {scope}]{dep_str}\n"
                    f"     {desc}\n"
                    f"{expected_line}"
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

    def to_plan_step_hypotheses(
        self,
        *,
        emit_enabled: Optional[bool] = None,
    ) -> tuple:
        """Materialize :class:`PlanStepHypothesis` instances from
        ``ordered_changes``.

        Consumers (orchestrator wire-up at Slice 4) feed the result
        into :func:`detect_falsification`. Returns an empty tuple
        when planning was skipped, when no changes exist, or when
        the ``JARVIS_PLAN_HYPOTHESIS_EMIT_ENABLED`` sub-flag is
        explicitly disabled. Defers per-entry construction to
        Slice 1's :func:`pair_plan_step_with_hypothesis`
        convenience constructor so we never duplicate normalization.

        Args:
          emit_enabled: explicit override (test injection). Defaults
            to env via :func:`_plan_hypothesis_emit_enabled`.

        Returns:
          Tuple[PlanStepHypothesis, ...] — empty when emission is
          off / plan skipped / no changes.
        """
        is_enabled = (
            emit_enabled
            if emit_enabled is not None
            else _plan_hypothesis_emit_enabled()
        )
        if not is_enabled:
            return ()
        if self.skipped or not self.ordered_changes:
            return ()
        out: List[PlanStepHypothesis] = []
        for idx, change in enumerate(self.ordered_changes):
            if not isinstance(change, dict):
                continue
            try:
                hyp = pair_plan_step_with_hypothesis(
                    step_index=idx,
                    ordered_change=change,
                    expected_outcome=str(
                        change.get("expected_outcome", "") or ""
                    ),
                )
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.debug(
                    "[PlanGenerator] to_plan_step_hypotheses skipped "
                    "change idx=%d: %s", idx, exc,
                )
                continue
            out.append(hyp)
        return tuple(out)


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
        if skip_reason and skip_reason.startswith("trivial_op:"):
            # Priority C consumer wiring — probe the trivial-op
            # assumption before silently skipping PLAN. The legacy
            # heuristic (1 file + short description) misclassified
            # large-file ops as trivial in soak #3, which silently
            # disabled Slice 2.3 claim capture for the whole op.
            # The probe falsifies the heuristic when a target file
            # is non-trivially sized; if REFUTED, force PLAN to run.
            try:
                from backend.core.ouroboros.governance.verification.hypothesis_consumers import (
                    probe_trivial_op_assumption,
                )
                _verdict = await probe_trivial_op_assumption(
                    target_files=ctx.target_files,
                    op_id=ctx.op_id,
                    description=ctx.description or "",
                )
                if not _verdict.treat_as_trivial:
                    logger.info(
                        "[PlanGenerator] trivial-op assumption REFUTED "
                        "by HypothesisProbe — forcing PLAN op=%s "
                        "post=%.3f reason=%s",
                        ctx.op_id,
                        _verdict.confidence_posterior,
                        _verdict.observation_summary[:80],
                    )
                    skip_reason = ""  # override the skip
            except Exception:  # noqa: BLE001 — defensive
                logger.debug(
                    "[PlanGenerator] trivial-op probe failed; legacy "
                    "heuristic stands op=%s", ctx.op_id, exc_info=True,
                )
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

            # Stamp the UI-affected classification from the structured
            # target_files signal (primary) + approach prose (secondary
            # fallback). Feeds the Visual VERIFY trigger downstream.
            result.ui_affected = classify_ui_affected(
                ctx.target_files, result.approach,
            )

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
      "estimated_scope": "<small|medium|large>",
      "expected_outcome": "<falsifiable predicate this step will satisfy>"
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
- `expected_outcome` is a **falsifiable predicate** — a single concrete check the downstream system can use to detect when this step did NOT land as planned. Good predicates are checkable without judgment ("file `auth.py` exists and defines `login(request) -> bool`", "the function `compute_total` returns the sum of its inputs", "the migration adds a non-null column `email` to `users`"). Bad predicates are vague ("auth works", "looks good", "improves performance"). One clear sentence; no hedging.
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

        # Normalize each change entry. ``expected_outcome`` is gated
        # by the JARVIS_PLAN_HYPOTHESIS_EMIT_ENABLED sub-flag — when
        # off, we silently drop any field the model emitted (so legacy
        # consumers see the legacy shape) and to_plan_step_hypotheses
        # later returns ``()``. When on, the field is preserved with
        # safe-default empty string for older models that don't
        # populate it yet.
        emit_hypothesis = _plan_hypothesis_emit_enabled()
        normalized_changes: List[Dict[str, Any]] = []
        for change in ordered_changes:
            if not isinstance(change, dict):
                continue
            entry: Dict[str, Any] = {
                "file_path": str(change.get("file_path", "")),
                "change_type": str(change.get("change_type", "modify")),
                "description": str(change.get("description", "")),
                "dependencies": [
                    str(d) for d in change.get("dependencies", [])
                    if isinstance(d, str)
                ],
                "estimated_scope": str(change.get("estimated_scope", "medium")),
            }
            if emit_hypothesis:
                entry["expected_outcome"] = str(
                    change.get("expected_outcome", "") or "",
                )
            normalized_changes.append(entry)

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


# ---------------------------------------------------------------------------
# Slice 5 — Module-owned FlagRegistry seed for the new Slice 3 sub-flag
# ---------------------------------------------------------------------------


def register_flags(registry) -> int:  # noqa: ANN001
    """Register the Slice 3 hypothesis-emit sub-flag. Auto-discovered.
    Returns count."""
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category, FlagSpec, FlagType,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.warning(
            "[PlanGenerator] register_flags degraded: %s", exc,
        )
        return 0
    spec = FlagSpec(
        name="JARVIS_PLAN_HYPOTHESIS_EMIT_ENABLED",
        type=FlagType.BOOL, default=True,
        category=Category.SAFETY,
        source_file=(
            "backend/core/ouroboros/governance/plan_generator.py"
        ),
        example="JARVIS_PLAN_HYPOTHESIS_EMIT_ENABLED=true",
        description=(
            "Sub-flag for plan.1 schema's expected_outcome field. "
            "Independent of JARVIS_PLAN_FALSIFICATION_ENABLED so "
            "operators can toggle hypothesis emission and detection "
            "independently. Default true post Slice 5 graduation."
        ),
    )
    try:
        registry.register(spec)
        return 1
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[PlanGenerator] register_flags spec %s skipped: %s",
            spec.name, exc,
        )
        return 0
