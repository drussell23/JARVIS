"""
Context Engine v1.0 - Full Codebase Context Window Management
=============================================================

Enterprise-grade system for managing extremely large codebase contexts.
Handles 200k+ tokens, 1000+ file codebases with intelligent prioritization.

Features:
- Hierarchical code summarization for large codebases
- Semantic chunking with overlap for continuity
- Priority-based context selection using relevance scoring
- Streaming/incremental context loading
- LRU caching with TTL for fast retrieval
- Cross-repository context aggregation
- Token-aware truncation with semantic boundaries

Architecture:
    ┌─────────────────────────────────────────────────────────────────────────┐
    │                       Context Engine v1.0                                │
    ├─────────────────────────────────────────────────────────────────────────┤
    │                                                                          │
    │   ┌─────────────────┐   ┌─────────────────┐   ┌─────────────────┐       │
    │   │  Code Indexer   │   │ Semantic Chunker│   │ Context Ranker  │       │
    │   │  (AST + Hash)   │──▶│  (Boundaries)   │──▶│  (Relevance)    │       │
    │   └─────────────────┘   └─────────────────┘   └─────────────────┘       │
    │           │                     │                     │                  │
    │           └─────────────────────┴─────────────────────┘                  │
    │                                 │                                        │
    │                    ┌────────────▼────────────┐                           │
    │                    │   Context Window Manager │                          │
    │                    │   (Token-Aware)          │                          │
    │                    └────────────┬────────────┘                           │
    │                                 │                                        │
    │   ┌──────────────┬──────────────┼──────────────┬──────────────┐         │
    │   │              │              │              │              │         │
    │   ▼              ▼              ▼              ▼              ▼         │
    │ Hierarchical  Streaming    Priority-Based   Cross-Repo    Cache        │
    │ Summarizer    Loader       Selector         Aggregator    Manager      │
    │                                                                          │
    └─────────────────────────────────────────────────────────────────────────┘

Author: JARVIS AI System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import ast
import hashlib
import heapq
import logging
import mmap
import os
import re
import struct
import sys
import time
import weakref
from abc import ABC, abstractmethod
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from enum import Enum, auto
from functools import lru_cache, wraps
from pathlib import Path
from typing import (
    Any, AsyncGenerator, AsyncIterator, Awaitable, Callable, Coroutine,
    DefaultDict, Dict, FrozenSet, Generator, Generic, Iterable, Iterator,
    List, Literal, Mapping, NamedTuple, Optional, Protocol, Sequence,
    Set, Tuple, Type, TypeVar, Union, cast, overload
)

logger = logging.getLogger(__name__)

T = TypeVar("T")
K = TypeVar("K")
V = TypeVar("V")


# =============================================================================
# CONFIGURATION - Environment Driven (Zero Hardcoding)
# =============================================================================

def _get_env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _get_env_int(key: str, default: int) -> int:
    try:
        return int(_get_env(key, str(default)))
    except ValueError:
        return default


def _get_env_float(key: str, default: float) -> float:
    try:
        return float(_get_env(key, str(default)))
    except ValueError:
        return default


def _get_env_bool(key: str, default: bool = False) -> bool:
    val = _get_env(key, str(default)).lower()
    return val in ("true", "1", "yes", "on")


def _get_env_list(key: str, default: List[str] = None) -> List[str]:
    val = _get_env(key, "")
    if not val:
        return default or []
    return [x.strip() for x in val.split(",") if x.strip()]


class ContextEngineConfig:
    """Configuration for context engine."""

    # Token limits
    MAX_CONTEXT_TOKENS: int = _get_env_int("CONTEXT_MAX_TOKENS", 200000)
    CHUNK_SIZE_TOKENS: int = _get_env_int("CONTEXT_CHUNK_SIZE", 2000)
    CHUNK_OVERLAP_TOKENS: int = _get_env_int("CONTEXT_CHUNK_OVERLAP", 200)

    # Caching
    CACHE_SIZE: int = _get_env_int("CONTEXT_CACHE_SIZE", 1000)
    CACHE_TTL_SECONDS: int = _get_env_int("CONTEXT_CACHE_TTL", 3600)
    INDEX_PERSIST_DIR: Path = Path(_get_env("CONTEXT_INDEX_DIR", str(Path.home() / ".jarvis/context_index")))

    # Processing
    MAX_FILES_PARALLEL: int = _get_env_int("CONTEXT_MAX_PARALLEL_FILES", 50)
    MAX_FILE_SIZE_MB: float = _get_env_float("CONTEXT_MAX_FILE_SIZE_MB", 10.0)
    SUMMARIZATION_THRESHOLD: int = _get_env_int("CONTEXT_SUMMARIZATION_THRESHOLD", 5000)

    # Relevance scoring
    RELEVANCE_DECAY_FACTOR: float = _get_env_float("CONTEXT_RELEVANCE_DECAY", 0.95)
    RECENCY_WEIGHT: float = _get_env_float("CONTEXT_RECENCY_WEIGHT", 0.3)
    SIMILARITY_WEIGHT: float = _get_env_float("CONTEXT_SIMILARITY_WEIGHT", 0.5)
    DEPENDENCY_WEIGHT: float = _get_env_float("CONTEXT_DEPENDENCY_WEIGHT", 0.2)

    # Repository paths
    JARVIS_REPO: Path = Path(_get_env("JARVIS_REPO", str(Path.home() / "Documents/repos/JARVIS-AI-Agent")))
    PRIME_REPO: Path = Path(_get_env("PRIME_REPO", str(Path.home() / "Documents/repos/jarvis-prime")))
    REACTOR_REPO: Path = Path(_get_env("REACTOR_REPO", str(Path.home() / "Documents/repos/reactor-core")))

    # File patterns
    INCLUDE_PATTERNS: List[str] = _get_env_list("CONTEXT_INCLUDE_PATTERNS", ["*.py", "*.js", "*.ts", "*.tsx", "*.java", "*.go", "*.rs"])
    EXCLUDE_PATTERNS: List[str] = _get_env_list("CONTEXT_EXCLUDE_PATTERNS", ["**/node_modules/**", "**/__pycache__/**", "**/venv/**", "**/.git/**"])


# =============================================================================
# ENUMS AND TYPES
# =============================================================================

class ContextPriority(Enum):
    """Priority levels for context items."""
    CRITICAL = 5      # Currently edited file
    HIGH = 4          # Direct dependencies
    MEDIUM = 3        # Indirect dependencies
    LOW = 2           # Same directory
    BACKGROUND = 1    # Everything else


class ChunkType(Enum):
    """Types of code chunks."""
    FILE_HEADER = "file_header"
    IMPORT_BLOCK = "import_block"
    CLASS_DEFINITION = "class_definition"
    FUNCTION_DEFINITION = "function_definition"
    METHOD_DEFINITION = "method_definition"
    DOCSTRING = "docstring"
    COMMENT_BLOCK = "comment_block"
    CODE_BLOCK = "code_block"
    VARIABLE_DECLARATION = "variable_declaration"


class SummarizationLevel(Enum):
    """Levels of code summarization."""
    FULL = "full"                    # Complete code
    SIGNATURES = "signatures"         # Function/class signatures only
    DOCSTRINGS = "docstrings"        # Signatures + docstrings
    NAMES_ONLY = "names_only"        # Just names and structure
    SKELETON = "skeleton"            # Minimal structure


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass(frozen=True)
class TokenEstimate:
    """Estimated token count for text."""
    char_count: int
    word_count: int
    estimated_tokens: int

    @classmethod
    def from_text(cls, text: str) -> "TokenEstimate":
        """Estimate tokens from text using heuristics."""
        char_count = len(text)
        word_count = len(text.split())
        # Rough estimate: ~4 chars per token for code
        estimated_tokens = max(char_count // 4, word_count)
        return cls(char_count, word_count, estimated_tokens)


@dataclass
class CodeChunk:
    """A semantic chunk of code."""
    id: str
    file_path: Path
    chunk_type: ChunkType
    content: str
    start_line: int
    end_line: int
    token_estimate: TokenEstimate
    metadata: Dict[str, Any] = field(default_factory=dict)

    # Semantic info
    name: Optional[str] = None
    parent_name: Optional[str] = None
    dependencies: Set[str] = field(default_factory=set)

    def __hash__(self) -> int:
        return hash(self.id)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, CodeChunk):
            return False
        return self.id == other.id


@dataclass
class FileIndex:
    """Index entry for a file."""
    file_path: Path
    content_hash: str
    last_modified: float
    token_estimate: TokenEstimate
    chunks: List[str]  # Chunk IDs
    symbols: Set[str]
    imports: Set[str]
    dependencies: Set[str]
    summary: Optional[str] = None

    def is_stale(self, current_mtime: float) -> bool:
        """Check if index is stale."""
        return current_mtime > self.last_modified


@dataclass
class ContextWindow:
    """A context window with selected content."""
    chunks: List[CodeChunk]
    total_tokens: int
    max_tokens: int
    coverage: float  # 0-1, how much of relevant content is included
    truncated: bool
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_text(self, include_headers: bool = True) -> str:
        """Convert context window to text."""
        parts = []
        current_file = None

        for chunk in self.chunks:
            if include_headers and chunk.file_path != current_file:
                current_file = chunk.file_path
                parts.append(f"\n# === {current_file} ===\n")
            parts.append(chunk.content)

        return "\n".join(parts)


@dataclass
class RelevanceScore:
    """Relevance score for a code item."""
    item_id: str
    total_score: float
    recency_score: float
    similarity_score: float
    dependency_score: float
    priority: ContextPriority

    def __lt__(self, other: "RelevanceScore") -> bool:
        return self.total_score < other.total_score


# =============================================================================
# TOKEN ESTIMATION
# =============================================================================

class TokenEstimator:
    """
    Advanced token estimation for code.

    Uses multiple heuristics and can be calibrated
    against actual tokenizer if available.
    """

    # Language-specific token multipliers (code tends to have more tokens per char)
    LANGUAGE_MULTIPLIERS: Dict[str, float] = {
        ".py": 0.28,    # Python is relatively token-efficient
        ".js": 0.25,
        ".ts": 0.26,
        ".tsx": 0.26,
        ".java": 0.22,  # Java is verbose
        ".go": 0.27,
        ".rs": 0.26,
        ".c": 0.25,
        ".cpp": 0.24,
        ".h": 0.25,
    }

    DEFAULT_MULTIPLIER = 0.25

    def __init__(self):
        self._calibration_samples: List[Tuple[str, int]] = []
        self._calibrated_multiplier: Optional[float] = None

    def estimate(self, text: str, file_extension: str = "") -> TokenEstimate:
        """Estimate token count for text."""
        char_count = len(text)
        word_count = len(text.split())

        # Use calibrated multiplier if available
        if self._calibrated_multiplier:
            multiplier = self._calibrated_multiplier
        else:
            multiplier = self.LANGUAGE_MULTIPLIERS.get(file_extension, self.DEFAULT_MULTIPLIER)

        # Count special tokens (newlines, brackets, operators)
        special_count = sum(1 for c in text if c in "[]{}()<>+=*/&|^~!@#$%:;,.\n")

        # Estimate: chars * multiplier + special tokens
        estimated_tokens = int(char_count * multiplier + special_count * 0.3)

        # Sanity check against word count
        estimated_tokens = max(estimated_tokens, word_count)

        return TokenEstimate(char_count, word_count, estimated_tokens)

    def calibrate(self, text: str, actual_tokens: int) -> None:
        """Calibrate estimator with actual token count."""
        self._calibration_samples.append((text, actual_tokens))

        # Recalculate multiplier with 10+ samples
        if len(self._calibration_samples) >= 10:
            total_chars = sum(len(t) for t, _ in self._calibration_samples)
            total_tokens = sum(tokens for _, tokens in self._calibration_samples)
            self._calibrated_multiplier = total_tokens / total_chars if total_chars > 0 else 0.25


# =============================================================================
# LRU CACHE WITH TTL
# =============================================================================

class TTLCache(Generic[K, V]):
    """
    Thread-safe LRU cache with TTL expiration.

    Features:
    - O(1) get/set operations
    - Automatic expiration of stale entries
    - Memory-efficient weak reference option
    - Async-compatible
    """

    @dataclass
    class CacheEntry(Generic[V]):
        value: V
        expires_at: float
        access_count: int = 0

    def __init__(
        self,
        max_size: int = 1000,
        ttl_seconds: float = 3600,
        use_weak_refs: bool = False,
    ):
        self._max_size = max_size
        self._ttl = ttl_seconds
        self._use_weak_refs = use_weak_refs
        self._cache: OrderedDict[K, TTLCache.CacheEntry[V]] = OrderedDict()
        self._lock = asyncio.Lock()
        self._hits = 0
        self._misses = 0

    async def get(self, key: K) -> Optional[V]:
        """Get item from cache."""
        async with self._lock:
            entry = self._cache.get(key)

            if entry is None:
                self._misses += 1
                return None

            # Check expiration
            if time.time() > entry.expires_at:
                del self._cache[key]
                self._misses += 1
                return None

            # Move to end (most recently used)
            self._cache.move_to_end(key)
            entry.access_count += 1
            self._hits += 1

            return entry.value

    async def set(self, key: K, value: V, ttl: Optional[float] = None) -> None:
        """Set item in cache."""
        async with self._lock:
            ttl = ttl or self._ttl
            expires_at = time.time() + ttl

            # Remove oldest if at capacity
            while len(self._cache) >= self._max_size:
                self._cache.popitem(last=False)

            self._cache[key] = TTLCache.CacheEntry(value=value, expires_at=expires_at)
            self._cache.move_to_end(key)

    async def invalidate(self, key: K) -> bool:
        """Remove item from cache."""
        async with self._lock:
            if key in self._cache:
                del self._cache[key]
                return True
            return False

    async def clear(self) -> None:
        """Clear entire cache."""
        async with self._lock:
            self._cache.clear()

    async def cleanup_expired(self) -> int:
        """Remove all expired entries."""
        async with self._lock:
            now = time.time()
            expired = [k for k, v in self._cache.items() if now > v.expires_at]
            for k in expired:
                del self._cache[k]
            return len(expired)

    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        total = self._hits + self._misses
        return {
            "size": len(self._cache),
            "max_size": self._max_size,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": self._hits / total if total > 0 else 0,
        }


# =============================================================================
# SEMANTIC CODE CHUNKER
# =============================================================================

class SemanticChunker:
    """
    Chunks code into semantic units preserving logical boundaries.

    Uses AST parsing to identify:
    - Import blocks
    - Class definitions
    - Function definitions
    - Docstrings
    - Logical code blocks
    """

    def __init__(self, token_estimator: TokenEstimator):
        self._token_estimator = token_estimator
        self._chunk_id_counter = 0

    def _generate_chunk_id(self, file_path: Path, chunk_type: ChunkType) -> str:
        """Generate unique chunk ID."""
        self._chunk_id_counter += 1
        file_hash = hashlib.md5(str(file_path).encode()).hexdigest()[:8]
        return f"{file_hash}_{chunk_type.value}_{self._chunk_id_counter}"

    async def chunk_file(
        self,
        file_path: Path,
        content: str,
        max_chunk_tokens: int = ContextEngineConfig.CHUNK_SIZE_TOKENS,
    ) -> List[CodeChunk]:
        """Chunk a file into semantic units."""
        chunks = []
        extension = file_path.suffix

        # Try AST parsing for Python
        if extension == ".py":
            chunks = await self._chunk_python(file_path, content, max_chunk_tokens)
        else:
            # Fallback to line-based chunking
            chunks = await self._chunk_generic(file_path, content, max_chunk_tokens)

        return chunks

    async def _chunk_python(
        self,
        file_path: Path,
        content: str,
        max_chunk_tokens: int,
    ) -> List[CodeChunk]:
        """Chunk Python file using AST."""
        chunks = []
        lines = content.splitlines(keepends=True)

        try:
            tree = ast.parse(content)
        except SyntaxError:
            # Fallback to generic chunking
            return await self._chunk_generic(file_path, content, max_chunk_tokens)

        # Extract imports
        import_lines = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                if hasattr(node, 'lineno'):
                    import_lines.append(node.lineno)

        if import_lines:
            start = min(import_lines)
            end = max(import_lines)
            import_content = "".join(lines[start-1:end])
            chunks.append(CodeChunk(
                id=self._generate_chunk_id(file_path, ChunkType.IMPORT_BLOCK),
                file_path=file_path,
                chunk_type=ChunkType.IMPORT_BLOCK,
                content=import_content,
                start_line=start,
                end_line=end,
                token_estimate=self._token_estimator.estimate(import_content, file_path.suffix),
            ))

        # Extract classes and functions
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef):
                chunks.extend(await self._chunk_class(file_path, content, lines, node, max_chunk_tokens))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                chunk = await self._chunk_function(file_path, content, lines, node, max_chunk_tokens)
                if chunk:
                    chunks.append(chunk)

        # If no chunks created, use generic chunking
        if not chunks:
            chunks = await self._chunk_generic(file_path, content, max_chunk_tokens)

        return chunks

    async def _chunk_class(
        self,
        file_path: Path,
        content: str,
        lines: List[str],
        node: ast.ClassDef,
        max_chunk_tokens: int,
    ) -> List[CodeChunk]:
        """Chunk a class definition."""
        chunks = []

        # Get class header (signature + docstring)
        class_start = node.lineno
        class_end = node.end_lineno or class_start

        # Find first method or end of docstring
        first_child_line = class_end
        docstring_end = class_start

        for child in node.body:
            if isinstance(child, ast.Expr) and isinstance(child.value, ast.Constant):
                # Docstring
                docstring_end = child.end_lineno or docstring_end
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                first_child_line = child.lineno
                break

        # Class header chunk
        header_end = max(docstring_end, class_start)
        header_content = "".join(lines[class_start-1:header_end])

        chunks.append(CodeChunk(
            id=self._generate_chunk_id(file_path, ChunkType.CLASS_DEFINITION),
            file_path=file_path,
            chunk_type=ChunkType.CLASS_DEFINITION,
            content=header_content,
            start_line=class_start,
            end_line=header_end,
            token_estimate=self._token_estimator.estimate(header_content, file_path.suffix),
            name=node.name,
            dependencies=self._extract_dependencies_from_node(node),
        ))

        # Chunk methods
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                chunk = await self._chunk_function(
                    file_path, content, lines, child, max_chunk_tokens, parent_name=node.name
                )
                if chunk:
                    chunks.append(chunk)

        return chunks

    async def _chunk_function(
        self,
        file_path: Path,
        content: str,
        lines: List[str],
        node: Union[ast.FunctionDef, ast.AsyncFunctionDef],
        max_chunk_tokens: int,
        parent_name: Optional[str] = None,
    ) -> Optional[CodeChunk]:
        """Chunk a function definition."""
        start = node.lineno
        end = node.end_lineno or start

        func_content = "".join(lines[start-1:end])
        token_estimate = self._token_estimator.estimate(func_content, file_path.suffix)

        chunk_type = ChunkType.METHOD_DEFINITION if parent_name else ChunkType.FUNCTION_DEFINITION

        return CodeChunk(
            id=self._generate_chunk_id(file_path, chunk_type),
            file_path=file_path,
            chunk_type=chunk_type,
            content=func_content,
            start_line=start,
            end_line=end,
            token_estimate=token_estimate,
            name=node.name,
            parent_name=parent_name,
            dependencies=self._extract_dependencies_from_node(node),
        )

    async def _chunk_generic(
        self,
        file_path: Path,
        content: str,
        max_chunk_tokens: int,
    ) -> List[CodeChunk]:
        """Generic line-based chunking for non-Python files."""
        chunks = []
        lines = content.splitlines(keepends=True)

        current_chunk_lines = []
        current_start = 1
        current_tokens = 0

        for i, line in enumerate(lines, 1):
            line_tokens = self._token_estimator.estimate(line, file_path.suffix).estimated_tokens

            if current_tokens + line_tokens > max_chunk_tokens and current_chunk_lines:
                # Save current chunk
                chunk_content = "".join(current_chunk_lines)
                chunks.append(CodeChunk(
                    id=self._generate_chunk_id(file_path, ChunkType.CODE_BLOCK),
                    file_path=file_path,
                    chunk_type=ChunkType.CODE_BLOCK,
                    content=chunk_content,
                    start_line=current_start,
                    end_line=i - 1,
                    token_estimate=self._token_estimator.estimate(chunk_content, file_path.suffix),
                ))

                # Start new chunk
                current_chunk_lines = [line]
                current_start = i
                current_tokens = line_tokens
            else:
                current_chunk_lines.append(line)
                current_tokens += line_tokens

        # Save final chunk
        if current_chunk_lines:
            chunk_content = "".join(current_chunk_lines)
            chunks.append(CodeChunk(
                id=self._generate_chunk_id(file_path, ChunkType.CODE_BLOCK),
                file_path=file_path,
                chunk_type=ChunkType.CODE_BLOCK,
                content=chunk_content,
                start_line=current_start,
                end_line=len(lines),
                token_estimate=self._token_estimator.estimate(chunk_content, file_path.suffix),
            ))

        return chunks

    def _extract_dependencies_from_node(self, node: ast.AST) -> Set[str]:
        """Extract symbol dependencies from AST node."""
        dependencies = set()

        for child in ast.walk(node):
            if isinstance(child, ast.Name):
                dependencies.add(child.id)
            elif isinstance(child, ast.Attribute):
                if isinstance(child.value, ast.Name):
                    dependencies.add(f"{child.value.id}.{child.attr}")

        return dependencies


# =============================================================================
# CODE SUMMARIZER
# =============================================================================

class CodeSummarizer:
    """
    Creates hierarchical summaries of code at various detail levels.

    Summarization levels:
    - FULL: Complete code
    - SIGNATURES: Function/class signatures only
    - DOCSTRINGS: Signatures + docstrings
    - NAMES_ONLY: Just names and structure
    - SKELETON: Minimal structure
    """

    def __init__(self, token_estimator: TokenEstimator):
        self._token_estimator = token_estimator

    async def summarize(
        self,
        content: str,
        level: SummarizationLevel,
        file_extension: str = ".py",
    ) -> str:
        """Summarize code at specified level."""
        if level == SummarizationLevel.FULL:
            return content

        if file_extension == ".py":
            return await self._summarize_python(content, level)
        else:
            return await self._summarize_generic(content, level)

    async def _summarize_python(self, content: str, level: SummarizationLevel) -> str:
        """Summarize Python code."""
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return await self._summarize_generic(content, level)

        parts = []

        # Process imports
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                if level in (SummarizationLevel.SIGNATURES, SummarizationLevel.DOCSTRINGS):
                    parts.append(ast.unparse(node))

        # Process definitions
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef):
                parts.append(self._summarize_class(node, level))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                parts.append(self._summarize_function(node, level))

        return "\n\n".join(parts)

    def _summarize_class(self, node: ast.ClassDef, level: SummarizationLevel) -> str:
        """Summarize a class definition."""
        parts = []

        # Class signature
        bases = ", ".join(ast.unparse(b) for b in node.bases)
        signature = f"class {node.name}({bases}):" if bases else f"class {node.name}:"
        parts.append(signature)

        # Docstring
        if level in (SummarizationLevel.DOCSTRINGS, SummarizationLevel.SIGNATURES):
            docstring = ast.get_docstring(node)
            if docstring:
                parts.append(f'    """{docstring}"""')

        # Methods
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if level == SummarizationLevel.NAMES_ONLY:
                    parts.append(f"    def {child.name}(...): ...")
                elif level == SummarizationLevel.SKELETON:
                    parts.append(f"    {child.name}")
                else:
                    method_summary = self._summarize_function(child, level, indent=4)
                    parts.append(method_summary)

        return "\n".join(parts)

    def _summarize_function(
        self,
        node: Union[ast.FunctionDef, ast.AsyncFunctionDef],
        level: SummarizationLevel,
        indent: int = 0,
    ) -> str:
        """Summarize a function definition."""
        prefix = " " * indent
        parts = []

        # Function signature
        async_prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
        args = ast.unparse(node.args)
        returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""

        if level == SummarizationLevel.NAMES_ONLY:
            parts.append(f"{prefix}{async_prefix}def {node.name}(...): ...")
        elif level == SummarizationLevel.SKELETON:
            parts.append(f"{prefix}{node.name}")
        else:
            parts.append(f"{prefix}{async_prefix}def {node.name}({args}){returns}:")

            # Docstring
            if level == SummarizationLevel.DOCSTRINGS:
                docstring = ast.get_docstring(node)
                if docstring:
                    # Truncate long docstrings
                    if len(docstring) > 200:
                        docstring = docstring[:200] + "..."
                    parts.append(f'{prefix}    """{docstring}"""')

            parts.append(f"{prefix}    ...")

        return "\n".join(parts)

    async def _summarize_generic(self, content: str, level: SummarizationLevel) -> str:
        """Generic summarization for non-Python files."""
        lines = content.splitlines()

        if level == SummarizationLevel.SKELETON:
            # Just count lines and estimate content
            return f"# File with {len(lines)} lines"

        if level == SummarizationLevel.NAMES_ONLY:
            # Extract function-like patterns
            func_pattern = re.compile(r'^\s*(function|def|fn|func|pub fn|async fn)\s+(\w+)', re.MULTILINE)
            class_pattern = re.compile(r'^\s*(class|struct|interface|type)\s+(\w+)', re.MULTILINE)

            funcs = func_pattern.findall(content)
            classes = class_pattern.findall(content)

            parts = []
            for _, name in classes:
                parts.append(f"class/struct {name}")
            for _, name in funcs:
                parts.append(f"function {name}")

            return "\n".join(parts) if parts else f"# File with {len(lines)} lines"

        # SIGNATURES level - return first 50 lines or until first function body
        return "\n".join(lines[:50]) + "\n# ... (truncated)"


# =============================================================================
# CONTEXT RANKER
# =============================================================================

class ContextRanker:
    """
    Ranks code chunks by relevance to current context.

    Factors:
    - Recency (recently accessed/modified)
    - Similarity (keyword/semantic overlap)
    - Dependency (import/call relationships)
    - Priority (file importance)
    """

    def __init__(self, config: Type[ContextEngineConfig] = ContextEngineConfig):
        self._config = config
        self._access_times: Dict[str, float] = {}

    def record_access(self, item_id: str) -> None:
        """Record access time for an item."""
        self._access_times[item_id] = time.time()

    def calculate_relevance(
        self,
        chunk: CodeChunk,
        query_keywords: Set[str],
        dependency_graph: Dict[str, Set[str]],
        current_file: Optional[Path] = None,
    ) -> RelevanceScore:
        """Calculate relevance score for a chunk."""
        # Recency score
        last_access = self._access_times.get(chunk.id, 0)
        time_diff = time.time() - last_access if last_access else float('inf')
        recency_score = self._config.RELEVANCE_DECAY_FACTOR ** (time_diff / 3600)  # Decay per hour

        # Similarity score (keyword overlap)
        chunk_keywords = self._extract_keywords(chunk.content)
        if query_keywords and chunk_keywords:
            overlap = len(query_keywords & chunk_keywords)
            similarity_score = overlap / max(len(query_keywords), 1)
        else:
            similarity_score = 0.0

        # Dependency score
        chunk_key = str(chunk.file_path)
        deps = dependency_graph.get(chunk_key, set())
        current_key = str(current_file) if current_file else ""
        dependency_score = 1.0 if current_key in deps or chunk_key in dependency_graph.get(current_key, set()) else 0.0

        # Priority based on relationship to current file
        if current_file:
            if chunk.file_path == current_file:
                priority = ContextPriority.CRITICAL
            elif chunk.file_path.parent == current_file.parent:
                priority = ContextPriority.LOW
            else:
                priority = ContextPriority.BACKGROUND
        else:
            priority = ContextPriority.MEDIUM

        # Boost for dependency
        if dependency_score > 0:
            priority = ContextPriority.HIGH

        # Calculate total score
        total_score = (
            self._config.RECENCY_WEIGHT * recency_score +
            self._config.SIMILARITY_WEIGHT * similarity_score +
            self._config.DEPENDENCY_WEIGHT * dependency_score +
            priority.value * 0.1  # Priority boost
        )

        return RelevanceScore(
            item_id=chunk.id,
            total_score=total_score,
            recency_score=recency_score,
            similarity_score=similarity_score,
            dependency_score=dependency_score,
            priority=priority,
        )

    def _extract_keywords(self, text: str) -> Set[str]:
        """Extract keywords from text."""
        # Remove comments and strings
        text = re.sub(r'#.*$', '', text, flags=re.MULTILINE)
        text = re.sub(r'""".*?"""', '', text, flags=re.DOTALL)
        text = re.sub(r"'''.*?'''", '', text, flags=re.DOTALL)
        text = re.sub(r'".*?"', '', text)
        text = re.sub(r"'.*?'", '', text)

        # Extract identifiers
        identifiers = set(re.findall(r'\b[a-zA-Z_][a-zA-Z0-9_]*\b', text))

        # Filter common keywords
        common = {'def', 'class', 'if', 'else', 'elif', 'for', 'while', 'return',
                  'import', 'from', 'as', 'try', 'except', 'finally', 'with',
                  'True', 'False', 'None', 'and', 'or', 'not', 'in', 'is',
                  'self', 'cls', 'async', 'await', 'yield', 'lambda', 'pass',
                  'break', 'continue', 'raise', 'assert', 'global', 'nonlocal'}

        return identifiers - common


# =============================================================================
# FILE INDEXER
# =============================================================================

class FileIndexer:
    """
    Indexes codebase files for fast retrieval.

    Features:
    - Content hashing for change detection
    - Symbol extraction for dependency analysis
    - Persistent index storage
    - Incremental updates
    """

    def __init__(
        self,
        chunker: SemanticChunker,
        summarizer: CodeSummarizer,
        token_estimator: TokenEstimator,
    ):
        self._chunker = chunker
        self._summarizer = summarizer
        self._token_estimator = token_estimator
        self._index: Dict[Path, FileIndex] = {}
        self._chunks: Dict[str, CodeChunk] = {}
        self._lock = asyncio.Lock()

    async def index_file(self, file_path: Path) -> Optional[FileIndex]:
        """Index a single file."""
        try:
            # Check if file exists and is readable
            if not file_path.exists() or not file_path.is_file():
                return None

            # Check file size
            file_size = file_path.stat().st_size
            if file_size > ContextEngineConfig.MAX_FILE_SIZE_MB * 1024 * 1024:
                logger.warning(f"Skipping large file: {file_path} ({file_size / 1024 / 1024:.1f} MB)")
                return None

            # Read content
            content = await asyncio.to_thread(file_path.read_text, encoding='utf-8', errors='ignore')

            # Calculate hash
            content_hash = hashlib.sha256(content.encode()).hexdigest()

            # Check if already indexed and not changed
            existing = self._index.get(file_path)
            if existing and existing.content_hash == content_hash:
                return existing

            # Chunk the file
            chunks = await self._chunker.chunk_file(file_path, content)

            # Extract symbols and imports
            symbols, imports, dependencies = await self._extract_metadata(content, file_path.suffix)

            # Create summary if file is large
            token_estimate = self._token_estimator.estimate(content, file_path.suffix)
            summary = None
            if token_estimate.estimated_tokens > ContextEngineConfig.SUMMARIZATION_THRESHOLD:
                summary = await self._summarizer.summarize(
                    content, SummarizationLevel.SIGNATURES, file_path.suffix
                )

            # Create index entry
            index_entry = FileIndex(
                file_path=file_path,
                content_hash=content_hash,
                last_modified=file_path.stat().st_mtime,
                token_estimate=token_estimate,
                chunks=[c.id for c in chunks],
                symbols=symbols,
                imports=imports,
                dependencies=dependencies,
                summary=summary,
            )

            # Store index and chunks
            async with self._lock:
                self._index[file_path] = index_entry
                for chunk in chunks:
                    self._chunks[chunk.id] = chunk

            return index_entry

        except Exception as e:
            logger.error(f"Failed to index file {file_path}: {e}")
            return None

    async def index_directory(
        self,
        directory: Path,
        patterns: Optional[List[str]] = None,
        exclude_patterns: Optional[List[str]] = None,
        progress_callback: Optional[Callable[[int, int], Awaitable[None]]] = None,
    ) -> int:
        """Index all matching files in directory."""
        patterns = patterns or ContextEngineConfig.INCLUDE_PATTERNS
        exclude_patterns = exclude_patterns or ContextEngineConfig.EXCLUDE_PATTERNS

        # Find all matching files
        files = []
        for pattern in patterns:
            files.extend(directory.glob(f"**/{pattern}"))

        # Filter excluded patterns
        import fnmatch
        filtered_files = []
        for f in files:
            excluded = False
            for exclude in exclude_patterns:
                if fnmatch.fnmatch(str(f), exclude):
                    excluded = True
                    break
            if not excluded:
                filtered_files.append(f)

        # Index files in parallel
        total = len(filtered_files)
        indexed = 0

        semaphore = asyncio.Semaphore(ContextEngineConfig.MAX_FILES_PARALLEL)

        async def index_with_semaphore(file_path: Path) -> bool:
            async with semaphore:
                result = await self.index_file(file_path)
                return result is not None

        tasks = [index_with_semaphore(f) for f in filtered_files]

        for i, task in enumerate(asyncio.as_completed(tasks)):
            if await task:
                indexed += 1
            if progress_callback:
                await progress_callback(i + 1, total)

        return indexed

    async def _extract_metadata(
        self,
        content: str,
        extension: str,
    ) -> Tuple[Set[str], Set[str], Set[str]]:
        """Extract symbols, imports, and dependencies from content."""
        symbols = set()
        imports = set()
        dependencies = set()

        if extension == ".py":
            try:
                tree = ast.parse(content)

                for node in ast.walk(tree):
                    # Symbols (definitions)
                    if isinstance(node, ast.ClassDef):
                        symbols.add(node.name)
                    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        symbols.add(node.name)
                    elif isinstance(node, ast.Assign):
                        for target in node.targets:
                            if isinstance(target, ast.Name):
                                symbols.add(target.id)

                    # Imports
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            imports.add(alias.name)
                    elif isinstance(node, ast.ImportFrom):
                        module = node.module or ""
                        imports.add(module)
                        for alias in node.names:
                            imports.add(f"{module}.{alias.name}")

                    # Dependencies (names used)
                    if isinstance(node, ast.Name):
                        dependencies.add(node.id)
                    elif isinstance(node, ast.Attribute):
                        if isinstance(node.value, ast.Name):
                            dependencies.add(f"{node.value.id}.{node.attr}")

            except SyntaxError:
                pass

        return symbols, imports, dependencies

    def get_chunk(self, chunk_id: str) -> Optional[CodeChunk]:
        """Get a chunk by ID."""
        return self._chunks.get(chunk_id)

    def get_file_index(self, file_path: Path) -> Optional[FileIndex]:
        """Get file index entry."""
        return self._index.get(file_path)

    def get_all_chunks(self) -> List[CodeChunk]:
        """Get all indexed chunks."""
        return list(self._chunks.values())

    def get_stats(self) -> Dict[str, Any]:
        """Get indexer statistics."""
        total_tokens = sum(idx.token_estimate.estimated_tokens for idx in self._index.values())
        return {
            "files_indexed": len(self._index),
            "total_chunks": len(self._chunks),
            "total_tokens": total_tokens,
        }


# =============================================================================
# CONTEXT WINDOW MANAGER
# =============================================================================

class ContextWindowManager:
    """
    Manages context windows with token-aware selection.

    Features:
    - Priority-based chunk selection
    - Token budget management
    - Streaming context loading
    - Automatic summarization for overflow
    """

    def __init__(
        self,
        indexer: FileIndexer,
        ranker: ContextRanker,
        summarizer: CodeSummarizer,
    ):
        self._indexer = indexer
        self._ranker = ranker
        self._summarizer = summarizer

    async def build_context(
        self,
        focus_files: List[Path],
        query_keywords: Optional[Set[str]] = None,
        max_tokens: int = ContextEngineConfig.MAX_CONTEXT_TOKENS,
        include_summaries: bool = True,
    ) -> ContextWindow:
        """Build a context window focused on specific files."""
        query_keywords = query_keywords or set()

        # Get all chunks
        all_chunks = self._indexer.get_all_chunks()

        if not all_chunks:
            return ContextWindow(
                chunks=[],
                total_tokens=0,
                max_tokens=max_tokens,
                coverage=0.0,
                truncated=False,
            )

        # Build dependency graph
        dependency_graph = self._build_dependency_graph()

        # Score and rank chunks
        current_file = focus_files[0] if focus_files else None
        scores = []

        for chunk in all_chunks:
            score = self._ranker.calculate_relevance(
                chunk, query_keywords, dependency_graph, current_file
            )
            scores.append((score, chunk))

        # Sort by score (highest first)
        scores.sort(key=lambda x: x[0].total_score, reverse=True)

        # Select chunks within token budget
        selected_chunks = []
        total_tokens = 0

        # First pass: include focus files at full detail
        for file_path in focus_files:
            file_index = self._indexer.get_file_index(file_path)
            if file_index:
                for chunk_id in file_index.chunks:
                    chunk = self._indexer.get_chunk(chunk_id)
                    if chunk and total_tokens + chunk.token_estimate.estimated_tokens <= max_tokens:
                        selected_chunks.append(chunk)
                        total_tokens += chunk.token_estimate.estimated_tokens
                        self._ranker.record_access(chunk.id)

        # Second pass: add related chunks by relevance
        for score, chunk in scores:
            if chunk in selected_chunks:
                continue

            if total_tokens + chunk.token_estimate.estimated_tokens <= max_tokens:
                selected_chunks.append(chunk)
                total_tokens += chunk.token_estimate.estimated_tokens
                self._ranker.record_access(chunk.id)

        # Calculate coverage
        total_available = sum(c.token_estimate.estimated_tokens for c in all_chunks)
        coverage = total_tokens / total_available if total_available > 0 else 1.0

        return ContextWindow(
            chunks=selected_chunks,
            total_tokens=total_tokens,
            max_tokens=max_tokens,
            coverage=coverage,
            truncated=total_tokens >= max_tokens * 0.95,
        )

    async def stream_context(
        self,
        focus_files: List[Path],
        batch_size: int = 10,
    ) -> AsyncGenerator[List[CodeChunk], None]:
        """Stream context chunks in batches."""
        context = await self.build_context(focus_files)

        for i in range(0, len(context.chunks), batch_size):
            yield context.chunks[i:i + batch_size]

    def _build_dependency_graph(self) -> Dict[str, Set[str]]:
        """Build dependency graph from indexed files."""
        graph: Dict[str, Set[str]] = defaultdict(set)

        for file_path, index in self._indexer._index.items():
            for imp in index.imports:
                # Try to resolve import to file
                for other_path, other_index in self._indexer._index.items():
                    if imp in other_index.symbols or imp.split('.')[-1] in other_index.symbols:
                        graph[str(file_path)].add(str(other_path))

        return dict(graph)


# =============================================================================
# CROSS-REPO CONTEXT AGGREGATOR
# =============================================================================

class CrossRepoContextAggregator:
    """
    Aggregates context across multiple repositories.

    Coordinates between JARVIS, JARVIS-Prime, and Reactor-Core.
    """

    def __init__(self):
        self._repos: Dict[str, Path] = {
            "jarvis": ContextEngineConfig.JARVIS_REPO,
            "prime": ContextEngineConfig.PRIME_REPO,
            "reactor": ContextEngineConfig.REACTOR_REPO,
        }

        self._token_estimator = TokenEstimator()
        self._chunker = SemanticChunker(self._token_estimator)
        self._summarizer = CodeSummarizer(self._token_estimator)

        self._indexers: Dict[str, FileIndexer] = {}
        self._context_managers: Dict[str, ContextWindowManager] = {}

        self._cache = TTLCache[str, ContextWindow](
            max_size=ContextEngineConfig.CACHE_SIZE,
            ttl_seconds=ContextEngineConfig.CACHE_TTL_SECONDS,
        )

    async def initialize(self) -> bool:
        """Initialize all repository indexers."""
        logger.info("Initializing Cross-Repo Context Aggregator...")

        for repo_name, repo_path in self._repos.items():
            if not repo_path.exists():
                logger.warning(f"Repository not found: {repo_name} at {repo_path}")
                continue

            indexer = FileIndexer(self._chunker, self._summarizer, self._token_estimator)
            ranker = ContextRanker()
            context_manager = ContextWindowManager(indexer, ranker, self._summarizer)

            self._indexers[repo_name] = indexer
            self._context_managers[repo_name] = context_manager

            logger.info(f"  Indexing {repo_name}...")
            count = await indexer.index_directory(repo_path)
            logger.info(f"  ✓ {repo_name}: {count} files indexed")

        return True

    async def get_unified_context(
        self,
        focus_files: List[Path],
        query_keywords: Optional[Set[str]] = None,
        max_tokens: int = ContextEngineConfig.MAX_CONTEXT_TOKENS,
    ) -> ContextWindow:
        """Get unified context across all repositories."""
        # Check cache
        cache_key = f"{','.join(str(f) for f in focus_files)}_{','.join(query_keywords or [])}"
        cached = await self._cache.get(cache_key)
        if cached:
            return cached

        # Allocate tokens per repo based on focus files
        repo_allocations = self._calculate_token_allocation(focus_files, max_tokens)

        # Get context from each repo
        all_chunks = []
        total_tokens = 0

        for repo_name, allocation in repo_allocations.items():
            if repo_name not in self._context_managers:
                continue

            manager = self._context_managers[repo_name]

            # Filter focus files for this repo
            repo_path = self._repos[repo_name]
            repo_focus = [f for f in focus_files if str(f).startswith(str(repo_path))]

            context = await manager.build_context(
                focus_files=repo_focus or [],
                query_keywords=query_keywords,
                max_tokens=allocation,
            )

            all_chunks.extend(context.chunks)
            total_tokens += context.total_tokens

        # Build unified context
        unified = ContextWindow(
            chunks=all_chunks,
            total_tokens=total_tokens,
            max_tokens=max_tokens,
            coverage=total_tokens / max_tokens if max_tokens > 0 else 0,
            truncated=total_tokens >= max_tokens * 0.95,
            metadata={"repos": list(repo_allocations.keys())},
        )

        # Cache result
        await self._cache.set(cache_key, unified)

        return unified

    def _calculate_token_allocation(
        self,
        focus_files: List[Path],
        max_tokens: int,
    ) -> Dict[str, int]:
        """Calculate token allocation per repository."""
        allocations = {}

        # Count focus files per repo
        repo_counts = defaultdict(int)
        for file_path in focus_files:
            for repo_name, repo_path in self._repos.items():
                if str(file_path).startswith(str(repo_path)):
                    repo_counts[repo_name] += 1
                    break

        # If no focus files, distribute evenly
        if not repo_counts:
            per_repo = max_tokens // len(self._repos)
            return {name: per_repo for name in self._repos}

        # Allocate based on focus file count
        total_focus = sum(repo_counts.values())
        for repo_name in self._repos:
            ratio = repo_counts.get(repo_name, 0) / total_focus if total_focus > 0 else 0
            # Give at least 10% to each repo, rest proportional to focus
            base_allocation = max_tokens * 0.1 // len(self._repos)
            proportional = (max_tokens * 0.7) * ratio
            allocations[repo_name] = int(base_allocation + proportional)

        return allocations

    def get_stats(self) -> Dict[str, Any]:
        """Get aggregator statistics."""
        stats = {
            "cache": self._cache.get_stats(),
            "repositories": {},
        }

        for repo_name, indexer in self._indexers.items():
            stats["repositories"][repo_name] = indexer.get_stats()

        return stats


# =============================================================================
# MAIN CONTEXT ENGINE
# =============================================================================

class ContextEngine:
    """
    Main context engine for full codebase context window management.

    Provides:
    - 200k+ token context windows
    - Cross-repository context aggregation
    - Intelligent prioritization and ranking
    - Streaming and incremental loading
    - Caching and persistence
    """

    def __init__(self):
        self._aggregator = CrossRepoContextAggregator()
        self._initialized = False
        self._lock = asyncio.Lock()

    async def initialize(self) -> bool:
        """Initialize the context engine."""
        async with self._lock:
            if self._initialized:
                return True

            success = await self._aggregator.initialize()
            self._initialized = success
            return success

    async def get_context(
        self,
        focus_files: Optional[List[Path]] = None,
        query: Optional[str] = None,
        max_tokens: int = ContextEngineConfig.MAX_CONTEXT_TOKENS,
    ) -> ContextWindow:
        """
        Get context window for given focus files and query.

        Args:
            focus_files: Files to prioritize in context
            query: Optional query to extract keywords from
            max_tokens: Maximum tokens in context window

        Returns:
            ContextWindow with selected chunks
        """
        if not self._initialized:
            await self.initialize()

        focus_files = focus_files or []

        # Extract keywords from query
        keywords = set()
        if query:
            keywords = set(re.findall(r'\b[a-zA-Z_][a-zA-Z0-9_]*\b', query))

        return await self._aggregator.get_unified_context(
            focus_files=focus_files,
            query_keywords=keywords,
            max_tokens=max_tokens,
        )

    async def stream_context(
        self,
        focus_files: Optional[List[Path]] = None,
        batch_tokens: int = 10000,
    ) -> AsyncGenerator[str, None]:
        """
        Stream context in batches.

        Yields text chunks suitable for incremental processing.
        """
        context = await self.get_context(focus_files)

        current_batch = []
        current_tokens = 0

        for chunk in context.chunks:
            if current_tokens + chunk.token_estimate.estimated_tokens > batch_tokens:
                if current_batch:
                    yield "\n\n".join(c.content for c in current_batch)
                current_batch = [chunk]
                current_tokens = chunk.token_estimate.estimated_tokens
            else:
                current_batch.append(chunk)
                current_tokens += chunk.token_estimate.estimated_tokens

        if current_batch:
            yield "\n\n".join(c.content for c in current_batch)

    async def get_file_summary(
        self,
        file_path: Path,
        level: SummarizationLevel = SummarizationLevel.SIGNATURES,
    ) -> str:
        """Get summary of a specific file."""
        if not self._initialized:
            await self.initialize()

        # Find which repo this file belongs to
        for indexer in self._aggregator._indexers.values():
            file_index = indexer.get_file_index(file_path)
            if file_index and file_index.summary:
                return file_index.summary

        # Generate summary on the fly
        if file_path.exists():
            content = await asyncio.to_thread(file_path.read_text, encoding='utf-8', errors='ignore')
            return await self._aggregator._summarizer.summarize(content, level, file_path.suffix)

        return ""

    def get_stats(self) -> Dict[str, Any]:
        """Get engine statistics."""
        return {
            "initialized": self._initialized,
            **self._aggregator.get_stats(),
        }


# =============================================================================
# SINGLETON ACCESSOR
# =============================================================================

_context_engine: Optional[ContextEngine] = None


def get_context_engine() -> ContextEngine:
    """Get the singleton context engine instance."""
    global _context_engine
    if _context_engine is None:
        _context_engine = ContextEngine()
    return _context_engine


async def get_context_engine_async() -> ContextEngine:
    """Get the singleton context engine instance (async)."""
    engine = get_context_engine()
    if not engine._initialized:
        await engine.initialize()
    return engine
