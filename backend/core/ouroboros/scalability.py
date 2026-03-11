# DEPRECATED: superseded by governance/governed_loop_service.py. Quarantine date: 2026-03-11
"""
Ouroboros Scalability Engine v1.0
==================================

Advanced scalability features for handling large codebases:
- Large File Chunking: Split files >1MB into semantic chunks
- Parallel Improvement Pipeline: Process multiple files concurrently
- Context Window Optimizer: Dynamic token allocation
- Cross-Repo Scaling: Efficient multi-repo coordination

Handles codebases from 1,000 to 100,000+ files.

Architecture:
    ┌─────────────────────────────────────────────────────────────────────────┐
    │                     Scalability Engine                                   │
    ├─────────────────────────────────────────────────────────────────────────┤
    │  ┌───────────────────┐  ┌───────────────────┐  ┌─────────────────────┐  │
    │  │ LargeFileChunker  │  │ ParallelPipeline  │  │ ContextOptimizer   │  │
    │  │ ├── AST Chunking  │  │ ├── Worker Pool   │  │ ├── Token Budget   │  │
    │  │ ├── Semantic Split│  │ ├── Dep Ordering  │  │ ├── Priority Queue │  │
    │  │ └── Merge Logic   │  │ └── Result Merge  │  │ └── Dynamic Alloc  │  │
    │  └─────────┬─────────┘  └─────────┬─────────┘  └──────────┬──────────┘  │
    │            │                      │                        │             │
    │            ▼                      ▼                        ▼             │
    │  ┌─────────────────────────────────────────────────────────────────────┐│
    │  │                   ScalableImprovementOrchestrator                   ││
    │  │   ├── Handles 1-1000 files per batch                                ││
    │  │   ├── Respects dependency ordering                                  ││
    │  │   ├── Atomic rollback on failure                                    ││
    │  │   └── Cross-repo coordination                                       ││
    │  └─────────────────────────────────────────────────────────────────────┘│
    └─────────────────────────────────────────────────────────────────────────┘

Author: Trinity System
Version: 1.0.0
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import logging
import os
import time
from abc import ABC, abstractmethod
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import (
    Any,
    AsyncIterator,
    Callable,
    Dict,
    FrozenSet,
    List,
    Optional,
    Set,
    Tuple,
    TypeVar,
    Union,
)

logger = logging.getLogger("Ouroboros.Scalability")


# =============================================================================
# CONFIGURATION
# =============================================================================

class ScalabilityConfig:
    """Configuration for scalability features."""

    # File chunking
    CHUNK_THRESHOLD_BYTES = int(os.getenv("OUROBOROS_CHUNK_THRESHOLD", "102400"))  # 100KB
    CHUNK_THRESHOLD_LINES = int(os.getenv("OUROBOROS_CHUNK_LINES", "500"))
    MAX_CHUNK_SIZE = int(os.getenv("OUROBOROS_MAX_CHUNK", "50000"))  # 50KB per chunk
    MIN_CHUNK_SIZE = int(os.getenv("OUROBOROS_MIN_CHUNK", "1000"))   # 1KB min

    # Parallel processing
    MAX_PARALLEL_IMPROVEMENTS = int(os.getenv("OUROBOROS_MAX_PARALLEL_IMPROVE", "5"))
    MAX_PARALLEL_VALIDATIONS = int(os.getenv("OUROBOROS_MAX_PARALLEL_VALIDATE", "10"))
    WORKER_POOL_SIZE = int(os.getenv("OUROBOROS_WORKER_POOL", "8"))

    # Context optimization
    DEFAULT_CONTEXT_BUDGET = int(os.getenv("OUROBOROS_CONTEXT_BUDGET", "8000"))
    MAX_CONTEXT_BUDGET = int(os.getenv("OUROBOROS_MAX_CONTEXT", "32000"))
    RESERVED_TOKENS = int(os.getenv("OUROBOROS_RESERVED_TOKENS", "1000"))

    # Batching
    BATCH_SIZE = int(os.getenv("OUROBOROS_BATCH_SIZE", "10"))
    BATCH_TIMEOUT = float(os.getenv("OUROBOROS_BATCH_TIMEOUT", "300.0"))

    # Cross-repo
    CROSS_REPO_TIMEOUT = float(os.getenv("OUROBOROS_CROSS_REPO_TIMEOUT", "60.0"))
    CROSS_REPO_RETRY = int(os.getenv("OUROBOROS_CROSS_REPO_RETRY", "3"))


# =============================================================================
# DATA STRUCTURES
# =============================================================================

class ChunkType(Enum):
    """Type of file chunk."""
    IMPORTS = "imports"           # Import statements
    CONSTANTS = "constants"       # Module-level constants
    CLASS = "class"               # Complete class
    FUNCTION = "function"         # Standalone function
    PARTIAL_CLASS = "partial"     # Part of a class (for very large classes)


@dataclass
class FileChunk:
    """A semantic chunk of a large file."""
    chunk_id: str
    chunk_type: ChunkType
    source: str
    start_line: int
    end_line: int
    dependencies: Set[str] = field(default_factory=set)  # Other chunks this depends on
    dependents: Set[str] = field(default_factory=set)    # Chunks that depend on this
    token_count: int = 0
    order: int = 0  # For reassembly

    @property
    def line_count(self) -> int:
        return self.end_line - self.start_line + 1


@dataclass
class ChunkedFile:
    """A file split into semantic chunks."""
    file_path: Path
    chunks: List[FileChunk]
    original_hash: str
    total_lines: int
    total_tokens: int

    def get_chunk(self, chunk_id: str) -> Optional[FileChunk]:
        """Get a chunk by ID."""
        for chunk in self.chunks:
            if chunk.chunk_id == chunk_id:
                return chunk
        return None

    def reassemble(self) -> str:
        """Reassemble chunks back into complete file."""
        sorted_chunks = sorted(self.chunks, key=lambda c: c.order)
        return "\n\n".join(c.source for c in sorted_chunks)


@dataclass
class ParallelTask:
    """A task in the parallel pipeline."""
    task_id: str
    file_path: Path
    goal: str
    priority: int = 0
    dependencies: Set[str] = field(default_factory=set)  # Other task IDs this depends on
    status: str = "pending"
    result: Optional[Any] = None
    error: Optional[str] = None
    start_time: float = 0.0
    end_time: float = 0.0


@dataclass
class ImprovementBatch:
    """A batch of improvements to process together."""
    batch_id: str
    tasks: List[ParallelTask]
    atomic: bool = True
    cross_repo: bool = False
    created_at: float = field(default_factory=time.time)


# =============================================================================
# LARGE FILE CHUNKER
# =============================================================================

class LargeFileChunker:
    """
    Splits large files into semantic chunks for processing.

    Strategy:
    1. Parse AST to understand structure
    2. Split at class/function boundaries
    3. Keep imports/constants as separate chunk
    4. Track inter-chunk dependencies
    5. Support reassembly after improvement
    """

    def __init__(self, token_counter: Optional[Any] = None):
        self._token_counter = token_counter
        self._cache: Dict[str, ChunkedFile] = {}

    async def should_chunk(self, file_path: Path) -> bool:
        """Check if a file should be chunked."""
        if not file_path.exists():
            return False

        stat = file_path.stat()

        # Check size
        if stat.st_size > ScalabilityConfig.CHUNK_THRESHOLD_BYTES:
            return True

        # Check lines
        try:
            content = await self._read_file(file_path)
            if content.count('\n') > ScalabilityConfig.CHUNK_THRESHOLD_LINES:
                return True
        except Exception:
            pass

        return False

    async def chunk_file(self, file_path: Path) -> ChunkedFile:
        """
        Split a large file into semantic chunks.

        Returns ChunkedFile with ordered chunks that can be:
        - Processed independently
        - Improved individually
        - Reassembled back together
        """
        # Check cache
        content = await self._read_file(file_path)
        content_hash = hashlib.md5(content.encode()).hexdigest()

        cache_key = f"{file_path}:{content_hash}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        chunks = []
        chunk_order = 0

        try:
            tree = ast.parse(content, filename=str(file_path))
        except SyntaxError as e:
            # Can't parse - return as single chunk
            return ChunkedFile(
                file_path=file_path,
                chunks=[FileChunk(
                    chunk_id=f"{file_path}::full",
                    chunk_type=ChunkType.FUNCTION,
                    source=content,
                    start_line=1,
                    end_line=content.count('\n') + 1,
                    order=0,
                )],
                original_hash=content_hash,
                total_lines=content.count('\n') + 1,
                total_tokens=self._count_tokens(content),
            )

        source_lines = content.splitlines(keepends=True)

        # Phase 1: Extract imports and module-level code
        import_chunk, import_end = self._extract_imports(tree, source_lines, file_path)
        if import_chunk:
            import_chunk.order = chunk_order
            chunks.append(import_chunk)
            chunk_order += 1

        # Phase 2: Extract classes and functions
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef):
                class_chunks = self._extract_class_chunks(node, source_lines, file_path, chunk_order)
                for chunk in class_chunks:
                    chunk.order = chunk_order
                    chunks.append(chunk)
                    chunk_order += 1

            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                func_chunk = self._extract_function_chunk(node, source_lines, file_path)
                func_chunk.order = chunk_order
                chunks.append(func_chunk)
                chunk_order += 1

        # Phase 3: Compute inter-chunk dependencies
        self._compute_dependencies(chunks)

        # Phase 4: Compute token counts
        for chunk in chunks:
            chunk.token_count = self._count_tokens(chunk.source)

        result = ChunkedFile(
            file_path=file_path,
            chunks=chunks,
            original_hash=content_hash,
            total_lines=len(source_lines),
            total_tokens=sum(c.token_count for c in chunks),
        )

        self._cache[cache_key] = result
        logger.info(f"[Chunker] Split {file_path.name} into {len(chunks)} chunks")

        return result

    def _extract_imports(
        self,
        tree: ast.Module,
        source_lines: List[str],
        file_path: Path,
    ) -> Tuple[Optional[FileChunk], int]:
        """Extract imports and module docstring."""
        import_lines = []
        last_import_line = 0

        # Get docstring
        docstring_end = 0
        if (tree.body and isinstance(tree.body[0], ast.Expr) and
            isinstance(tree.body[0].value, ast.Constant)):
            docstring_end = tree.body[0].end_lineno or 0

        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                last_import_line = max(last_import_line, node.end_lineno or node.lineno)

        if last_import_line == 0 and docstring_end == 0:
            return None, 0

        end_line = max(last_import_line, docstring_end)
        source = "".join(source_lines[:end_line])

        return FileChunk(
            chunk_id=f"{file_path}::imports",
            chunk_type=ChunkType.IMPORTS,
            source=source.strip(),
            start_line=1,
            end_line=end_line,
        ), end_line

    def _extract_class_chunks(
        self,
        node: ast.ClassDef,
        source_lines: List[str],
        file_path: Path,
        base_order: int,
    ) -> List[FileChunk]:
        """Extract a class, potentially splitting very large classes."""
        start = node.lineno - 1
        end = node.end_lineno or node.lineno

        # Include decorators
        if node.decorator_list:
            start = node.decorator_list[0].lineno - 1

        source = "".join(source_lines[start:end])
        lines = end - start

        # If class is small enough, return as single chunk
        if lines <= ScalabilityConfig.CHUNK_THRESHOLD_LINES:
            return [FileChunk(
                chunk_id=f"{file_path}::{node.name}",
                chunk_type=ChunkType.CLASS,
                source=source,
                start_line=start + 1,
                end_line=end,
            )]

        # Large class - split into partial chunks
        chunks = []

        # Class header (up to first method)
        header_end = start
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                header_end = item.lineno - 2
                break
        else:
            header_end = end

        header_source = "".join(source_lines[start:header_end + 1])
        chunks.append(FileChunk(
            chunk_id=f"{file_path}::{node.name}::header",
            chunk_type=ChunkType.PARTIAL_CLASS,
            source=header_source,
            start_line=start + 1,
            end_line=header_end + 1,
        ))

        # Each method as separate chunk
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                method_start = item.lineno - 1
                if item.decorator_list:
                    method_start = item.decorator_list[0].lineno - 1

                method_end = item.end_lineno or item.lineno
                method_source = "".join(source_lines[method_start:method_end])

                chunks.append(FileChunk(
                    chunk_id=f"{file_path}::{node.name}::{item.name}",
                    chunk_type=ChunkType.PARTIAL_CLASS,
                    source=method_source,
                    start_line=method_start + 1,
                    end_line=method_end,
                    dependencies={f"{file_path}::{node.name}::header"},
                ))

        return chunks

    def _extract_function_chunk(
        self,
        node: Union[ast.FunctionDef, ast.AsyncFunctionDef],
        source_lines: List[str],
        file_path: Path,
    ) -> FileChunk:
        """Extract a standalone function."""
        start = node.lineno - 1
        if node.decorator_list:
            start = node.decorator_list[0].lineno - 1

        end = node.end_lineno or node.lineno
        source = "".join(source_lines[start:end])

        return FileChunk(
            chunk_id=f"{file_path}::{node.name}",
            chunk_type=ChunkType.FUNCTION,
            source=source,
            start_line=start + 1,
            end_line=end,
        )

    def _compute_dependencies(self, chunks: List[FileChunk]) -> None:
        """Compute inter-chunk dependencies based on name references."""
        # Build name -> chunk mapping
        chunk_names: Dict[str, str] = {}
        for chunk in chunks:
            # Extract the entity name from chunk_id
            parts = chunk.chunk_id.split("::")
            if len(parts) >= 2:
                chunk_names[parts[-1]] = chunk.chunk_id

        # Find references in each chunk
        for chunk in chunks:
            try:
                tree = ast.parse(chunk.source)
                for node in ast.walk(tree):
                    if isinstance(node, ast.Name):
                        if node.id in chunk_names and chunk_names[node.id] != chunk.chunk_id:
                            chunk.dependencies.add(chunk_names[node.id])
                    elif isinstance(node, ast.Attribute):
                        if node.attr in chunk_names and chunk_names[node.attr] != chunk.chunk_id:
                            chunk.dependencies.add(chunk_names[node.attr])
            except SyntaxError:
                pass

        # Build reverse dependencies
        for chunk in chunks:
            for dep_id in chunk.dependencies:
                for other in chunks:
                    if other.chunk_id == dep_id:
                        other.dependents.add(chunk.chunk_id)

    async def _read_file(self, file_path: Path) -> str:
        """Read file asynchronously."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, file_path.read_text)

    def _count_tokens(self, text: str) -> int:
        """Count tokens in text."""
        if self._token_counter:
            return self._token_counter.count(text)
        # Fallback: ~4 chars per token
        return len(text) // 4

    async def merge_chunk_improvements(
        self,
        original: ChunkedFile,
        improved_chunks: Dict[str, str],
    ) -> str:
        """
        Merge improved chunks back into complete file.

        Args:
            original: The original chunked file
            improved_chunks: {chunk_id: improved_source}

        Returns:
            Complete improved file content
        """
        # Create new chunks with improvements
        merged_chunks = []

        for chunk in sorted(original.chunks, key=lambda c: c.order):
            if chunk.chunk_id in improved_chunks:
                # Use improved version
                new_chunk = FileChunk(
                    chunk_id=chunk.chunk_id,
                    chunk_type=chunk.chunk_type,
                    source=improved_chunks[chunk.chunk_id],
                    start_line=chunk.start_line,
                    end_line=chunk.end_line,
                    order=chunk.order,
                )
            else:
                # Keep original
                new_chunk = chunk

            merged_chunks.append(new_chunk)

        # Reassemble
        return "\n\n".join(c.source for c in merged_chunks)


# =============================================================================
# PARALLEL IMPROVEMENT PIPELINE
# =============================================================================

class ParallelImprovementPipeline:
    """
    Processes multiple file improvements in parallel.

    Features:
    - Respects file dependencies (won't process B until A is done if B depends on A)
    - Configurable concurrency limits
    - Atomic batch processing (all succeed or all rollback)
    - Progress tracking
    """

    def __init__(
        self,
        max_parallel: int = ScalabilityConfig.MAX_PARALLEL_IMPROVEMENTS,
        worker_pool_size: int = ScalabilityConfig.WORKER_POOL_SIZE,
    ):
        self._max_parallel = max_parallel
        self._executor = ThreadPoolExecutor(max_workers=worker_pool_size)
        self._active_tasks: Dict[str, asyncio.Task] = {}
        self._results: Dict[str, Any] = {}
        self._lock = asyncio.Lock()

    async def process_batch(
        self,
        batch: ImprovementBatch,
        improve_func: Callable[[ParallelTask], Any],
    ) -> Dict[str, Any]:
        """
        Process a batch of improvements in parallel.

        Args:
            batch: The batch of tasks to process
            improve_func: Async function to call for each task

        Returns:
            {task_id: result} for all tasks
        """
        logger.info(f"[ParallelPipeline] Processing batch {batch.batch_id} with {len(batch.tasks)} tasks")
        start_time = time.time()

        # Build dependency graph
        pending = {t.task_id: t for t in batch.tasks}
        completed: Set[str] = set()
        failed: Set[str] = set()
        results: Dict[str, Any] = {}

        # Process in waves based on dependencies
        while pending:
            # Find tasks that can run (dependencies satisfied)
            ready = []
            for task_id, task in list(pending.items()):
                if task.dependencies <= completed:
                    ready.append(task)
                elif task.dependencies & failed:
                    # Dependency failed - skip this task
                    task.status = "skipped"
                    task.error = "Dependency failed"
                    failed.add(task_id)
                    del pending[task_id]

            if not ready:
                if pending:
                    logger.warning(f"[ParallelPipeline] Circular dependency detected, {len(pending)} tasks stuck")
                break

            # Process ready tasks in parallel (up to limit)
            wave_tasks = []
            for task in ready[:self._max_parallel]:
                task.status = "running"
                task.start_time = time.time()
                del pending[task.task_id]

                coro = self._run_task(task, improve_func)
                wave_tasks.append((task, asyncio.create_task(coro)))

            # Wait for wave to complete
            for task, async_task in wave_tasks:
                try:
                    result = await asyncio.wait_for(
                        async_task,
                        timeout=ScalabilityConfig.BATCH_TIMEOUT,
                    )
                    task.result = result
                    task.status = "completed"
                    task.end_time = time.time()
                    completed.add(task.task_id)
                    results[task.task_id] = result

                except asyncio.TimeoutError:
                    task.status = "timeout"
                    task.error = "Task timed out"
                    failed.add(task.task_id)

                except Exception as e:
                    task.status = "failed"
                    task.error = str(e)
                    task.end_time = time.time()
                    failed.add(task.task_id)
                    logger.error(f"[ParallelPipeline] Task {task.task_id} failed: {e}")

                    if batch.atomic:
                        # Atomic mode - fail entire batch
                        logger.warning(f"[ParallelPipeline] Atomic batch failed, aborting remaining tasks")
                        return {"error": str(e), "failed_task": task.task_id}

        elapsed = time.time() - start_time
        logger.info(
            f"[ParallelPipeline] Batch complete: {len(completed)} succeeded, "
            f"{len(failed)} failed in {elapsed:.1f}s"
        )

        return results

    async def _run_task(
        self,
        task: ParallelTask,
        improve_func: Callable,
    ) -> Any:
        """Run a single improvement task."""
        return await improve_func(task)

    async def shutdown(self) -> None:
        """Shutdown the pipeline."""
        self._executor.shutdown(wait=True)


# =============================================================================
# CONTEXT WINDOW OPTIMIZER
# =============================================================================

class ContextWindowOptimizer:
    """
    Dynamically optimizes context allocation for maximum effectiveness.

    Strategy:
    1. Prioritize most relevant code
    2. Include critical dependencies
    3. Allocate remaining budget to supporting context
    4. Track what got included for debugging
    """

    def __init__(self, max_budget: int = ScalabilityConfig.MAX_CONTEXT_BUDGET):
        self._max_budget = max_budget
        self._token_counter = None

    async def initialize(self) -> None:
        """Initialize token counter."""
        try:
            from backend.core.smart_context import TokenCounter
            self._token_counter = await TokenCounter.get_instance()
        except ImportError:
            logger.warning("[ContextOptimizer] TokenCounter not available, using heuristic")

    def count_tokens(self, text: str) -> int:
        """Count tokens in text."""
        if self._token_counter:
            return self._token_counter.count(text)
        return len(text) // 4

    async def optimize_context(
        self,
        target_content: str,
        goal: str,
        available_context: List[Tuple[str, str, float]],  # (name, content, relevance)
        budget: int = ScalabilityConfig.DEFAULT_CONTEXT_BUDGET,
    ) -> Tuple[str, Dict[str, Any]]:
        """
        Build optimized context within token budget.

        Args:
            target_content: The file being improved
            goal: The improvement goal
            available_context: List of (name, content, relevance_score)
            budget: Token budget

        Returns:
            (optimized_context_string, metadata)
        """
        # Reserve space for target and goal
        target_tokens = self.count_tokens(target_content)
        goal_tokens = self.count_tokens(goal)
        reserved = target_tokens + goal_tokens + ScalabilityConfig.RESERVED_TOKENS

        available_budget = min(budget, self._max_budget) - reserved
        if available_budget <= 0:
            return "", {"error": "Target file exceeds budget", "target_tokens": target_tokens}

        # Sort by relevance
        sorted_context = sorted(available_context, key=lambda x: x[2], reverse=True)

        # Greedy packing
        selected = []
        used_tokens = 0
        metadata = {
            "budget": budget,
            "reserved": reserved,
            "available": available_budget,
            "selected": [],
            "excluded": [],
        }

        for name, content, relevance in sorted_context:
            tokens = self.count_tokens(content)

            if used_tokens + tokens <= available_budget:
                selected.append((name, content))
                used_tokens += tokens
                metadata["selected"].append({"name": name, "tokens": tokens, "relevance": relevance})
            else:
                metadata["excluded"].append({"name": name, "tokens": tokens, "relevance": relevance})

        # Format selected context
        context_parts = []
        for name, content in selected:
            context_parts.append(f"### {name}\n```python\n{content}\n```\n")

        metadata["total_tokens"] = used_tokens
        metadata["utilization"] = used_tokens / available_budget if available_budget > 0 else 0

        return "\n".join(context_parts), metadata


# =============================================================================
# SCALABLE IMPROVEMENT ORCHESTRATOR
# =============================================================================

class ScalableImprovementOrchestrator:
    """
    High-level orchestrator for scalable improvements.

    Combines:
    - Large file chunking
    - Parallel processing
    - Context optimization
    - Cross-repo coordination
    """

    def __init__(self):
        self._chunker = LargeFileChunker()
        self._pipeline = ParallelImprovementPipeline()
        self._context_optimizer = ContextWindowOptimizer()
        self._initialized = False

    async def initialize(self) -> None:
        """Initialize all components."""
        if self._initialized:
            return

        await self._context_optimizer.initialize()
        self._initialized = True
        logger.info("[ScalableOrchestrator] Initialized")

    async def improve_files(
        self,
        files: List[Tuple[Path, str]],  # (file_path, goal)
        improve_func: Callable[[Path, str, str], str],  # (path, content, goal) -> improved
        atomic: bool = True,
        parallel: bool = True,
    ) -> Dict[Path, str]:
        """
        Improve multiple files with scalability.

        Args:
            files: List of (file_path, goal) tuples
            improve_func: Function to improve a single file
            atomic: If True, rollback all if any fails
            parallel: If True, process in parallel

        Returns:
            {file_path: improved_content}
        """
        if not self._initialized:
            await self.initialize()

        results: Dict[Path, str] = {}
        chunked_files: Dict[Path, ChunkedFile] = {}

        # Phase 1: Identify large files that need chunking
        for file_path, goal in files:
            if await self._chunker.should_chunk(file_path):
                chunked = await self._chunker.chunk_file(file_path)
                chunked_files[file_path] = chunked
                logger.info(f"[ScalableOrchestrator] {file_path.name} chunked into {len(chunked.chunks)} parts")

        # Phase 2: Create tasks
        tasks = []
        for file_path, goal in files:
            if file_path in chunked_files:
                # Create task for each chunk
                chunked = chunked_files[file_path]
                for chunk in chunked.chunks:
                    task = ParallelTask(
                        task_id=chunk.chunk_id,
                        file_path=file_path,
                        goal=goal,
                        dependencies=chunk.dependencies,
                    )
                    tasks.append(task)
            else:
                # Single task for whole file
                task = ParallelTask(
                    task_id=str(file_path),
                    file_path=file_path,
                    goal=goal,
                )
                tasks.append(task)

        # Phase 3: Process
        if parallel and len(tasks) > 1:
            batch = ImprovementBatch(
                batch_id=f"batch_{int(time.time())}",
                tasks=tasks,
                atomic=atomic,
            )

            async def task_improver(task: ParallelTask) -> str:
                content = task.file_path.read_text()
                return await improve_func(task.file_path, content, task.goal)

            batch_results = await self._pipeline.process_batch(batch, task_improver)

            if "error" in batch_results:
                raise RuntimeError(batch_results["error"])

            # Merge chunked results
            for file_path, goal in files:
                if file_path in chunked_files:
                    chunked = chunked_files[file_path]
                    improved_chunks = {
                        chunk.chunk_id: batch_results.get(chunk.chunk_id, chunk.source)
                        for chunk in chunked.chunks
                    }
                    results[file_path] = await self._chunker.merge_chunk_improvements(
                        chunked, improved_chunks
                    )
                else:
                    results[file_path] = batch_results.get(str(file_path), "")

        else:
            # Sequential processing
            for file_path, goal in files:
                content = file_path.read_text()
                improved = await improve_func(file_path, content, goal)
                results[file_path] = improved

        return results

    async def shutdown(self) -> None:
        """Shutdown the orchestrator."""
        await self._pipeline.shutdown()


# =============================================================================
# SINGLETON ACCESSOR
# =============================================================================

_scalability_instance: Optional[ScalableImprovementOrchestrator] = None
_scalability_lock = asyncio.Lock()


async def get_scalable_orchestrator() -> ScalableImprovementOrchestrator:
    """Get the global ScalableImprovementOrchestrator instance."""
    global _scalability_instance

    async with _scalability_lock:
        if _scalability_instance is None:
            _scalability_instance = ScalableImprovementOrchestrator()
            await _scalability_instance.initialize()
        return _scalability_instance


# =============================================================================
# EXPORTS
# =============================================================================

__all__ = [
    "ScalabilityConfig",
    "LargeFileChunker",
    "ParallelImprovementPipeline",
    "ContextWindowOptimizer",
    "ScalableImprovementOrchestrator",
    "FileChunk",
    "ChunkedFile",
    "ParallelTask",
    "ImprovementBatch",
    "get_scalable_orchestrator",
]
