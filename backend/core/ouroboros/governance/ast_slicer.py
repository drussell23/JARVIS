"""Surgical AST slicing — Phase 11 P11.1 (refactor extraction).

Lifts ``ASTChunker`` (and its directly-coupled types: ``ChunkType``,
``RelevanceReason``, ``CodeChunk``, ``ChunkPriority``) out of
``backend/core/smart_context.py`` into this shared module so multiple
downstream consumers can compose the same chunking logic without
duplicating it.

## Why this module exists (the §3 audit's headline)

Two systems were about to need the same AST-chunking logic:

  * Existing: ``SmartContextSelector`` (``backend/core/smart_context.py``)
    — uses ``ASTChunker`` to extract relevant chunks from large files
    for the GENERATE prompt.
  * Pending (Phase 11 P11.2): ``read_file`` tool's ``target_symbol``
    parameter — the operator wants surgical extraction surfaced
    directly to Venom tool calls so the model can request a specific
    function instead of pulling whole files.

Building a second AST chunker for ``read_file`` would have duplicated
~300 LOC of tree-walking + chunk-extraction logic. Splitting the
chunker into a shared module + having both callers consume it is
First-Order discipline (per directive 2026-04-27 — "no duplicates").

## What this module ships

  * ``ChunkType`` — enum of chunk kinds (function/method/class/etc.)
  * ``RelevanceReason`` — enum (kept here because ``CodeChunk``
    references it; relevance scoring itself stays in
    ``smart_context.RelevanceScorer``)
  * ``CodeChunk`` — frozen-by-design dataclass for one extracted chunk
  * ``ChunkPriority`` — NamedTuple for priority queue ordering
  * ``TokenCounterProtocol`` — duck-type protocol for the
    token-counting dependency (avoids tight coupling to
    ``smart_context.TokenCounter``)
  * ``ASTChunker`` — the chunker itself

## What this module does NOT ship

  * Token counting — stays in ``smart_context.TokenCounter``;
    ``ASTChunker`` accepts any object satisfying
    ``TokenCounterProtocol``.
  * Relevance scoring — stays in
    ``smart_context.RelevanceScorer``; only the enum it uses lives
    here.
  * Dependency resolution — stays in
    ``smart_context.DependencyResolver``; the chunker's per-chunk
    ``calls`` set is enough downstream input.
  * Token budget management — stays in
    ``smart_context.TokenBudgetManager``.

## Authority posture

  * **Pure extraction** — no I/O beyond reading the target file
    asynchronously via ``asyncio.run_in_executor``. No subprocesses,
    no network, no orchestrator/policy/iron_gate imports.
  * **Stdlib + ``ast`` only** at top level. No third-party deps.
  * **NEVER raises into caller** — ``SyntaxError`` /
    ``UnicodeDecodeError`` / generic ``Exception`` all return ``[]``
    (empty chunk list) so a malformed file doesn't take down the
    consumer's prompt-building pipeline.

## Backward-compat invariant

``smart_context.py`` re-exports every symbol moved here so existing
callers like ``from backend.core.smart_context import ASTChunker,
CodeChunk`` continue to work. The chunker's behavior is byte-identical
pre/post-extraction — verified by the regression spine in
``tests/governance/test_ast_slicer.py``.
"""
from __future__ import annotations

import ast
import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import (
    Any,
    Dict,
    List,
    NamedTuple,
    Optional,
    Protocol,
    Set,
    Union,
)

logger = logging.getLogger("AstSlicer")


# ---------------------------------------------------------------------------
# Token-counter protocol — duck-typed so the chunker doesn't import the
# concrete ``TokenCounter`` from smart_context (which would cause a
# circular dependency post-extraction).
# ---------------------------------------------------------------------------


class TokenCounterProtocol(Protocol):
    """Anything with a ``count(text: str) -> int`` method.

    The chunker accepts any object satisfying this protocol. Production
    consumers pass ``smart_context.TokenCounter`` (heuristic +
    tiktoken). Tests can pass a stub like ``len`` or any callable
    wrapped in a small adapter.
    """

    def count(self, text: str) -> int:  # noqa: D401 — protocol method
        ...


# ---------------------------------------------------------------------------
# Chunk taxonomy
# ---------------------------------------------------------------------------


class ChunkType(Enum):
    """Type of code chunk."""

    FUNCTION = "function"
    METHOD = "method"
    CLASS = "class"
    CLASS_SKELETON = "class_skeleton"   # Class with method signatures only
    MODULE_HEADER = "module_header"      # Imports and module-level constants
    VARIABLE = "variable"
    DECORATOR = "decorator"


class RelevanceReason(Enum):
    """Why a chunk was selected. Lives here (vs. in
    ``smart_context.py``) only because ``CodeChunk`` references it."""

    DIRECT_MATCH = "direct_match"        # Directly matches query
    DEPENDENCY = "dependency"            # Called by or calls relevant code
    STRUCTURAL = "structural"            # Same class/module as relevant code
    SEMANTIC = "semantic"                # Semantically similar to query
    BLAST_RADIUS = "blast_radius"        # In the impact zone of changes


@dataclass
class CodeChunk:
    """A surgically extracted piece of code.

    Mutable on purpose — token_count + relevance fields are populated
    after construction by the chunker / scorer / budget manager.
    Equality and hashing are both based on ``chunk_id`` so chunks can
    live in sets and priority queues without colliding on identical
    bodies.
    """

    # Identity
    chunk_id: str                            # Unique identifier
    chunk_type: ChunkType
    name: str                                # Function/class/variable name
    qualified_name: str                      # Full path: module.Class.method

    # Location
    file_path: Path
    start_line: int
    end_line: int

    # Content
    source_code: str
    signature: Optional[str] = None          # For functions: def foo(a, b) -> int
    docstring: Optional[str] = None
    decorators: List[str] = field(default_factory=list)

    # Metadata
    token_count: int = 0
    complexity: int = 0                      # Cyclomatic complexity

    # Relevance
    relevance_score: float = 0.0
    relevance_reasons: List[RelevanceReason] = field(default_factory=list)

    # Dependencies
    calls: Set[str] = field(default_factory=set)       # Functions this calls
    called_by: Set[str] = field(default_factory=set)   # Functions that call this
    imports: Set[str] = field(default_factory=set)     # Imports needed

    def __hash__(self) -> int:
        return hash(self.chunk_id)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, CodeChunk):
            return self.chunk_id == other.chunk_id
        return False

    def __lt__(self, other: "CodeChunk") -> bool:
        """For priority queue (higher relevance = higher priority)."""
        return self.relevance_score > other.relevance_score


class ChunkPriority(NamedTuple):
    """For priority queue ordering."""

    negative_score: float                     # Negative because heapq is min-heap
    chunk_id: str
    chunk: CodeChunk


# ---------------------------------------------------------------------------
# AST Chunker
# ---------------------------------------------------------------------------


class ASTChunker:
    """Extracts code chunks from Python files using AST.

    This is the core of surgical context — it doesn't just read files,
    it UNDERSTANDS structure and extracts exactly what's needed.

    Two consumers post-Phase-11-P11.1:

      * ``backend/core/smart_context.py:SmartContextSelector`` — bulk
        chunking for prompt-building.
      * ``backend/core/ouroboros/governance/tool_executor.py``
        ``read_file`` tool (P11.2) — single-symbol extraction for
        Venom tool calls.

    Usage::

        chunker = ASTChunker(token_counter, include_decorators=True)
        chunks = await chunker.extract_chunks(
            Path("backend/foo.py"),
            target_names={"my_function", "MyClass.my_method"},
        )

    The chunker caches results per-(file, target-set, include-all)
    triple so a hot path that asks for the same chunks repeatedly
    doesn't re-parse.
    """

    def __init__(
        self,
        token_counter: TokenCounterProtocol,
        include_decorators: bool = True,
    ) -> None:
        self._token_counter = token_counter
        self._include_decorators = include_decorators
        self._chunk_cache: Dict[str, List[CodeChunk]] = {}

    async def extract_chunks(
        self,
        file_path: Path,
        target_names: Optional[Set[str]] = None,
        include_all: bool = False,
    ) -> List[CodeChunk]:
        """Extract code chunks from a Python file.

        Args:
          file_path: Path to Python file
          target_names: If provided, only extract these specific entities
          include_all: If True, extract all entities (for dependency
                       resolution)

        Returns:
          List of ``CodeChunk`` objects. Empty list on parse failure.
        """
        # Check cache
        cache_key = (
            f"{file_path}:{hash(frozenset(target_names or []))}"
            f":{include_all}"
        )
        if cache_key in self._chunk_cache:
            return self._chunk_cache[cache_key]

        try:
            source = await self._read_file(file_path)
            tree = ast.parse(source, filename=str(file_path))
        except SyntaxError as e:
            logger.warning(
                "[ASTChunker] Syntax error in %s: %s", file_path, e,
            )
            return []
        except Exception as e:  # noqa: BLE001 — defensive, NEVER raises
            logger.warning(
                "[ASTChunker] Failed to parse %s: %s", file_path, e,
            )
            return []

        chunks: List[CodeChunk] = []
        source_lines = source.splitlines(keepends=True)

        # Extract module-level docstring and imports
        module_header = self._extract_module_header(
            tree, source_lines, file_path,
        )
        if module_header and (include_all or target_names is None):
            chunks.append(module_header)

        # Walk the AST
        for node in ast.walk(tree):
            chunk: Optional[CodeChunk] = None

            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Skip if we have targets and this isn't one
                if target_names and node.name not in target_names:
                    continue
                chunk = self._extract_function(node, source_lines, file_path)

            elif isinstance(node, ast.ClassDef):
                if target_names and node.name not in target_names:
                    # Check if any method is targeted
                    method_names = {
                        n.name for n in node.body
                        if isinstance(
                            n, (ast.FunctionDef, ast.AsyncFunctionDef),
                        )
                    }
                    if not (target_names & method_names):
                        continue

                # Extract class with its methods
                class_chunks = self._extract_class(
                    node, source_lines, file_path, target_names,
                )
                chunks.extend(class_chunks)
                continue  # Methods handled in _extract_class

            if chunk:
                chunks.append(chunk)

        # Compute token counts
        for chunk in chunks:
            chunk.token_count = self._token_counter.count(chunk.source_code)

        # Cache results
        self._chunk_cache[cache_key] = chunks

        return chunks

    async def _read_file(self, file_path: Path) -> str:
        """Read file asynchronously."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, file_path.read_text)

    def _extract_module_header(
        self,
        tree: ast.Module,
        source_lines: List[str],
        file_path: Path,
    ) -> Optional[CodeChunk]:
        """Extract module docstring and imports."""
        imports: List[str] = []
        docstring = ast.get_docstring(tree)
        last_import_line = 0

        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                imports.append(ast.unparse(node))
                last_import_line = max(
                    last_import_line, node.end_lineno or node.lineno,
                )
            elif isinstance(node, ast.Expr) and isinstance(
                node.value, ast.Constant,
            ):
                # Module docstring
                if node.lineno == 1 or (node.lineno <= 3 and not imports):
                    continue  # Already captured

        if not imports and not docstring:
            return None

        # Build source
        end_line = last_import_line or (3 if docstring else 0)
        source = "".join(source_lines[:end_line])

        return CodeChunk(
            chunk_id=f"{file_path}::__module__",
            chunk_type=ChunkType.MODULE_HEADER,
            name="__module__",
            qualified_name=str(file_path.stem),
            file_path=file_path,
            start_line=1,
            end_line=end_line,
            source_code=source.strip(),
            docstring=docstring,
            imports=set(imports),
        )

    def _extract_function(
        self,
        node: Union[ast.FunctionDef, ast.AsyncFunctionDef],
        source_lines: List[str],
        file_path: Path,
        parent_class: Optional[str] = None,
    ) -> CodeChunk:
        """Extract a function/method as a chunk."""
        # Get source lines (1-indexed to 0-indexed)
        start = node.lineno - 1
        end = node.end_lineno or node.lineno

        # Include decorators
        decorator_lines: List[str] = []
        if node.decorator_list and self._include_decorators:
            first_decorator = node.decorator_list[0]
            start = first_decorator.lineno - 1
            for dec in node.decorator_list:
                decorator_lines.append(f"@{ast.unparse(dec)}")

        source = "".join(source_lines[start:end])

        # Build signature
        args: List[str] = []
        for arg in node.args.args:
            arg_str = arg.arg
            if arg.annotation:
                arg_str += f": {ast.unparse(arg.annotation)}"
            args.append(arg_str)

        returns = ""
        if node.returns:
            returns = f" -> {ast.unparse(node.returns)}"

        async_prefix = (
            "async " if isinstance(node, ast.AsyncFunctionDef) else ""
        )
        signature = (
            f"{async_prefix}def {node.name}({', '.join(args)}){returns}"
        )

        # Extract calls made by this function
        calls = self._extract_calls(node)

        # Determine type
        chunk_type = ChunkType.METHOD if parent_class else ChunkType.FUNCTION

        qualified = (
            f"{parent_class}.{node.name}" if parent_class else node.name
        )

        return CodeChunk(
            chunk_id=f"{file_path}::{qualified}",
            chunk_type=chunk_type,
            name=node.name,
            qualified_name=qualified,
            file_path=file_path,
            start_line=node.lineno,
            end_line=end,
            source_code=source.strip(),
            signature=signature,
            docstring=ast.get_docstring(node),
            decorators=decorator_lines,
            calls=calls,
            complexity=self._calculate_complexity(node),
        )

    def _extract_class(
        self,
        node: ast.ClassDef,
        source_lines: List[str],
        file_path: Path,
        target_names: Optional[Set[str]] = None,
    ) -> List[CodeChunk]:
        """Extract a class and its methods as chunks."""
        chunks: List[CodeChunk] = []

        # Get class bounds
        start = node.lineno - 1
        if node.decorator_list and self._include_decorators:
            start = node.decorator_list[0].lineno - 1

        # Extract base classes
        bases = [ast.unparse(base) for base in node.bases]

        # Class skeleton (without method bodies)
        skeleton_lines: List[str] = []
        decorator_lines = [
            f"@{ast.unparse(d)}" for d in node.decorator_list
        ]

        base_str = f"({', '.join(bases)})" if bases else ""
        skeleton_lines.append(f"class {node.name}{base_str}:")

        if ast.get_docstring(node):
            skeleton_lines.append(f'    """{ast.get_docstring(node)}"""')

        # Add method signatures
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Check if this method is targeted
                should_extract_full = (
                    target_names is None
                    or item.name in target_names
                    or node.name in target_names
                )

                if should_extract_full:
                    # Extract full method
                    method_chunk = self._extract_function(
                        item, source_lines, file_path,
                        parent_class=node.name,
                    )
                    chunks.append(method_chunk)
                else:
                    # Just add signature to skeleton
                    async_prefix = (
                        "async "
                        if isinstance(item, ast.AsyncFunctionDef)
                        else ""
                    )
                    args = ", ".join(a.arg for a in item.args.args)
                    skeleton_lines.append(
                        f"    {async_prefix}def {item.name}({args}): ..."
                    )

        # Create class skeleton chunk
        skeleton_source = "\n".join(decorator_lines + skeleton_lines)

        class_chunk = CodeChunk(
            chunk_id=f"{file_path}::{node.name}",
            chunk_type=(
                ChunkType.CLASS_SKELETON if chunks else ChunkType.CLASS
            ),
            name=node.name,
            qualified_name=node.name,
            file_path=file_path,
            start_line=node.lineno,
            end_line=node.end_lineno or node.lineno,
            source_code=skeleton_source,
            docstring=ast.get_docstring(node),
            decorators=[
                f"@{ast.unparse(d)}" for d in node.decorator_list
            ],
        )
        class_chunk.base_classes = bases  # type: ignore[attr-defined]

        chunks.insert(0, class_chunk)

        return chunks

    def _extract_calls(self, node: ast.AST) -> Set[str]:
        """Extract function/method calls made in a code block."""
        calls: Set[str] = set()

        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                if isinstance(child.func, ast.Name):
                    calls.add(child.func.id)
                elif isinstance(child.func, ast.Attribute):
                    # method call: obj.method()
                    calls.add(child.func.attr)

        return calls

    def _calculate_complexity(self, node: ast.AST) -> int:
        """Calculate cyclomatic complexity."""
        complexity = 1

        for child in ast.walk(node):
            if isinstance(child, (ast.If, ast.While, ast.For, ast.AsyncFor)):
                complexity += 1
            elif isinstance(child, ast.ExceptHandler):
                complexity += 1
            elif isinstance(child, (ast.And, ast.Or)):
                complexity += 1
            elif isinstance(child, ast.comprehension):
                complexity += 1

        return complexity


__all__ = [
    "ASTChunker",
    "ChunkPriority",
    "ChunkType",
    "CodeChunk",
    "RelevanceReason",
    "TokenCounterProtocol",
]
