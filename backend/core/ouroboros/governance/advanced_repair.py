"""
Advanced Repair Techniques — Research-grade APR from 3 academic papers.

1. Hierarchical Fault Localization (Agentless + RepoRepair)
   3-stage narrowing: file → function → line. Reduces prompt size 10x.

2. Slow/Fast Thinking Router (SIADAFIX)
   Classify complexity, route to fast (simple fix) or slow (deep reasoning).

3. Documentation-Augmented Repair (RepoRepair)
   Auto-generate docs for target code FIRST, then use docs as auxiliary
   knowledge to guide localization and repair.

Boundary Principle:
  Deterministic: AST analysis for localization, complexity scoring,
  doc generation via AST introspection. No model inference in the
  preprocessing stages — intelligence is applied only at the repair step.
"""
from __future__ import annotations

import ast
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# 1. HIERARCHICAL FAULT LOCALIZATION (Agentless + RepoRepair)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class FaultLocation:
    """A progressively narrowed fault location."""
    file_path: str
    function_name: str = ""
    start_line: int = 0
    end_line: int = 0
    context_snippet: str = ""  # Just the relevant lines, not the whole file
    confidence: float = 0.0


class HierarchicalFaultLocalizer:
    """3-stage fault localization: file → function → line.

    Instead of sending entire target files to the model (which can be
    thousands of lines), this localizer narrows the search space via AST
    analysis before the model sees anything.

    Stage 1 (file): Already done by IntentEnvelope.target_files
    Stage 2 (function): AST analysis to find the most likely function
    Stage 3 (line): Extract just the relevant lines for the model

    The model receives ~50 lines of focused context instead of ~2000.
    """

    def __init__(self, project_root: Path) -> None:
        self._project_root = project_root

    def localize(
        self,
        target_files: Tuple[str, ...],
        error_message: str = "",
        stack_trace: str = "",
    ) -> List[FaultLocation]:
        """Hierarchically localize faults in target files.

        Uses error messages and stack traces to narrow from file → function → lines.
        All via AST + regex — no model inference.
        """
        locations = []

        for rel_path in target_files:
            if not rel_path.endswith(".py"):
                continue

            full_path = self._project_root / rel_path
            if not full_path.exists():
                continue

            try:
                source = full_path.read_text()
                tree = ast.parse(source)
                lines = source.split("\n")
            except (SyntaxError, FileNotFoundError, UnicodeDecodeError):
                continue

            # Stage 2: Narrow to function
            candidate_functions = self._find_candidate_functions(
                tree, lines, error_message, stack_trace, rel_path,
            )

            if candidate_functions:
                for func_name, start, end, confidence in candidate_functions:
                    # Stage 3: Extract focused context (±5 lines around function)
                    ctx_start = max(0, start - 1)
                    ctx_end = min(len(lines), end + 1)
                    snippet = "\n".join(
                        f"{i + ctx_start + 1:4d} | {line}"
                        for i, line in enumerate(lines[ctx_start:ctx_end])
                    )
                    locations.append(FaultLocation(
                        file_path=rel_path,
                        function_name=func_name,
                        start_line=start,
                        end_line=end,
                        context_snippet=snippet,
                        confidence=confidence,
                    ))
            else:
                # Fallback: use the whole file but mark it as low confidence
                locations.append(FaultLocation(
                    file_path=rel_path,
                    confidence=0.3,
                ))

        # Sort by confidence (highest first)
        locations.sort(key=lambda l: -l.confidence)
        return locations[:5]  # Top 5 locations

    def _find_candidate_functions(
        self,
        tree: ast.Module,
        lines: List[str],
        error_message: str,
        stack_trace: str,
        file_path: str,
    ) -> List[Tuple[str, int, int, float]]:
        """Find functions most likely to contain the fault.

        Returns [(function_name, start_line, end_line, confidence)]
        """
        import re
        candidates = []

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue

            name = node.name
            start = node.lineno
            end = node.end_lineno or start
            confidence = 0.0

            # Check if function name appears in stack trace
            if stack_trace and name in stack_trace:
                confidence += 0.5

            # Check if function name appears in error message
            if error_message and name in error_message:
                confidence += 0.3

            # Check if line numbers from stack trace are in this function
            if stack_trace:
                trace_lines = re.findall(
                    rf'{re.escape(file_path)}.*?line (\d+)',
                    stack_trace,
                )
                for tl in trace_lines:
                    if start <= int(tl) <= end:
                        confidence += 0.6

            # Check function complexity (complex functions are more likely to have bugs)
            branches = sum(
                1 for child in ast.walk(node)
                if isinstance(child, (ast.If, ast.For, ast.While,
                                      ast.ExceptHandler, ast.With))
            )
            if branches > 10:
                confidence += 0.1

            if confidence > 0:
                candidates.append((name, start, end, min(1.0, confidence)))

        return sorted(candidates, key=lambda x: -x[3])

    def format_for_prompt(self, locations: List[FaultLocation]) -> str:
        """Format localized fault locations for the generation prompt.

        Instead of the full file, the model receives focused snippets.
        """
        if not locations:
            return ""

        lines = ["## Fault Localization (hierarchical, AST-narrowed)"]
        for loc in locations:
            if loc.context_snippet:
                lines.append(
                    f"\n### {loc.file_path}::{loc.function_name}() "
                    f"(lines {loc.start_line}-{loc.end_line}, "
                    f"confidence={loc.confidence:.0%})"
                )
                lines.append(f"```python\n{loc.context_snippet}\n```")
            else:
                lines.append(
                    f"\n### {loc.file_path} (whole file, "
                    f"confidence={loc.confidence:.0%})"
                )

        lines.append(
            "\nFocus your fix on the identified locations. "
            "The most likely fault is in the highest-confidence function."
        )
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════
# 2. SLOW/FAST THINKING ROUTER (SIADAFIX)
# ═══════════════════════════════════════════════════════════════════════════

class ThinkingDepth:
    """Enum-like for thinking depth levels."""
    FAST = "fast"        # Simple fix — low token budget, no deep reasoning
    STANDARD = "standard"  # Normal operation — default pipeline
    SLOW = "slow"        # Complex fix — high token budget, extended reasoning


@dataclass
class ThinkingRouteDecision:
    """Decision about which thinking depth to use."""
    depth: str
    reason: str
    max_tokens_multiplier: float  # 0.5x for fast, 1.0x for standard, 2.0x for slow
    model_tier_override: str = ""  # "" = use default, "tier0" = force Doubleword


class SlowFastThinkingRouter:
    """Route operations to fast or slow thinking based on complexity.

    Simple fixes (typo, missing import, one-line change) get fast thinking:
    smaller prompt, lower token budget, possibly cheaper model.

    Complex fixes (architecture change, cross-file refactor, security fix)
    get slow thinking: full prompt, high token budget, best model.

    Deterministic classification via AST + heuristics. No model inference
    in the routing decision itself.
    """

    # Complexity indicators for fast thinking (simple fixes)
    _FAST_INDICATORS = frozenset({
        "missing import", "typo", "rename", "unused variable",
        "missing return", "indentation", "whitespace", "comment",
        "docstring", "type hint", "annotation",
    })

    # Complexity indicators for slow thinking (deep reasoning)
    _SLOW_INDICATORS = frozenset({
        "security", "vulnerability", "injection", "authentication",
        "architecture", "refactor", "redesign", "migration",
        "concurrency", "race condition", "deadlock", "memory leak",
        "cross-repo", "multi-file", "breaking change",
    })

    @classmethod
    def route(
        cls,
        description: str,
        target_files: Tuple[str, ...],
        error_pattern: str = "",
        file_count: int = 0,
    ) -> ThinkingRouteDecision:
        """Determine thinking depth for an operation. Deterministic."""
        desc_lower = description.lower()
        error_lower = error_pattern.lower()
        combined = f"{desc_lower} {error_lower}"

        # Check fast indicators
        fast_matches = sum(1 for ind in cls._FAST_INDICATORS if ind in combined)
        slow_matches = sum(1 for ind in cls._SLOW_INDICATORS if ind in combined)

        # File count heuristic
        effective_files = file_count or len(target_files)

        if fast_matches > 0 and slow_matches == 0 and effective_files <= 2:
            return ThinkingRouteDecision(
                depth=ThinkingDepth.FAST,
                reason=f"Simple fix detected ({fast_matches} fast indicators, "
                       f"{effective_files} files)",
                max_tokens_multiplier=0.5,
            )

        if slow_matches > 0 or effective_files > 5:
            return ThinkingRouteDecision(
                depth=ThinkingDepth.SLOW,
                reason=f"Complex operation ({slow_matches} slow indicators, "
                       f"{effective_files} files)",
                max_tokens_multiplier=2.0,
                model_tier_override="tier0" if slow_matches >= 2 else "",
            )

        return ThinkingRouteDecision(
            depth=ThinkingDepth.STANDARD,
            reason="Standard complexity",
            max_tokens_multiplier=1.0,
        )

    @staticmethod
    def format_for_prompt(decision: ThinkingRouteDecision) -> str:
        """Inject thinking depth guidance into the prompt."""
        if decision.depth == ThinkingDepth.FAST:
            return (
                "\n## FAST MODE: This is a simple fix.\n"
                "Be concise. Fix the specific issue. "
                "Do not refactor surrounding code. "
                "Minimal changes only.\n"
            )
        elif decision.depth == ThinkingDepth.SLOW:
            return (
                "\n## DEEP REASONING MODE: This is a complex operation.\n"
                "Think step by step. Consider edge cases, security implications, "
                "and cross-file dependencies. Explain your reasoning. "
                "Generate comprehensive tests.\n"
            )
        return ""


# ═══════════════════════════════════════════════════════════════════════════
# 3. DOCUMENTATION-AUGMENTED REPAIR (RepoRepair)
# ═══════════════════════════════════════════════════════════════════════════

class DocAugmentedRepair:
    """Generate code documentation FIRST, then use it to guide repair.

    Before sending code to the model for repair, this module generates
    a structured documentation summary of the target functions via AST
    introspection. This documentation becomes auxiliary knowledge that
    helps the model understand what the code is SUPPOSED to do, not
    just what it currently does.

    The model receives:
      1. Auto-generated docs (what the code should do)
      2. Localized fault snippets (what's broken)
      3. Error context (how it's broken)

    This is fundamentally different from just sending the raw code.
    """

    def __init__(self, project_root: Path) -> None:
        self._project_root = project_root

    def generate_docs_for_repair(
        self, target_files: Tuple[str, ...],
    ) -> str:
        """Auto-generate documentation for target files as repair context.

        Uses AST to extract: module purpose, class hierarchy, function
        signatures with docstrings, import dependencies, and call graph.
        All deterministic — no model inference.
        """
        docs_sections = []

        for rel_path in target_files:
            if not rel_path.endswith(".py"):
                continue

            full_path = self._project_root / rel_path
            if not full_path.exists():
                continue

            doc = self._document_file(full_path, rel_path)
            if doc:
                docs_sections.append(doc)

        if not docs_sections:
            return ""

        return (
            "## Auto-Generated Code Documentation (repair context)\n"
            "Use this documentation to understand what the code is "
            "SUPPOSED to do. Fix the code to match these specifications.\n\n"
            + "\n\n".join(docs_sections)
        )

    def _document_file(self, file_path: Path, rel_path: str) -> str:
        """Generate documentation for one file via AST."""
        try:
            source = file_path.read_text()
            tree = ast.parse(source)
        except (SyntaxError, FileNotFoundError, UnicodeDecodeError):
            return ""

        lines = []
        lines.append(f"### {rel_path}")

        # Module docstring
        module_doc = ast.get_docstring(tree)
        if module_doc:
            lines.append(f"**Purpose:** {module_doc.split(chr(10))[0]}")

        # Imports
        imports = []
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    imports.append(f"{module}.{alias.name}")
        if imports:
            lines.append(f"**Dependencies:** {', '.join(imports[:10])}")

        # Classes
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef):
                class_doc = ast.get_docstring(node)
                bases = [
                    getattr(b, "id", getattr(b, "attr", "?"))
                    for b in node.bases
                ]
                lines.append(
                    f"\n**class {node.name}**"
                    f"({', '.join(bases)})" if bases else f"\n**class {node.name}**"
                )
                if class_doc:
                    lines.append(f"  {class_doc.split(chr(10))[0]}")

                # Methods
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if item.name.startswith("_") and item.name != "__init__":
                            continue
                        self._document_function(item, lines, indent=2)

        # Top-level functions
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("_"):
                    continue
                self._document_function(node, lines, indent=0)

        return "\n".join(lines) if len(lines) > 2 else ""

    @staticmethod
    def _document_function(
        node: ast.FunctionDef, lines: List[str], indent: int = 0,
    ) -> None:
        """Document a single function from its AST node."""
        prefix = "  " * indent
        is_async = isinstance(node, ast.AsyncFunctionDef)
        async_prefix = "async " if is_async else ""

        # Build signature
        args = []
        for arg in node.args.args:
            name = arg.arg
            if name == "self" or name == "cls":
                continue
            annotation = ""
            if arg.annotation:
                annotation = f": {ast.dump(arg.annotation)}"
            args.append(f"{name}{annotation}")

        sig = f"{async_prefix}def {node.name}({', '.join(args)})"

        # Return annotation
        if node.returns:
            sig += f" -> {ast.dump(node.returns)}"

        lines.append(f"{prefix}- `{sig}`")

        # Docstring (first line only)
        doc = ast.get_docstring(node)
        if doc:
            lines.append(f"{prefix}  {doc.split(chr(10))[0]}")
