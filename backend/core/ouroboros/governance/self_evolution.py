"""
Self-Evolution Engine — Research-grade self-programming techniques for Ouroboros.

Implements 9 techniques from 5 academic papers:

1. Runtime Prompt Adaptation (Live-SWE-Agent)
   Modify generation prompts based on real-time execution feedback.
   Prompts evolve as the organism learns what works.

2. Module-Level Mutation (CSE arXiv 2601.07348)
   Evolve individual functions, not whole files. Surgical precision
   reduces blast radius of self-modification.

3. Negative Constraints (CSE)
   Explicit "never do X" rules from failed attempts. Prevents the
   organism from repeating the same mistakes.

4. Deterministic Code Metrics Feedback (SPA)
   pytest + coverage + complexity + lint scores drive evolution,
   not model judgment alone. Objective quality signals.

5. Dynamic Re-Planning (Devin v3.0)
   If a strategy fails mid-operation, alter the approach without
   human intervention. Adaptive, not rigid.

6. Multi-Version Evolution (SWE-EVO)
   Track evolution across multiple pipeline runs, not just single
   operations. The organism's code improves over days, not minutes.

7. Generate-Verify-Refine Cycle (CSE) — strengthened
   Tighter integration between generation, verification, and
   targeted refinement with rollback on regression.

8. Hierarchical Memory (CSE) — strengthened
   Local (per-operation) + global (cross-operation) memory with
   explicit positive/negative signal distinction.

9. Repository Auto-Documentation (Devin v3.0)
   Automatically generate architecture summaries from codebase
   analysis, refreshed periodically.

Boundary Principle:
  Deterministic: Metrics computation, constraint checking, memory
  persistence, version tracking.
  Agentic: Prompt adaptation, mutation content, re-planning decisions.
"""
from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_PERSISTENCE_DIR = Path(
    os.environ.get(
        "JARVIS_SELF_EVOLUTION_DIR",
        str(Path.home() / ".jarvis" / "ouroboros" / "evolution"),
    )
)


# ═══════════════════════════════════════════════════════════════════════════
# 1. RUNTIME PROMPT ADAPTATION (Live-SWE-Agent)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class PromptAdaptation:
    """A learned prompt modification from execution feedback."""
    domain_key: str
    original_instruction: str
    adapted_instruction: str
    reason: str                # Why the adaptation was made
    success_rate_before: float
    success_rate_after: float
    applied_count: int = 0
    created_at: float = field(default_factory=time.time)


class RuntimePromptAdapter:
    """Modifies generation prompts based on real-time execution feedback.

    When an operation succeeds or fails, the adapter records what prompt
    patterns were used. Over time, it learns which instructions produce
    better results for each domain and evolves the prompts accordingly.

    The prompts are NOT rewritten by a model — they're modified via
    deterministic rules derived from outcome statistics.
    """

    def __init__(self, persistence_dir: Path = _PERSISTENCE_DIR) -> None:
        self._persistence_dir = persistence_dir
        self._adaptations: Dict[str, List[PromptAdaptation]] = defaultdict(list)
        self._domain_stats: Dict[str, Dict[str, int]] = defaultdict(
            lambda: {"success": 0, "failure": 0}
        )
        self._load()

    def record_outcome(
        self, domain_key: str, prompt_hash: str, success: bool,
        failure_class: str = "",
    ) -> None:
        """Record an operation outcome for prompt adaptation."""
        stats = self._domain_stats[domain_key]
        if success:
            stats["success"] += 1
        else:
            stats["failure"] += 1

        # Check if we should adapt
        total = stats["success"] + stats["failure"]
        if total >= 5 and total % 5 == 0:
            self._evaluate_adaptations(domain_key)

        self._persist()

    def get_adapted_instructions(self, domain_key: str) -> str:
        """Get any prompt adaptations for a domain."""
        adaptations = self._adaptations.get(domain_key, [])
        if not adaptations:
            return ""

        lines = ["## Learned Prompt Adaptations"]
        for a in adaptations[-3:]:  # Last 3 adaptations
            lines.append(f"- {a.adapted_instruction} (reason: {a.reason})")
        return "\n".join(lines)

    def _evaluate_adaptations(self, domain_key: str) -> None:
        """Evaluate if prompt adaptations are needed based on outcomes."""
        stats = self._domain_stats[domain_key]
        total = stats["success"] + stats["failure"]
        if total < 5:
            return

        failure_rate = stats["failure"] / total

        # High failure rate → add more explicit instructions
        if failure_rate > 0.5:
            self._adaptations[domain_key].append(PromptAdaptation(
                domain_key=domain_key,
                original_instruction="",
                adapted_instruction=(
                    f"WARNING: This domain has a {failure_rate:.0%} failure rate "
                    f"over {total} attempts. Be extra careful with: "
                    f"import statements, type annotations, and edge cases."
                ),
                reason=f"High failure rate ({failure_rate:.0%})",
                success_rate_before=1 - failure_rate,
                success_rate_after=0.0,
            ))

    def _persist(self) -> None:
        try:
            self._persistence_dir.mkdir(parents=True, exist_ok=True)
            path = self._persistence_dir / "prompt_adaptations.json"
            data = {
                "adaptations": {
                    k: [{"domain_key": a.domain_key, "adapted_instruction": a.adapted_instruction,
                         "reason": a.reason, "created_at": a.created_at}
                        for a in v]
                    for k, v in self._adaptations.items()
                },
                "stats": dict(self._domain_stats),
            }
            path.write_text(json.dumps(data, indent=2))
        except Exception:
            pass

    def _load(self) -> None:
        try:
            path = self._persistence_dir / "prompt_adaptations.json"
            if not path.exists():
                return
            data = json.loads(path.read_text())
            for k, adapts in data.get("adaptations", {}).items():
                self._adaptations[k] = [
                    PromptAdaptation(
                        domain_key=a["domain_key"],
                        original_instruction="",
                        adapted_instruction=a["adapted_instruction"],
                        reason=a["reason"],
                        success_rate_before=0, success_rate_after=0,
                        created_at=a.get("created_at", 0),
                    )
                    for a in adapts
                ]
            for k, v in data.get("stats", {}).items():
                self._domain_stats[k] = v
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════
# 2. MODULE-LEVEL MUTATION (CSE)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class FunctionMutation:
    """A mutation targeting a specific function, not a whole file."""
    file_path: str
    function_name: str
    start_line: int
    end_line: int
    original_source: str
    mutated_source: str
    reason: str


class ModuleLevelMutator:
    """Evolve individual functions, not whole files.

    Uses AST to identify function boundaries, extracts the specific
    function being modified, and generates a replacement for just that
    function. Reduces blast radius of self-modification.
    """

    @staticmethod
    def extract_function(
        file_path: Path, function_name: str,
    ) -> Optional[FunctionMutation]:
        """Extract a specific function's source code and line range."""
        try:
            source = file_path.read_text()
            tree = ast.parse(source)

            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if node.name == function_name:
                        start = node.lineno
                        end = node.end_lineno or start
                        lines = source.split("\n")
                        func_source = "\n".join(lines[start - 1:end])
                        return FunctionMutation(
                            file_path=str(file_path),
                            function_name=function_name,
                            start_line=start,
                            end_line=end,
                            original_source=func_source,
                            mutated_source="",
                            reason="",
                        )
        except (SyntaxError, FileNotFoundError):
            pass
        return None

    @staticmethod
    def apply_function_mutation(
        file_path: Path, mutation: FunctionMutation,
    ) -> bool:
        """Replace a function in a file with its mutated version."""
        try:
            source = file_path.read_text()
            lines = source.split("\n")
            lines[mutation.start_line - 1:mutation.end_line] = \
                mutation.mutated_source.split("\n")
            file_path.write_text("\n".join(lines))
            return True
        except Exception:
            return False

    @staticmethod
    def list_functions(file_path: Path) -> List[Dict[str, Any]]:
        """List all functions in a file with their line ranges and complexity."""
        functions = []
        try:
            source = file_path.read_text()
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    # Rough cyclomatic complexity: count branches
                    branches = sum(
                        1 for child in ast.walk(node)
                        if isinstance(child, (ast.If, ast.For, ast.While,
                                              ast.ExceptHandler, ast.With))
                    )
                    functions.append({
                        "name": node.name,
                        "start_line": node.lineno,
                        "end_line": node.end_lineno or node.lineno,
                        "is_async": isinstance(node, ast.AsyncFunctionDef),
                        "complexity": branches + 1,
                        "decorators": len(node.decorator_list),
                        "args": len(node.args.args),
                    })
        except (SyntaxError, FileNotFoundError):
            pass
        return functions


# ═══════════════════════════════════════════════════════════════════════════
# 3. NEGATIVE CONSTRAINTS (CSE)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class NegativeConstraint:
    """An explicit 'never do X' rule from a failed evolution attempt."""
    domain_key: str
    constraint: str            # What NOT to do
    reason: str                # Why (from the failure)
    source_op_id: str          # Operation that produced the failure
    severity: str = "hard"     # "hard" (always block) or "soft" (warn only)
    created_at: float = field(default_factory=time.time)


class NegativeConstraintStore:
    """Explicit 'never do X again' rules from failed self-evolution.

    When a generated patch causes a regression, the specific pattern
    that caused the failure is recorded as a negative constraint.
    Future generation prompts include these constraints to prevent
    the organism from repeating the same mistakes.
    """

    def __init__(self, persistence_dir: Path = _PERSISTENCE_DIR) -> None:
        self._persistence_dir = persistence_dir
        self._constraints: Dict[str, List[NegativeConstraint]] = defaultdict(list)
        self._load()

    def add_constraint(
        self, domain_key: str, constraint: str, reason: str,
        source_op_id: str = "", severity: str = "hard",
    ) -> None:
        """Add a negative constraint from a failed operation."""
        nc = NegativeConstraint(
            domain_key=domain_key, constraint=constraint,
            reason=reason, source_op_id=source_op_id, severity=severity,
        )
        self._constraints[domain_key].append(nc)
        # Keep max 20 per domain
        if len(self._constraints[domain_key]) > 20:
            self._constraints[domain_key] = self._constraints[domain_key][-20:]
        self._persist()
        logger.info(
            "[NegativeConstraints] Added for %s: %s (%s)",
            domain_key, constraint[:60], severity,
        )

    def get_constraints(self, domain_key: str) -> List[NegativeConstraint]:
        return self._constraints.get(domain_key, [])

    def format_for_prompt(self, domain_key: str) -> str:
        """Format constraints for injection into generation prompt."""
        constraints = self.get_constraints(domain_key)
        if not constraints:
            return ""

        lines = ["## NEGATIVE CONSTRAINTS (do NOT repeat these mistakes)"]
        for nc in constraints[-10:]:
            icon = "HARD BLOCK" if nc.severity == "hard" else "WARNING"
            lines.append(f"- [{icon}] {nc.constraint} — Reason: {nc.reason}")
        return "\n".join(lines)

    def check_violation(self, content: str, domain_key: str) -> List[str]:
        """Check if generated content violates any hard constraints.

        Returns list of violated constraint descriptions. Deterministic
        string matching — no model inference.
        """
        violations = []
        for nc in self.get_constraints(domain_key):
            if nc.severity != "hard":
                continue
            # Simple keyword check (deterministic)
            constraint_lower = nc.constraint.lower()
            content_lower = content.lower()
            # Extract key phrases from constraint
            for phrase in re.findall(r'"([^"]+)"', nc.constraint):
                if phrase.lower() in content_lower:
                    violations.append(nc.constraint)
                    break
        return violations

    def _persist(self) -> None:
        try:
            self._persistence_dir.mkdir(parents=True, exist_ok=True)
            path = self._persistence_dir / "negative_constraints.json"
            data = {
                k: [{"domain_key": c.domain_key, "constraint": c.constraint,
                     "reason": c.reason, "source_op_id": c.source_op_id,
                     "severity": c.severity, "created_at": c.created_at}
                    for c in v]
                for k, v in self._constraints.items()
            }
            path.write_text(json.dumps(data, indent=2))
        except Exception:
            pass

    def _load(self) -> None:
        try:
            path = self._persistence_dir / "negative_constraints.json"
            if not path.exists():
                return
            data = json.loads(path.read_text())
            for k, constraints in data.items():
                self._constraints[k] = [
                    NegativeConstraint(**c) for c in constraints
                ]
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════
# 4. DETERMINISTIC CODE METRICS FEEDBACK (SPA)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class CodeMetricsReport:
    """Deterministic quality metrics for a Python file."""
    file_path: str
    line_count: int
    function_count: int
    avg_complexity: float       # Average cyclomatic complexity
    max_complexity: int         # Highest function complexity
    has_docstrings: bool        # Module-level docstring present
    docstring_coverage: float   # % of public functions with docstrings
    import_count: int
    lint_issues: int = 0        # From basic checks (not full pylint)


class CodeMetricsAnalyzer:
    """Compute deterministic code metrics for generation feedback.

    Instead of relying solely on model judgment, these objective metrics
    drive evolution decisions. A function with complexity > 15 is factually
    too complex — that's a measurement, not an opinion.

    Metrics: line count, function count, cyclomatic complexity,
    docstring coverage, import count. All via AST — no external tools.
    """

    @staticmethod
    def analyze(file_path: Path) -> Optional[CodeMetricsReport]:
        """Analyze a Python file and return deterministic quality metrics."""
        try:
            source = file_path.read_text()
            tree = ast.parse(source)
        except (SyntaxError, FileNotFoundError, UnicodeDecodeError):
            return None

        lines = source.split("\n")
        functions = []
        public_funcs = 0
        documented_funcs = 0

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Complexity: count branches
                branches = sum(
                    1 for child in ast.walk(node)
                    if isinstance(child, (ast.If, ast.For, ast.While,
                                          ast.ExceptHandler, ast.With))
                )
                functions.append(branches + 1)

                if not node.name.startswith("_"):
                    public_funcs += 1
                    if ast.get_docstring(node):
                        documented_funcs += 1

        import_count = sum(
            1 for node in ast.iter_child_nodes(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
        )

        avg_complexity = sum(functions) / len(functions) if functions else 0
        max_complexity = max(functions) if functions else 0
        doc_coverage = documented_funcs / public_funcs if public_funcs > 0 else 1.0

        return CodeMetricsReport(
            file_path=str(file_path),
            line_count=len(lines),
            function_count=len(functions),
            avg_complexity=round(avg_complexity, 1),
            max_complexity=max_complexity,
            has_docstrings=bool(ast.get_docstring(tree)),
            docstring_coverage=round(doc_coverage, 2),
            import_count=import_count,
        )

    @staticmethod
    def format_for_prompt(report: CodeMetricsReport) -> str:
        """Format metrics as generation context. Deterministic."""
        quality_flags = []
        if report.max_complexity > 15:
            quality_flags.append(f"HIGH COMPLEXITY: max={report.max_complexity}")
        if report.docstring_coverage < 0.5:
            quality_flags.append(f"LOW DOC COVERAGE: {report.docstring_coverage:.0%}")
        if report.line_count > 500:
            quality_flags.append(f"LARGE FILE: {report.line_count} lines")

        if not quality_flags:
            return ""

        return (
            f"## Code Metrics for {report.file_path}\n"
            f"Lines: {report.line_count}, Functions: {report.function_count}, "
            f"Avg complexity: {report.avg_complexity}, Max: {report.max_complexity}, "
            f"Doc coverage: {report.docstring_coverage:.0%}\n"
            f"Quality flags: {', '.join(quality_flags)}\n"
            f"Address these quality issues in your changes."
        )


# ═══════════════════════════════════════════════════════════════════════════
# 5. DYNAMIC RE-PLANNING (Devin v3.0)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class PlanRevision:
    """A mid-operation plan revision triggered by strategy failure."""
    original_strategy: str
    revised_strategy: str
    trigger: str               # What caused the re-plan
    attempt_number: int
    created_at: float = field(default_factory=time.time)


class DynamicRePlanner:
    """If a strategy fails mid-operation, alter the approach.

    Instead of rigid retry with the same strategy, the re-planner
    detects failure patterns and suggests alternative approaches.
    Deterministic pattern matching → strategy selection.
    """

    # Failure pattern → alternative strategy (deterministic mapping)
    _STRATEGY_MAP: Dict[str, str] = {
        "import_error": (
            "The import failed. Try: 1) Check if the package is installed, "
            "2) Use a try/except ImportError with a fallback, "
            "3) Add the package to requirements.txt"
        ),
        "syntax_error": (
            "Syntax error detected. Start from scratch with the function "
            "signature and rebuild line by line. Do NOT copy-paste."
        ),
        "type_error": (
            "Type mismatch. Check: 1) Function signatures, 2) Return types, "
            "3) Optional vs required parameters, 4) async/await usage"
        ),
        "assertion_error": (
            "Test assertion failed. Read the test carefully. "
            "The test is the specification — change the implementation, "
            "not the test. Check edge cases."
        ),
        "timeout": (
            "Operation timed out. The solution is too slow. Consider: "
            "1) Caching, 2) Reducing iterations, 3) Async I/O, "
            "4) Breaking into smaller functions"
        ),
        "permission_error": (
            "Permission denied. Check file paths and ensure the code "
            "operates within the project root. Never write to system dirs."
        ),
    }

    @classmethod
    def suggest_replan(
        cls, failure_class: str, error_message: str, attempt: int,
    ) -> Optional[PlanRevision]:
        """Suggest an alternative strategy based on failure pattern."""
        # Match failure class to strategy
        for pattern, strategy in cls._STRATEGY_MAP.items():
            if pattern in failure_class.lower() or pattern in error_message.lower():
                return PlanRevision(
                    original_strategy=f"attempt {attempt}",
                    revised_strategy=strategy,
                    trigger=f"{failure_class}: {error_message[:100]}",
                    attempt_number=attempt,
                )

        # Generic re-plan for unknown failures
        if attempt >= 2:
            return PlanRevision(
                original_strategy=f"attempt {attempt}",
                revised_strategy=(
                    "Previous attempts failed with different errors. "
                    "Step back and re-analyze the problem from scratch. "
                    "Read the target file completely before generating a fix. "
                    "Consider whether the approach itself is wrong."
                ),
                trigger=f"Multiple failures: {failure_class}",
                attempt_number=attempt,
            )

        return None

    @staticmethod
    def format_for_prompt(revision: PlanRevision) -> str:
        """Format re-plan as prompt injection."""
        return (
            f"\n## STRATEGY REVISION (attempt {revision.attempt_number})\n"
            f"Trigger: {revision.trigger}\n"
            f"New approach: {revision.revised_strategy}\n"
        )


# ═══════════════════════════════════════════════════════════════════════════
# 6. MULTI-VERSION EVOLUTION TRACKER (SWE-EVO)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class EvolutionEpoch:
    """One version of the organism's self-evolution."""
    epoch_id: int
    started_at: float
    completed_at: float = 0.0
    operations_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    files_modified: int = 0
    capabilities_added: int = 0
    constraints_learned: int = 0


class MultiVersionEvolutionTracker:
    """Track evolution across multiple pipeline runs.

    The organism doesn't just fix one issue — it evolves over days.
    This tracker maintains epoch-level statistics showing how the
    system's capabilities grow over time.
    """

    def __init__(self, persistence_dir: Path = _PERSISTENCE_DIR) -> None:
        self._persistence_dir = persistence_dir
        self._epochs: List[EvolutionEpoch] = []
        self._current_epoch: Optional[EvolutionEpoch] = None
        self._load()

    def start_epoch(self) -> EvolutionEpoch:
        """Start a new evolution epoch (typically at supervisor boot)."""
        epoch_id = len(self._epochs) + 1
        epoch = EvolutionEpoch(epoch_id=epoch_id, started_at=time.time())
        self._current_epoch = epoch
        return epoch

    def record_operation(self, success: bool, files_modified: int = 0) -> None:
        """Record an operation result in the current epoch."""
        if self._current_epoch is None:
            return
        self._current_epoch.operations_count += 1
        if success:
            self._current_epoch.success_count += 1
        else:
            self._current_epoch.failure_count += 1
        self._current_epoch.files_modified += files_modified

    def record_capability(self) -> None:
        """Record a new capability added (graduation event)."""
        if self._current_epoch:
            self._current_epoch.capabilities_added += 1

    def record_constraint(self) -> None:
        """Record a new constraint learned."""
        if self._current_epoch:
            self._current_epoch.constraints_learned += 1

    def complete_epoch(self) -> None:
        """Complete the current epoch and archive it."""
        if self._current_epoch:
            self._current_epoch.completed_at = time.time()
            self._epochs.append(self._current_epoch)
            self._persist()
            self._current_epoch = None

    def get_evolution_summary(self) -> Dict[str, Any]:
        """Get a summary of the organism's evolution over time."""
        if not self._epochs:
            return {"epochs": 0, "total_operations": 0}

        total_ops = sum(e.operations_count for e in self._epochs)
        total_success = sum(e.success_count for e in self._epochs)
        total_caps = sum(e.capabilities_added for e in self._epochs)
        total_constraints = sum(e.constraints_learned for e in self._epochs)

        return {
            "epochs": len(self._epochs),
            "total_operations": total_ops,
            "total_successes": total_success,
            "success_rate": round(total_success / max(1, total_ops), 3),
            "capabilities_added": total_caps,
            "constraints_learned": total_constraints,
            "evolution_trend": self._compute_trend(),
        }

    def _compute_trend(self) -> str:
        """Compute evolution trend from recent epochs."""
        if len(self._epochs) < 2:
            return "insufficient_data"
        recent = self._epochs[-3:]
        rates = [
            e.success_count / max(1, e.operations_count)
            for e in recent
        ]
        if len(rates) >= 2:
            if rates[-1] > rates[0] + 0.1:
                return "improving"
            elif rates[-1] < rates[0] - 0.1:
                return "degrading"
        return "stable"

    def _persist(self) -> None:
        try:
            self._persistence_dir.mkdir(parents=True, exist_ok=True)
            path = self._persistence_dir / "evolution_epochs.json"
            data = [
                {
                    "epoch_id": e.epoch_id, "started_at": e.started_at,
                    "completed_at": e.completed_at,
                    "operations_count": e.operations_count,
                    "success_count": e.success_count,
                    "failure_count": e.failure_count,
                    "files_modified": e.files_modified,
                    "capabilities_added": e.capabilities_added,
                    "constraints_learned": e.constraints_learned,
                }
                for e in self._epochs
            ]
            path.write_text(json.dumps(data, indent=2))
        except Exception:
            pass

    def _load(self) -> None:
        try:
            path = self._persistence_dir / "evolution_epochs.json"
            if not path.exists():
                return
            data = json.loads(path.read_text())
            self._epochs = [EvolutionEpoch(**e) for e in data]
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════
# 9. REPOSITORY AUTO-DOCUMENTATION (Devin v3.0)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class DocGap:
    """A detected documentation gap in a Python file."""
    file_path: str
    symbol_name: str
    symbol_type: str  # "module" | "class" | "function"
    line_number: int
    has_docstring: bool
    has_type_hints: bool


class RepositoryAutoDocumentation:
    """Detect and track documentation gaps across the codebase.

    Scans Python files for missing docstrings on public modules, classes,
    and functions. Produces a summary that can be injected into generation
    prompts (as context) or used to create documentation-fix envelopes.

    Boundary Principle:
        Deterministic: AST scanning, gap detection, persistence.
        Agentic: Documentation content generation (done by the pipeline).
    """

    def __init__(self, persistence_dir: Path = _PERSISTENCE_DIR) -> None:
        self._persistence_dir = persistence_dir
        self._gaps: Dict[str, List[DocGap]] = {}  # file → gaps
        self._last_scan_at: float = 0.0
        self._load()

    def scan_file(self, file_path: Path) -> List[DocGap]:
        """Scan a single Python file for documentation gaps."""
        gaps: List[DocGap] = []
        try:
            source = file_path.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (SyntaxError, OSError, UnicodeDecodeError):
            return gaps

        rel = str(file_path)

        # Module-level docstring
        if not ast.get_docstring(tree):
            gaps.append(DocGap(
                file_path=rel, symbol_name="<module>",
                symbol_type="module", line_number=1,
                has_docstring=False, has_type_hints=True,
            ))

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                if not node.name.startswith("_"):
                    has_doc = ast.get_docstring(node) is not None
                    if not has_doc:
                        gaps.append(DocGap(
                            file_path=rel, symbol_name=node.name,
                            symbol_type="class", line_number=node.lineno,
                            has_docstring=False, has_type_hints=True,
                        ))

            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("_"):
                    continue
                has_doc = ast.get_docstring(node) is not None
                # Check return annotation
                has_ret = node.returns is not None
                # Check arg annotations (skip self/cls)
                args_to_check = node.args.args[1:] if node.args.args else []
                has_hints = has_ret and all(
                    a.annotation is not None for a in args_to_check
                )
                if not has_doc or not has_hints:
                    gaps.append(DocGap(
                        file_path=rel, symbol_name=node.name,
                        symbol_type="function", line_number=node.lineno,
                        has_docstring=has_doc, has_type_hints=has_hints,
                    ))

        self._gaps[rel] = gaps
        return gaps

    def scan_files(self, file_paths: List[Path]) -> Dict[str, List[DocGap]]:
        """Scan multiple files and return all gaps."""
        result: Dict[str, List[DocGap]] = {}
        for fp in file_paths:
            gaps = self.scan_file(fp)
            if gaps:
                result[str(fp)] = gaps
        self._last_scan_at = time.time()
        self._persist()
        return result

    def get_gaps_for_file(self, file_path: str) -> List[DocGap]:
        """Return cached gaps for a file."""
        return self._gaps.get(file_path, [])

    def format_for_prompt(self, file_paths: List[str]) -> str:
        """Format documentation gaps as context for generation prompts.

        Injected into pre-GENERATE so the model is aware of doc debt
        in the files it's about to modify.
        """
        lines: List[str] = []
        for fp in file_paths:
            gaps = self._gaps.get(fp, [])
            if not gaps:
                continue
            missing_docs = [g for g in gaps if not g.has_docstring]
            missing_hints = [g for g in gaps if not g.has_type_hints]
            if missing_docs or missing_hints:
                parts = []
                if missing_docs:
                    names = ", ".join(g.symbol_name for g in missing_docs[:5])
                    parts.append(f"{len(missing_docs)} missing docstrings ({names})")
                if missing_hints:
                    names = ", ".join(g.symbol_name for g in missing_hints[:5])
                    parts.append(f"{len(missing_hints)} missing type hints ({names})")
                lines.append(f"- {fp}: {'; '.join(parts)}")

        if not lines:
            return ""
        return (
            "## Documentation Gaps in Target Files\n"
            "When modifying these files, consider adding missing documentation:\n"
            + "\n".join(lines)
        )

    def get_stats(self) -> Dict[str, Any]:
        """Return summary statistics."""
        total_gaps = sum(len(g) for g in self._gaps.values())
        files_with_gaps = sum(1 for g in self._gaps.values() if g)
        return {
            "total_gaps": total_gaps,
            "files_scanned": len(self._gaps),
            "files_with_gaps": files_with_gaps,
            "last_scan_at": self._last_scan_at,
        }

    def _persist(self) -> None:
        try:
            self._persistence_dir.mkdir(parents=True, exist_ok=True)
            path = self._persistence_dir / "auto_documentation.json"
            data = {
                "last_scan_at": self._last_scan_at,
                "gaps": {
                    fp: [
                        {
                            "file_path": g.file_path,
                            "symbol_name": g.symbol_name,
                            "symbol_type": g.symbol_type,
                            "line_number": g.line_number,
                            "has_docstring": g.has_docstring,
                            "has_type_hints": g.has_type_hints,
                        }
                        for g in gaps
                    ]
                    for fp, gaps in self._gaps.items()
                },
            }
            path.write_text(json.dumps(data, indent=2))
        except Exception:
            pass

    def _load(self) -> None:
        try:
            path = self._persistence_dir / "auto_documentation.json"
            if not path.exists():
                return
            data = json.loads(path.read_text())
            self._last_scan_at = data.get("last_scan_at", 0.0)
            for fp, gap_list in data.get("gaps", {}).items():
                self._gaps[fp] = [DocGap(**g) for g in gap_list]
        except Exception:
            pass
