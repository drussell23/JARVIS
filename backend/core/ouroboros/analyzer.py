"""
Code Analyzer Module for Ouroboros
==================================

Provides AST-based code analysis for intelligent code understanding:
- Structural analysis (classes, functions, imports)
- Dependency mapping
- Complexity metrics
- Semantic diff with change impact analysis
- Context extraction for LLM prompts

Author: Trinity System
Version: 1.0.0
"""

from __future__ import annotations

import ast
import asyncio
import difflib
import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union


# =============================================================================
# ENUMS
# =============================================================================

class NodeType(Enum):
    """Types of AST nodes we track."""
    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"
    IMPORT = "import"
    VARIABLE = "variable"
    CONSTANT = "constant"


class ChangeType(Enum):
    """Types of changes detected."""
    ADDED = "added"
    REMOVED = "removed"
    MODIFIED = "modified"
    MOVED = "moved"
    RENAMED = "renamed"


class ImpactLevel(Enum):
    """Impact level of a change."""
    NONE = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class CodeEntity:
    """Represents a code entity (class, function, etc.)."""
    name: str
    node_type: NodeType
    line_start: int
    line_end: int
    signature: str = ""
    docstring: str = ""
    dependencies: Set[str] = field(default_factory=set)
    dependents: Set[str] = field(default_factory=set)
    complexity: int = 0
    source: str = ""

    def get_hash(self) -> str:
        """Get a hash of this entity for comparison."""
        content = f"{self.name}:{self.signature}:{self.source}"
        return hashlib.md5(content.encode()).hexdigest()[:12]


@dataclass
class ASTContext:
    """Context extracted from AST analysis."""
    file_path: Path
    module_name: str
    entities: List[CodeEntity] = field(default_factory=list)
    imports: List[str] = field(default_factory=list)
    global_vars: List[str] = field(default_factory=list)
    total_lines: int = 0
    cyclomatic_complexity: int = 0
    maintainability_index: float = 0.0

    def get_entity(self, name: str) -> Optional[CodeEntity]:
        """Get an entity by name."""
        for entity in self.entities:
            if entity.name == name:
                return entity
        return None

    def get_classes(self) -> List[CodeEntity]:
        """Get all class entities."""
        return [e for e in self.entities if e.node_type == NodeType.CLASS]

    def get_functions(self) -> List[CodeEntity]:
        """Get all function/method entities."""
        return [e for e in self.entities if e.node_type in (NodeType.FUNCTION, NodeType.METHOD)]

    def to_summary(self) -> str:
        """Generate a summary for LLM context."""
        lines = [
            f"# Module: {self.module_name}",
            f"# Lines: {self.total_lines}",
            f"# Complexity: {self.cyclomatic_complexity}",
            "",
            "## Imports",
        ]

        for imp in self.imports[:10]:  # Limit imports
            lines.append(f"- {imp}")

        lines.append("\n## Classes")
        for cls in self.get_classes():
            lines.append(f"- {cls.name}: {cls.signature}")
            if cls.docstring:
                lines.append(f"  {cls.docstring[:100]}...")

        lines.append("\n## Functions")
        for func in self.get_functions()[:20]:  # Limit functions
            lines.append(f"- {func.name}{func.signature}")

        return "\n".join(lines)


@dataclass
class SemanticChange:
    """A semantic change between two versions of code."""
    entity_name: str
    change_type: ChangeType
    old_value: Optional[str] = None
    new_value: Optional[str] = None
    line_old: int = 0
    line_new: int = 0
    description: str = ""


@dataclass
class ChangeImpact:
    """Analysis of the impact of changes."""
    level: ImpactLevel
    affected_entities: List[str] = field(default_factory=list)
    breaking_changes: List[str] = field(default_factory=list)
    suggestions: List[str] = field(default_factory=list)
    risk_score: float = 0.0

    def to_summary(self) -> str:
        """Generate impact summary."""
        lines = [
            f"Impact Level: {self.level.name}",
            f"Risk Score: {self.risk_score:.2f}",
        ]

        if self.breaking_changes:
            lines.append("\nBreaking Changes:")
            for bc in self.breaking_changes:
                lines.append(f"  - {bc}")

        if self.affected_entities:
            lines.append(f"\nAffected Entities: {len(self.affected_entities)}")

        if self.suggestions:
            lines.append("\nSuggestions:")
            for s in self.suggestions:
                lines.append(f"  - {s}")

        return "\n".join(lines)


@dataclass
class SemanticDiff:
    """Complete semantic diff between two versions."""
    changes: List[SemanticChange] = field(default_factory=list)
    impact: Optional[ChangeImpact] = None
    old_context: Optional[ASTContext] = None
    new_context: Optional[ASTContext] = None

    @property
    def has_breaking_changes(self) -> bool:
        """Check if there are any breaking changes."""
        return bool(self.impact and self.impact.breaking_changes)

    @property
    def total_changes(self) -> int:
        """Get total number of changes."""
        return len(self.changes)

    def get_changes_by_type(self, change_type: ChangeType) -> List[SemanticChange]:
        """Get changes of a specific type."""
        return [c for c in self.changes if c.change_type == change_type]

    def to_description(self) -> str:
        """Generate human-readable description of changes."""
        if not self.changes:
            return "No semantic changes detected."

        lines = [f"Total changes: {len(self.changes)}"]

        for change_type in ChangeType:
            changes = self.get_changes_by_type(change_type)
            if changes:
                lines.append(f"\n{change_type.name.title()} ({len(changes)}):")
                for c in changes[:5]:  # Limit per type
                    lines.append(f"  - {c.entity_name}: {c.description}")

        if self.impact:
            lines.append(f"\n{self.impact.to_summary()}")

        return "\n".join(lines)


# =============================================================================
# AST VISITOR
# =============================================================================

class CodeAnalysisVisitor(ast.NodeVisitor):
    """AST visitor for extracting code structure."""

    def __init__(self, source: str):
        self.source = source
        self.source_lines = source.splitlines()
        self.entities: List[CodeEntity] = []
        self.imports: List[str] = []
        self.global_vars: List[str] = []
        self._current_class: Optional[str] = None
        self._complexity = 0

    def visit_Module(self, node: ast.Module) -> None:
        """Visit module."""
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        """Visit import statement."""
        for alias in node.names:
            self.imports.append(alias.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """Visit from...import statement."""
        module = node.module or ""
        for alias in node.names:
            self.imports.append(f"{module}.{alias.name}")

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """Visit class definition."""
        bases = ", ".join(
            ast.unparse(base) if hasattr(ast, 'unparse') else str(base)
            for base in node.bases
        )
        signature = f"({bases})" if bases else ""

        docstring = ast.get_docstring(node) or ""

        # Get source
        source = self._get_source(node)

        entity = CodeEntity(
            name=node.name,
            node_type=NodeType.CLASS,
            line_start=node.lineno,
            line_end=node.end_lineno or node.lineno,
            signature=signature,
            docstring=docstring[:200],
            complexity=self._count_complexity(node),
            source=source,
        )

        self.entities.append(entity)

        # Visit methods
        old_class = self._current_class
        self._current_class = node.name
        self.generic_visit(node)
        self._current_class = old_class

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """Visit function definition."""
        self._handle_function(node, is_async=False)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """Visit async function definition."""
        self._handle_function(node, is_async=True)

    def _handle_function(self, node: Union[ast.FunctionDef, ast.AsyncFunctionDef], is_async: bool) -> None:
        """Handle function or async function."""
        # Build signature
        args = []
        for arg in node.args.args:
            arg_str = arg.arg
            if arg.annotation:
                ann = ast.unparse(arg.annotation) if hasattr(ast, 'unparse') else "..."
                arg_str += f": {ann}"
            args.append(arg_str)

        signature = f"({', '.join(args)})"

        if node.returns:
            ret = ast.unparse(node.returns) if hasattr(ast, 'unparse') else "..."
            signature += f" -> {ret}"

        docstring = ast.get_docstring(node) or ""
        source = self._get_source(node)

        node_type = NodeType.METHOD if self._current_class else NodeType.FUNCTION

        entity = CodeEntity(
            name=node.name,
            node_type=node_type,
            line_start=node.lineno,
            line_end=node.end_lineno or node.lineno,
            signature=signature,
            docstring=docstring[:200],
            complexity=self._count_complexity(node),
            source=source,
        )

        # Track dependencies (function calls)
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                if isinstance(child.func, ast.Name):
                    entity.dependencies.add(child.func.id)
                elif isinstance(child.func, ast.Attribute):
                    entity.dependencies.add(child.func.attr)

        self.entities.append(entity)
        self._complexity += entity.complexity

    def visit_Assign(self, node: ast.Assign) -> None:
        """Visit assignment (track global variables)."""
        if not self._current_class:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.global_vars.append(target.id)

    def _get_source(self, node: ast.AST) -> str:
        """Get source code for a node."""
        if hasattr(node, 'lineno') and hasattr(node, 'end_lineno'):
            start = node.lineno - 1
            end = node.end_lineno or node.lineno
            return "\n".join(self.source_lines[start:end])
        return ""

    def _count_complexity(self, node: ast.AST) -> int:
        """Count cyclomatic complexity of a node."""
        complexity = 1

        for child in ast.walk(node):
            if isinstance(child, (ast.If, ast.While, ast.For, ast.ExceptHandler)):
                complexity += 1
            elif isinstance(child, ast.BoolOp):
                complexity += len(child.values) - 1
            elif isinstance(child, (ast.Assert, ast.Raise)):
                complexity += 1
            elif isinstance(child, ast.comprehension):
                complexity += 1
                if child.ifs:
                    complexity += len(child.ifs)

        return complexity

    @property
    def total_complexity(self) -> int:
        return self._complexity


# =============================================================================
# CODE ANALYZER
# =============================================================================

class CodeAnalyzer:
    """
    Analyzes Python code using AST.

    Provides:
    - Structural analysis
    - Dependency mapping
    - Complexity metrics
    - Context extraction for LLM
    """

    async def analyze(self, source: str, file_path: Optional[Path] = None) -> ASTContext:
        """
        Analyze source code and return context.

        Args:
            source: Python source code
            file_path: Optional file path for module name

        Returns:
            ASTContext with analysis results
        """
        try:
            tree = ast.parse(source)
        except SyntaxError as e:
            # Return minimal context for invalid code
            return ASTContext(
                file_path=file_path or Path("unknown.py"),
                module_name="<invalid>",
                total_lines=len(source.splitlines()),
            )

        visitor = CodeAnalysisVisitor(source)
        visitor.visit(tree)

        # Calculate maintainability index
        # Simplified version of the Halstead/McCabe formula
        lines = len(source.splitlines())
        complexity = visitor.total_complexity
        maintainability = max(0, 171 - 5.2 * (complexity ** 0.5) - 0.23 * complexity - 16.2 * (lines ** 0.5))
        maintainability = maintainability * 100 / 171  # Normalize to 0-100

        module_name = file_path.stem if file_path else "module"

        context = ASTContext(
            file_path=file_path or Path("unknown.py"),
            module_name=module_name,
            entities=visitor.entities,
            imports=visitor.imports,
            global_vars=visitor.global_vars,
            total_lines=lines,
            cyclomatic_complexity=complexity,
            maintainability_index=maintainability,
        )

        # Build dependency graph
        self._build_dependencies(context)

        return context

    async def analyze_file(self, file_path: Path) -> ASTContext:
        """Analyze a file."""
        source = await asyncio.to_thread(file_path.read_text)
        return await self.analyze(source, file_path)

    def _build_dependencies(self, context: ASTContext) -> None:
        """Build bidirectional dependency graph."""
        entity_names = {e.name for e in context.entities}

        for entity in context.entities:
            # Filter to only internal dependencies
            internal_deps = entity.dependencies & entity_names

            for dep_name in internal_deps:
                dep_entity = context.get_entity(dep_name)
                if dep_entity:
                    dep_entity.dependents.add(entity.name)

    async def diff(self, old_source: str, new_source: str, file_path: Optional[Path] = None) -> SemanticDiff:
        """
        Compute semantic diff between two versions of code.

        Goes beyond line-by-line diff to understand structural changes.
        """
        old_context = await self.analyze(old_source, file_path)
        new_context = await self.analyze(new_source, file_path)

        changes: List[SemanticChange] = []

        # Build entity maps
        old_entities = {e.name: e for e in old_context.entities}
        new_entities = {e.name: e for e in new_context.entities}

        old_names = set(old_entities.keys())
        new_names = set(new_entities.keys())

        # Detect added entities
        for name in new_names - old_names:
            entity = new_entities[name]
            changes.append(SemanticChange(
                entity_name=name,
                change_type=ChangeType.ADDED,
                new_value=entity.signature,
                line_new=entity.line_start,
                description=f"Added {entity.node_type.value} '{name}'",
            ))

        # Detect removed entities
        for name in old_names - new_names:
            entity = old_entities[name]
            changes.append(SemanticChange(
                entity_name=name,
                change_type=ChangeType.REMOVED,
                old_value=entity.signature,
                line_old=entity.line_start,
                description=f"Removed {entity.node_type.value} '{name}'",
            ))

        # Detect modified entities
        for name in old_names & new_names:
            old_entity = old_entities[name]
            new_entity = new_entities[name]

            if old_entity.get_hash() != new_entity.get_hash():
                # Determine what changed
                if old_entity.signature != new_entity.signature:
                    desc = f"Signature changed from {old_entity.signature} to {new_entity.signature}"
                else:
                    desc = f"Implementation changed"

                changes.append(SemanticChange(
                    entity_name=name,
                    change_type=ChangeType.MODIFIED,
                    old_value=old_entity.source[:200],
                    new_value=new_entity.source[:200],
                    line_old=old_entity.line_start,
                    line_new=new_entity.line_start,
                    description=desc,
                ))

        # Analyze impact
        impact = self._analyze_impact(changes, old_context, new_context)

        return SemanticDiff(
            changes=changes,
            impact=impact,
            old_context=old_context,
            new_context=new_context,
        )

    def _analyze_impact(
        self,
        changes: List[SemanticChange],
        old_context: ASTContext,
        new_context: ASTContext,
    ) -> ChangeImpact:
        """Analyze the impact of changes."""
        affected = set()
        breaking = []
        suggestions = []

        for change in changes:
            entity_name = change.entity_name

            # Find what depends on this entity
            old_entity = old_context.get_entity(entity_name)
            if old_entity:
                affected.update(old_entity.dependents)

            # Check for breaking changes
            if change.change_type == ChangeType.REMOVED:
                if old_entity and old_entity.dependents:
                    breaking.append(f"Removed '{entity_name}' which is used by {old_entity.dependents}")

            elif change.change_type == ChangeType.MODIFIED:
                if old_entity and change.old_value and change.new_value:
                    # Check for signature changes
                    if "signature changed" in change.description.lower():
                        breaking.append(f"'{entity_name}' signature changed - may break callers")

        # Calculate impact level
        if breaking:
            level = ImpactLevel.CRITICAL
        elif len(affected) > 5:
            level = ImpactLevel.HIGH
        elif affected:
            level = ImpactLevel.MEDIUM
        elif changes:
            level = ImpactLevel.LOW
        else:
            level = ImpactLevel.NONE

        # Calculate risk score
        risk_score = (
            len(breaking) * 0.5 +
            len(affected) * 0.1 +
            len([c for c in changes if c.change_type == ChangeType.REMOVED]) * 0.2 +
            len([c for c in changes if c.change_type == ChangeType.MODIFIED]) * 0.1
        )
        risk_score = min(1.0, risk_score)

        # Generate suggestions
        if breaking:
            suggestions.append("Review breaking changes carefully before committing")
        if len(changes) > 10:
            suggestions.append("Consider splitting large changes into smaller commits")
        if risk_score > 0.5:
            suggestions.append("Add tests for affected functionality")

        return ChangeImpact(
            level=level,
            affected_entities=list(affected),
            breaking_changes=breaking,
            suggestions=suggestions,
            risk_score=risk_score,
        )

    def generate_context_prompt(self, context: ASTContext, focus_entity: Optional[str] = None) -> str:
        """
        Generate an LLM-friendly context prompt from AST analysis.

        Args:
            context: The analyzed AST context
            focus_entity: Optional entity to focus the context on

        Returns:
            Formatted context string for LLM
        """
        lines = [
            "# Code Context",
            f"Module: {context.module_name}",
            f"Lines: {context.total_lines}",
            f"Complexity: {context.cyclomatic_complexity}",
            f"Maintainability: {context.maintainability_index:.1f}/100",
            "",
        ]

        if context.imports:
            lines.append("## Dependencies")
            for imp in context.imports[:15]:
                lines.append(f"- {imp}")
            lines.append("")

        if focus_entity:
            entity = context.get_entity(focus_entity)
            if entity:
                lines.append(f"## Focus: {entity.name}")
                lines.append(f"Type: {entity.node_type.value}")
                lines.append(f"Signature: {entity.signature}")
                lines.append(f"Lines: {entity.line_start}-{entity.line_end}")
                if entity.dependencies:
                    lines.append(f"Uses: {', '.join(list(entity.dependencies)[:10])}")
                if entity.dependents:
                    lines.append(f"Used by: {', '.join(list(entity.dependents)[:10])}")
                lines.append("")

        lines.append("## Structure")
        for cls in context.get_classes():
            lines.append(f"class {cls.name}{cls.signature}:")
            # Find methods of this class
            for func in context.get_functions():
                if func.node_type == NodeType.METHOD:
                    # Check if method belongs to this class (simple heuristic)
                    if cls.line_start < func.line_start < cls.line_end:
                        lines.append(f"    def {func.name}{func.signature}")

        # Standalone functions
        standalone = [f for f in context.get_functions() if f.node_type == NodeType.FUNCTION]
        if standalone:
            lines.append("\n## Functions")
            for func in standalone[:20]:
                lines.append(f"def {func.name}{func.signature}")

        return "\n".join(lines)
