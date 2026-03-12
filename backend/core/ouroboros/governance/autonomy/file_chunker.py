"""Semantic file chunking for the L1 execution pipeline.

Splits large Python files into semantic chunks (imports, constants, classes,
functions) so L1 can process them in manageable pieces.  Falls back to
line-based splitting when AST parsing is not possible.

Extracted from the deprecated ``backend/core/ouroboros/scalability.py``
and adapted to work on *string content* rather than file paths.
"""

from __future__ import annotations

import ast
import hashlib
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Set

logger = logging.getLogger("Ouroboros.FileChunker")


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class ChunkType(Enum):
    """Semantic type of a file chunk."""

    IMPORTS = "imports"
    CONSTANTS = "constants"
    CLASS = "class"
    FUNCTION = "function"
    PARTIAL_CLASS = "partial"


@dataclass(frozen=True)
class ChunkConfig:
    """Configuration for file chunking thresholds."""

    threshold_lines: int = 500  # Files larger than this get chunked
    max_chunk_lines: int = 200  # Maximum lines per chunk
    min_chunk_lines: int = 10   # Minimum lines per chunk (avoid tiny fragments)


@dataclass
class FileChunk:
    """A semantic chunk of a large file."""

    chunk_id: str
    chunk_type: ChunkType
    content: str
    start_line: int
    end_line: int
    name: str  # class/function name, or "imports"/"constants"
    dependencies: Set[str] = field(default_factory=set)
    order: int = 0  # For reassembly ordering

    @property
    def line_count(self) -> int:
        return self.end_line - self.start_line + 1


@dataclass
class ChunkedFile:
    """A file split into semantic chunks."""

    file_path: str
    chunks: List[FileChunk]
    total_lines: int
    original_hash: str  # SHA-256 of original content for integrity

    def get_chunk(self, chunk_id: str) -> Optional[FileChunk]:
        """Find a chunk by ID."""
        for chunk in self.chunks:
            if chunk.chunk_id == chunk_id:
                return chunk
        return None

    def reassemble(self) -> str:
        """Reassemble chunks back into complete file content."""
        return "\n".join(
            c.content for c in sorted(self.chunks, key=lambda c: c.order)
        )


# ---------------------------------------------------------------------------
# Chunker
# ---------------------------------------------------------------------------

class LargeFileChunker:
    """Splits large Python files into semantic chunks using AST parsing.

    Used by L1 to break up large file operations into manageable pieces
    that can be processed independently.
    """

    def __init__(self, config: Optional[ChunkConfig] = None) -> None:
        self._config = config or ChunkConfig()

    # -- public API ---------------------------------------------------------

    def should_chunk(self, content: str) -> bool:
        """Return *True* if *content* exceeds the line threshold."""
        return content.count("\n") + (1 if content and not content.endswith("\n") else 0) > self._config.threshold_lines

    def chunk_content(
        self, content: str, file_path: str = "<unknown>"
    ) -> ChunkedFile:
        """Split Python file content into semantic chunks.

        Strategy
        --------
        1. Parse with ``ast.parse()``
        2. Extract import block (all top-level imports + module docstring)
        3. Extract module-level constants (assignments between imports and
           first class/function)
        4. Extract each top-level class as a chunk (split large classes)
        5. Extract each top-level function as a chunk
        6. Assign ordering for reassembly

        Falls back to :meth:`chunk_by_lines` if AST parsing fails.
        """
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        total_lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)

        if not content.strip():
            return ChunkedFile(
                file_path=file_path,
                chunks=[],
                total_lines=0,
                original_hash=content_hash,
            )

        try:
            tree = ast.parse(content, filename=file_path)
        except SyntaxError:
            return self.chunk_by_lines(content, file_path)

        source_lines = content.splitlines(keepends=True)

        chunks: List[FileChunk] = []
        order = 0

        # Phase 1 — imports (+ module docstring)
        import_chunk, import_end_line = self._extract_imports(
            tree, source_lines, file_path
        )
        if import_chunk is not None:
            import_chunk = FileChunk(
                chunk_id=import_chunk.chunk_id,
                chunk_type=import_chunk.chunk_type,
                content=import_chunk.content,
                start_line=import_chunk.start_line,
                end_line=import_chunk.end_line,
                name=import_chunk.name,
                dependencies=import_chunk.dependencies,
                order=order,
            )
            chunks.append(import_chunk)
            order += 1

        # Phase 2 — constants (module-level assignments between imports and
        #           the first class/function definition)
        const_chunk = self._extract_constants(
            tree, source_lines, file_path, import_end_line
        )
        if const_chunk is not None:
            const_chunk = FileChunk(
                chunk_id=const_chunk.chunk_id,
                chunk_type=const_chunk.chunk_type,
                content=const_chunk.content,
                start_line=const_chunk.start_line,
                end_line=const_chunk.end_line,
                name=const_chunk.name,
                dependencies=const_chunk.dependencies,
                order=order,
            )
            chunks.append(const_chunk)
            order += 1

        # Phase 3 — classes and functions
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef):
                class_chunks = self._extract_class(
                    node, source_lines, file_path, order
                )
                for cc in class_chunks:
                    cc_new = FileChunk(
                        chunk_id=cc.chunk_id,
                        chunk_type=cc.chunk_type,
                        content=cc.content,
                        start_line=cc.start_line,
                        end_line=cc.end_line,
                        name=cc.name,
                        dependencies=cc.dependencies,
                        order=order,
                    )
                    chunks.append(cc_new)
                    order += 1

            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fc = self._extract_function(node, source_lines, file_path)
                fc_new = FileChunk(
                    chunk_id=fc.chunk_id,
                    chunk_type=fc.chunk_type,
                    content=fc.content,
                    start_line=fc.start_line,
                    end_line=fc.end_line,
                    name=fc.name,
                    dependencies=fc.dependencies,
                    order=order,
                )
                chunks.append(fc_new)
                order += 1

        # Phase 4 — dependency analysis
        self._compute_dependencies(chunks)

        return ChunkedFile(
            file_path=file_path,
            chunks=chunks,
            total_lines=total_lines,
            original_hash=content_hash,
        )

    def chunk_by_lines(
        self, content: str, file_path: str = "<unknown>"
    ) -> ChunkedFile:
        """Fallback: split by line count when AST parsing fails."""
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        lines = content.splitlines(keepends=True)
        total_lines = len(lines)
        max_lines = max(self._config.max_chunk_lines, 1)

        chunks: List[FileChunk] = []
        order = 0
        idx = 0

        while idx < total_lines:
            end = min(idx + max_lines, total_lines)
            chunk_content = "".join(lines[idx:end])
            # Strip trailing newline that will be re-added by reassemble
            if chunk_content.endswith("\n"):
                chunk_content = chunk_content[:-1]
            chunks.append(
                FileChunk(
                    chunk_id=f"{file_path}::lines_{idx + 1}_{end}",
                    chunk_type=ChunkType.FUNCTION,  # generic fallback type
                    content=chunk_content,
                    start_line=idx + 1,
                    end_line=end,
                    name=f"lines_{idx + 1}_{end}",
                    order=order,
                )
            )
            order += 1
            idx = end

        return ChunkedFile(
            file_path=file_path,
            chunks=chunks,
            total_lines=total_lines,
            original_hash=content_hash,
        )

    # -- private helpers ----------------------------------------------------

    def _extract_imports(
        self,
        tree: ast.Module,
        source_lines: List[str],
        file_path: str,
    ) -> tuple:  # -> (Optional[FileChunk], int)
        """Extract imports and module docstring.

        Returns ``(chunk_or_None, last_import_line_1indexed)``.
        """
        last_import_line = 0
        docstring_end = 0

        # Module docstring
        if (
            tree.body
            and isinstance(tree.body[0], ast.Expr)
            and isinstance(tree.body[0].value, ast.Constant)
        ):
            docstring_end = tree.body[0].end_lineno or 0

        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                last_import_line = max(
                    last_import_line, node.end_lineno or node.lineno
                )

        if last_import_line == 0 and docstring_end == 0:
            return None, 0

        end_line = max(last_import_line, docstring_end)
        source = "".join(source_lines[:end_line]).rstrip("\n")

        chunk = FileChunk(
            chunk_id=f"{file_path}::imports",
            chunk_type=ChunkType.IMPORTS,
            content=source,
            start_line=1,
            end_line=end_line,
            name="imports",
        )
        return chunk, end_line

    def _extract_constants(
        self,
        tree: ast.Module,
        source_lines: List[str],
        file_path: str,
        after_line: int,
    ) -> Optional[FileChunk]:
        """Extract module-level constant assignments that sit between the
        import block and the first class/function definition.
        """
        # Find first class/function line
        first_def_line: Optional[int] = None
        for node in tree.body:
            if isinstance(
                node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                start = node.lineno
                if hasattr(node, "decorator_list") and node.decorator_list:
                    start = node.decorator_list[0].lineno
                first_def_line = start
                break

        # Collect assignment nodes between imports and first def
        const_nodes: List[ast.stmt] = []
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            if isinstance(node, ast.Expr) and isinstance(
                node.value, ast.Constant
            ):
                # skip module docstring
                continue
            if isinstance(
                node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                break
            # Anything remaining before first def is a "constant"
            const_nodes.append(node)

        if not const_nodes:
            return None

        start_line = const_nodes[0].lineno
        end_line = max(
            n.end_lineno or n.lineno for n in const_nodes
        )
        source = "".join(source_lines[start_line - 1 : end_line]).rstrip("\n")

        return FileChunk(
            chunk_id=f"{file_path}::constants",
            chunk_type=ChunkType.CONSTANTS,
            content=source,
            start_line=start_line,
            end_line=end_line,
            name="constants",
        )

    def _extract_class(
        self,
        node: ast.ClassDef,
        source_lines: List[str],
        file_path: str,
        base_order: int,
    ) -> List[FileChunk]:
        """Extract a class — split into PARTIAL_CLASS chunks if it exceeds
        ``max_chunk_lines``.
        """
        start = node.lineno - 1  # 0-indexed
        end = node.end_lineno or node.lineno  # 1-indexed inclusive

        if node.decorator_list:
            start = node.decorator_list[0].lineno - 1

        total_class_lines = end - start

        # Small enough — return as single CLASS chunk
        if total_class_lines <= self._config.max_chunk_lines:
            source = "".join(source_lines[start:end]).rstrip("\n")
            return [
                FileChunk(
                    chunk_id=f"{file_path}::{node.name}",
                    chunk_type=ChunkType.CLASS,
                    content=source,
                    start_line=start + 1,
                    end_line=end,
                    name=node.name,
                )
            ]

        # Large class — split into header + methods
        chunks: List[FileChunk] = []

        # Header: from class start up to the first method
        header_end = start  # 0-indexed
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                dec_start = item.lineno - 1
                if item.decorator_list:
                    dec_start = item.decorator_list[0].lineno - 1
                header_end = dec_start - 1
                break
        else:
            header_end = end - 1  # no methods — entire body is header

        header_source = "".join(
            source_lines[start : header_end + 1]
        ).rstrip("\n")
        chunks.append(
            FileChunk(
                chunk_id=f"{file_path}::{node.name}::header",
                chunk_type=ChunkType.PARTIAL_CLASS,
                content=header_source,
                start_line=start + 1,
                end_line=header_end + 1,
                name=f"{node.name}::header",
            )
        )

        # Each method as separate partial chunk
        for item in node.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue

            method_start = item.lineno - 1
            if item.decorator_list:
                method_start = item.decorator_list[0].lineno - 1
            method_end = item.end_lineno or item.lineno

            method_source = "".join(
                source_lines[method_start:method_end]
            ).rstrip("\n")
            chunks.append(
                FileChunk(
                    chunk_id=f"{file_path}::{node.name}::{item.name}",
                    chunk_type=ChunkType.PARTIAL_CLASS,
                    content=method_source,
                    start_line=method_start + 1,
                    end_line=method_end,
                    name=f"{node.name}::{item.name}",
                    dependencies={f"{file_path}::{node.name}::header"},
                )
            )

        return chunks

    def _extract_function(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        source_lines: List[str],
        file_path: str,
    ) -> FileChunk:
        """Extract a standalone function."""
        start = node.lineno - 1
        if node.decorator_list:
            start = node.decorator_list[0].lineno - 1
        end = node.end_lineno or node.lineno

        source = "".join(source_lines[start:end]).rstrip("\n")

        return FileChunk(
            chunk_id=f"{file_path}::{node.name}",
            chunk_type=ChunkType.FUNCTION,
            content=source,
            start_line=start + 1,
            end_line=end,
            name=node.name,
        )

    def _compute_dependencies(self, chunks: List[FileChunk]) -> None:
        """Compute inter-chunk dependencies based on name references."""
        # Build a mapping: simple_name -> chunk_id
        name_to_id: Dict[str, str] = {}
        for chunk in chunks:
            # Use the last segment of the chunk_id (e.g. "Foo" from "file::Foo")
            parts = chunk.chunk_id.split("::")
            if len(parts) >= 2:
                simple = parts[-1]
                name_to_id[simple] = chunk.chunk_id

        for chunk in chunks:
            try:
                mini_tree = ast.parse(chunk.content)
            except SyntaxError:
                continue
            for sub_node in ast.walk(mini_tree):
                ref_name: Optional[str] = None
                if isinstance(sub_node, ast.Name):
                    ref_name = sub_node.id
                elif isinstance(sub_node, ast.Attribute):
                    ref_name = sub_node.attr

                if ref_name is None:
                    continue
                target_id = name_to_id.get(ref_name)
                if target_id is not None and target_id != chunk.chunk_id:
                    chunk.dependencies.add(target_id)


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "ChunkConfig",
    "ChunkType",
    "ChunkedFile",
    "FileChunk",
    "LargeFileChunker",
]
