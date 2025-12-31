"""
Repository Intelligence System for JARVIS Ecosystem
====================================================

Advanced codebase understanding system that provides:
- Tree-sitter based code parsing for symbol extraction
- NetworkX graph-based code relationship analysis
- PageRank-based file importance ranking
- Cross-repository awareness (JARVIS, JARVIS Prime, Reactor Core)
- Intelligent caching with mtime invalidation
- LangGraph reasoning for architectural decisions
- Async-first parallel processing

Inspired by patterns from:
- Aider: Repository mapping with tree-sitter and PageRank
- Open Interpreter: Async tool execution with streaming
- MetaGPT: Structured workflow enforcement

Author: JARVIS AI System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import time
from abc import ABC, abstractmethod
from collections import defaultdict, Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import (
    Any, AsyncIterator, Awaitable, Callable, Dict, FrozenSet, Generator,
    List, Literal, NamedTuple, Optional, Protocol, Sequence, Set, Tuple,
    Type, TypeVar, Union
)
from uuid import uuid4

try:
    import networkx as nx
    NETWORKX_AVAILABLE = True
except ImportError:
    nx = None
    NETWORKX_AVAILABLE = False

try:
    from langgraph.graph import StateGraph, END
    from langgraph.checkpoint.memory import MemorySaver
    LANGGRAPH_AVAILABLE = True
except ImportError:
    LANGGRAPH_AVAILABLE = False
    StateGraph = None
    END = "end"
    MemorySaver = None

try:
    from tree_sitter import Language, Parser
    from tree_sitter_languages import get_language, get_parser
    TREE_SITTER_AVAILABLE = True
except ImportError:
    TREE_SITTER_AVAILABLE = False
    Language = None
    Parser = None
    get_language = None
    get_parser = None

try:
    from diskcache import Cache
    DISKCACHE_AVAILABLE = True
except ImportError:
    DISKCACHE_AVAILABLE = False
    Cache = dict

# Pydantic is optional - used for LangGraph state management
try:
    from pydantic import BaseModel, Field as PydanticField
    PYDANTIC_AVAILABLE = True
except ImportError:
    PYDANTIC_AVAILABLE = False
    BaseModel = None  # Will use dataclass fallback
    PydanticField = None

logger = logging.getLogger(__name__)


# ============================================================================
# Configuration - Environment Driven (No Hardcoding)
# ============================================================================

def _get_env(key: str, default: str = "") -> str:
    """Get environment variable with default."""
    return os.environ.get(key, default)


def _get_env_path(key: str, default: str = "") -> Path:
    """Get environment variable as Path."""
    return Path(os.path.expanduser(_get_env(key, default)))


def _get_env_int(key: str, default: int) -> int:
    """Get environment variable as integer."""
    try:
        return int(_get_env(key, str(default)))
    except ValueError:
        return default


def _get_env_float(key: str, default: float) -> float:
    """Get environment variable as float."""
    try:
        return float(_get_env(key, str(default)))
    except ValueError:
        return default


def _get_env_bool(key: str, default: bool = False) -> bool:
    """Get environment variable as boolean."""
    val = _get_env(key, str(default)).lower()
    return val in ("true", "1", "yes", "on")


@dataclass
class RepositoryConfig:
    """Configuration for a single repository."""
    name: str
    path: Path
    enabled: bool = True
    include_patterns: List[str] = field(default_factory=lambda: ["**/*.py", "**/*.ts", "**/*.js"])
    exclude_patterns: List[str] = field(default_factory=lambda: [
        "**/node_modules/**", "**/__pycache__/**", "**/venv/**",
        "**/.git/**", "**/dist/**", "**/build/**"
    ])
    max_file_size_kb: int = 500
    priority: int = 1  # Higher = more important

    @classmethod
    def from_env(cls, name: str, path_env_key: str, default_path: str) -> "RepositoryConfig":
        """Create config from environment variables."""
        return cls(
            name=name,
            path=_get_env_path(path_env_key, default_path),
            enabled=_get_env_bool(f"REPO_INTEL_{name.upper()}_ENABLED", True),
            max_file_size_kb=_get_env_int(f"REPO_INTEL_{name.upper()}_MAX_SIZE_KB", 500),
            priority=_get_env_int(f"REPO_INTEL_{name.upper()}_PRIORITY", 1),
        )


@dataclass
class RepositoryIntelligenceConfig:
    """Configuration for the Repository Intelligence system."""
    # Cache settings
    cache_dir: Path = field(default_factory=lambda: _get_env_path(
        "REPO_INTEL_CACHE_DIR", "~/.jarvis/repo_intelligence_cache"
    ))
    cache_ttl_hours: int = field(default_factory=lambda: _get_env_int(
        "REPO_INTEL_CACHE_TTL_HOURS", 24
    ))

    # Map settings
    max_map_tokens: int = field(default_factory=lambda: _get_env_int(
        "REPO_INTEL_MAX_MAP_TOKENS", 4096
    ))
    max_files_per_repo: int = field(default_factory=lambda: _get_env_int(
        "REPO_INTEL_MAX_FILES", 1000
    ))

    # Graph settings
    pagerank_damping: float = field(default_factory=lambda: _get_env_float(
        "REPO_INTEL_PAGERANK_DAMPING", 0.85
    ))

    # Cross-repo settings
    cross_repo_state_dir: Path = field(default_factory=lambda: _get_env_path(
        "JARVIS_CROSS_REPO_DIR", "~/.jarvis/cross_repo"
    ))

    # Parallel settings
    max_concurrent_parses: int = field(default_factory=lambda: _get_env_int(
        "REPO_INTEL_MAX_CONCURRENT", 10
    ))

    # Repository configurations
    repositories: Dict[str, RepositoryConfig] = field(default_factory=dict)

    def __post_init__(self):
        """Initialize repository configurations if not provided."""
        if not self.repositories:
            self.repositories = {
                "jarvis": RepositoryConfig.from_env(
                    "jarvis",
                    "JARVIS_REPO_PATH",
                    "~/Documents/repos/JARVIS-AI-Agent"
                ),
                "jarvis_prime": RepositoryConfig.from_env(
                    "jarvis_prime",
                    "JARVIS_PRIME_REPO_PATH",
                    "~/Documents/repos/jarvis-prime"
                ),
                "reactor_core": RepositoryConfig.from_env(
                    "reactor_core",
                    "REACTOR_CORE_REPO_PATH",
                    "~/Documents/repos/reactor-core"
                ),
            }


# ============================================================================
# Enums and Types
# ============================================================================

class SymbolKind(str, Enum):
    """Types of code symbols."""
    FUNCTION = "function"
    CLASS = "class"
    METHOD = "method"
    VARIABLE = "variable"
    CONSTANT = "constant"
    IMPORT = "import"
    MODULE = "module"
    INTERFACE = "interface"
    TYPE = "type"
    DECORATOR = "decorator"


class RelationshipType(str, Enum):
    """Types of code relationships."""
    IMPORTS = "imports"
    DEFINES = "defines"
    CALLS = "calls"
    INHERITS = "inherits"
    IMPLEMENTS = "implements"
    USES = "uses"
    REFERENCES = "references"


class MapRefreshMode(str, Enum):
    """How to refresh the repository map."""
    AUTO = "auto"           # Refresh if stale
    MANUAL = "manual"       # Only refresh on explicit request
    ALWAYS = "always"       # Always refresh
    NEVER = "never"         # Never refresh (use cache)


# ============================================================================
# Data Classes (Immutable for Audit Trails - like Open Interpreter)
# ============================================================================

@dataclass(frozen=True)
class CodeSymbol:
    """A code symbol (function, class, variable, etc.)."""
    name: str
    kind: SymbolKind
    file_path: str
    line_number: int
    end_line: int
    signature: Optional[str] = None
    docstring: Optional[str] = None
    parent_symbol: Optional[str] = None

    def __hash__(self):
        return hash((self.name, self.file_path, self.line_number))


@dataclass(frozen=True)
class CodeRelationship:
    """A relationship between code symbols."""
    source: str  # file path or symbol
    target: str  # file path or symbol
    relationship_type: RelationshipType
    weight: float = 1.0
    line_number: Optional[int] = None

    def __hash__(self):
        return hash((self.source, self.target, self.relationship_type))


@dataclass(frozen=True)
class FileInfo:
    """Information about a source file."""
    path: str
    relative_path: str
    repository: str
    size_bytes: int
    mtime: float
    line_count: int
    language: str
    symbols_count: int
    imports_count: int

    def __hash__(self):
        return hash((self.path, self.mtime))


class Tag(NamedTuple):
    """A code tag (definition or reference)."""
    rel_fname: str
    fname: str
    line: int
    name: str
    kind: Literal["def", "ref"]


# ============================================================================
# Result Types (like Open Interpreter's ToolResult)
# ============================================================================

@dataclass(frozen=True)
class IntelligenceResult:
    """Result from a repository intelligence operation."""
    output: Optional[str] = None
    error: Optional[str] = None
    data: Optional[Dict[str, Any]] = None
    system: Optional[str] = None  # System-level metadata
    timestamp: datetime = field(default_factory=datetime.utcnow)

    def __bool__(self) -> bool:
        return any([self.output, self.data]) and not self.error

    def to_dict(self) -> Dict[str, Any]:
        return {
            "output": self.output,
            "error": self.error,
            "data": self.data,
            "system": self.system,
            "timestamp": self.timestamp.isoformat(),
        }


@dataclass
class RepoMapResult:
    """Result of generating a repository map."""
    map_content: str
    token_count: int
    files_included: int
    symbols_extracted: int
    generation_time_ms: float
    cache_hit: bool
    repository: str
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class CrossRepoAnalysis:
    """Cross-repository analysis result."""
    repositories: List[str]
    shared_symbols: List[str]
    dependency_graph: Dict[str, List[str]]
    integration_points: List[Dict[str, Any]]
    recommendations: List[str]
    analysis_time_ms: float


# ============================================================================
# Cache Implementation
# ============================================================================

class IntelligenceCache:
    """Intelligent cache with mtime invalidation and async support."""

    def __init__(self, cache_dir: Path, ttl_hours: int = 24):
        self.cache_dir = cache_dir
        self.ttl = timedelta(hours=ttl_hours)
        self._memory_cache: Dict[str, Tuple[Any, datetime, float]] = {}
        self._disk_cache: Optional[Cache] = None

        # Initialize disk cache if available
        if DISKCACHE_AVAILABLE:
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                self._disk_cache = Cache(str(cache_dir))
            except Exception as e:
                logger.warning(f"Failed to initialize disk cache: {e}")

    def _make_key(self, *args, **kwargs) -> str:
        """Create a cache key from arguments."""
        key_data = json.dumps((args, sorted(kwargs.items())), sort_keys=True, default=str)
        return hashlib.sha256(key_data.encode()).hexdigest()[:32]

    async def get(
        self,
        key: str,
        file_path: Optional[Path] = None,
    ) -> Optional[Any]:
        """Get value from cache, checking mtime if file_path provided."""
        # Check memory cache first
        if key in self._memory_cache:
            value, cached_at, cached_mtime = self._memory_cache[key]

            # Check TTL
            if datetime.utcnow() - cached_at > self.ttl:
                del self._memory_cache[key]
                return None

            # Check mtime if file path provided
            if file_path and file_path.exists():
                current_mtime = file_path.stat().st_mtime
                if current_mtime > cached_mtime:
                    del self._memory_cache[key]
                    return None

            return value

        # Check disk cache
        if self._disk_cache:
            try:
                cached = self._disk_cache.get(key)
                if cached:
                    value, cached_at_str, cached_mtime = cached
                    cached_at = datetime.fromisoformat(cached_at_str)

                    if datetime.utcnow() - cached_at > self.ttl:
                        del self._disk_cache[key]
                        return None

                    if file_path and file_path.exists():
                        current_mtime = file_path.stat().st_mtime
                        if current_mtime > cached_mtime:
                            del self._disk_cache[key]
                            return None

                    # Promote to memory cache
                    self._memory_cache[key] = (value, cached_at, cached_mtime)
                    return value
            except Exception as e:
                logger.warning(f"Cache read error: {e}")

        return None

    async def set(
        self,
        key: str,
        value: Any,
        file_path: Optional[Path] = None,
    ) -> None:
        """Set value in cache with optional file mtime tracking."""
        mtime = file_path.stat().st_mtime if file_path and file_path.exists() else 0.0
        cached_at = datetime.utcnow()

        # Store in memory
        self._memory_cache[key] = (value, cached_at, mtime)

        # Store on disk
        if self._disk_cache:
            try:
                self._disk_cache[key] = (value, cached_at.isoformat(), mtime)
            except Exception as e:
                logger.warning(f"Cache write error: {e}")

    async def invalidate(self, key: str) -> None:
        """Invalidate a cache entry."""
        self._memory_cache.pop(key, None)
        if self._disk_cache:
            try:
                del self._disk_cache[key]
            except KeyError:
                pass

    async def clear(self) -> None:
        """Clear all cache entries."""
        self._memory_cache.clear()
        if self._disk_cache:
            self._disk_cache.clear()


# ============================================================================
# Code Parser (Tree-sitter based, like Aider)
# ============================================================================

class CodeParser:
    """Parse code files to extract symbols and relationships."""

    LANGUAGE_MAP = {
        ".py": "python",
        ".js": "javascript",
        ".ts": "typescript",
        ".tsx": "tsx",
        ".jsx": "javascript",
        ".go": "go",
        ".rs": "rust",
        ".java": "java",
        ".c": "c",
        ".cpp": "cpp",
        ".h": "c",
        ".hpp": "cpp",
        ".rb": "ruby",
        ".swift": "swift",
        ".kt": "kotlin",
    }

    def __init__(self):
        self._parsers: Dict[str, Any] = {}
        self._languages: Dict[str, Any] = {}

    def _get_parser(self, language: str) -> Optional[Any]:
        """Get or create a parser for a language."""
        if not TREE_SITTER_AVAILABLE:
            return None

        if language not in self._parsers:
            try:
                self._parsers[language] = get_parser(language)
                self._languages[language] = get_language(language)
            except Exception as e:
                logger.debug(f"No parser for {language}: {e}")
                return None

        return self._parsers.get(language)

    def get_language(self, file_path: Path) -> Optional[str]:
        """Determine language from file extension."""
        return self.LANGUAGE_MAP.get(file_path.suffix.lower())

    async def parse_file(self, file_path: Path) -> List[Tag]:
        """Parse a file and extract tags (definitions and references)."""
        language = self.get_language(file_path)
        if not language:
            return []

        parser = self._get_parser(language)
        if not parser:
            # Fallback to regex-based parsing
            return await self._parse_with_regex(file_path, language)

        try:
            code = file_path.read_text(encoding="utf-8", errors="ignore")
            tree = parser.parse(bytes(code, "utf-8"))
            return await self._extract_tags_from_tree(tree, file_path, language)
        except Exception as e:
            logger.debug(f"Failed to parse {file_path}: {e}")
            return await self._parse_with_regex(file_path, language)

    async def _extract_tags_from_tree(
        self,
        tree: Any,
        file_path: Path,
        language: str,
    ) -> List[Tag]:
        """Extract tags from a tree-sitter parse tree."""
        tags = []
        rel_path = str(file_path)

        def visit(node):
            # Python definitions
            if language == "python":
                if node.type == "function_definition":
                    name_node = node.child_by_field_name("name")
                    if name_node:
                        tags.append(Tag(
                            rel_fname=rel_path,
                            fname=str(file_path),
                            line=node.start_point[0],
                            name=name_node.text.decode("utf-8"),
                            kind="def"
                        ))
                elif node.type == "class_definition":
                    name_node = node.child_by_field_name("name")
                    if name_node:
                        tags.append(Tag(
                            rel_fname=rel_path,
                            fname=str(file_path),
                            line=node.start_point[0],
                            name=name_node.text.decode("utf-8"),
                            kind="def"
                        ))
                elif node.type == "identifier" and node.parent and node.parent.type not in (
                    "function_definition", "class_definition", "import_statement"
                ):
                    tags.append(Tag(
                        rel_fname=rel_path,
                        fname=str(file_path),
                        line=node.start_point[0],
                        name=node.text.decode("utf-8"),
                        kind="ref"
                    ))

            # JavaScript/TypeScript definitions
            elif language in ("javascript", "typescript", "tsx"):
                if node.type in ("function_declaration", "method_definition"):
                    name_node = node.child_by_field_name("name")
                    if name_node:
                        tags.append(Tag(
                            rel_fname=rel_path,
                            fname=str(file_path),
                            line=node.start_point[0],
                            name=name_node.text.decode("utf-8"),
                            kind="def"
                        ))
                elif node.type == "class_declaration":
                    name_node = node.child_by_field_name("name")
                    if name_node:
                        tags.append(Tag(
                            rel_fname=rel_path,
                            fname=str(file_path),
                            line=node.start_point[0],
                            name=name_node.text.decode("utf-8"),
                            kind="def"
                        ))

            for child in node.children:
                visit(child)

        visit(tree.root_node)
        return tags

    async def _parse_with_regex(self, file_path: Path, language: str) -> List[Tag]:
        """Fallback regex-based parsing when tree-sitter is unavailable."""
        import re
        tags = []
        rel_path = str(file_path)

        try:
            code = file_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return []

        # Python patterns
        if language == "python":
            # Function definitions
            for match in re.finditer(r"^\s*(?:async\s+)?def\s+(\w+)", code, re.MULTILINE):
                line = code[:match.start()].count("\n")
                tags.append(Tag(rel_path, str(file_path), line, match.group(1), "def"))

            # Class definitions
            for match in re.finditer(r"^\s*class\s+(\w+)", code, re.MULTILINE):
                line = code[:match.start()].count("\n")
                tags.append(Tag(rel_path, str(file_path), line, match.group(1), "def"))

            # Imports (as references)
            for match in re.finditer(r"^\s*(?:from\s+\S+\s+)?import\s+(.+)$", code, re.MULTILINE):
                line = code[:match.start()].count("\n")
                imports = match.group(1)
                for name in re.findall(r"\b(\w+)\b", imports):
                    if name not in ("as", "import", "from"):
                        tags.append(Tag(rel_path, str(file_path), line, name, "ref"))

        # JavaScript/TypeScript patterns
        elif language in ("javascript", "typescript", "tsx"):
            # Function definitions
            for match in re.finditer(r"(?:function|const|let|var)\s+(\w+)\s*[=\(]", code):
                line = code[:match.start()].count("\n")
                tags.append(Tag(rel_path, str(file_path), line, match.group(1), "def"))

            # Class definitions
            for match in re.finditer(r"class\s+(\w+)", code):
                line = code[:match.start()].count("\n")
                tags.append(Tag(rel_path, str(file_path), line, match.group(1), "def"))

        return tags


# ============================================================================
# Repository Graph (NetworkX-based, like Aider's PageRank)
# ============================================================================

class RepositoryGraph:
    """Graph-based representation of code relationships."""

    def __init__(self, config: RepositoryIntelligenceConfig):
        self.config = config
        self._graph: Optional[Any] = None

        if NETWORKX_AVAILABLE:
            self._graph = nx.MultiDiGraph()

    def add_file(self, file_info: FileInfo) -> None:
        """Add a file node to the graph."""
        if self._graph is None:
            return

        self._graph.add_node(
            file_info.relative_path,
            type="file",
            repository=file_info.repository,
            size=file_info.size_bytes,
            lines=file_info.line_count,
            language=file_info.language,
        )

    def add_relationship(self, relationship: CodeRelationship) -> None:
        """Add a relationship edge to the graph."""
        if self._graph is None:
            return

        self._graph.add_edge(
            relationship.source,
            relationship.target,
            type=relationship.relationship_type.value,
            weight=relationship.weight,
            line=relationship.line_number,
        )

    def compute_pagerank(
        self,
        personalization: Optional[Dict[str, float]] = None,
    ) -> Dict[str, float]:
        """Compute PageRank scores for files."""
        if self._graph is None or not NETWORKX_AVAILABLE:
            return {}

        if len(self._graph.nodes) == 0:
            return {}

        try:
            if personalization:
                # Personalized PageRank (boost mentioned files)
                return nx.pagerank(
                    self._graph,
                    alpha=self.config.pagerank_damping,
                    personalization=personalization,
                    weight="weight",
                )
            else:
                return nx.pagerank(
                    self._graph,
                    alpha=self.config.pagerank_damping,
                    weight="weight",
                )
        except Exception as e:
            logger.warning(f"PageRank computation failed: {e}")
            return {}

    def get_file_dependencies(self, file_path: str) -> List[str]:
        """Get files that a given file depends on."""
        if self._graph is None:
            return []

        return list(self._graph.successors(file_path))

    def get_file_dependents(self, file_path: str) -> List[str]:
        """Get files that depend on a given file."""
        if self._graph is None:
            return []

        return list(self._graph.predecessors(file_path))

    def get_strongly_connected_components(self) -> List[Set[str]]:
        """Find strongly connected components (circular dependencies)."""
        if self._graph is None or not NETWORKX_AVAILABLE:
            return []

        return [set(c) for c in nx.strongly_connected_components(self._graph) if len(c) > 1]


# ============================================================================
# Repository Mapper (Main Interface)
# ============================================================================

class RepositoryMapper:
    """
    Main repository intelligence interface.

    Provides:
    - Compressed repository maps for LLM context
    - File importance ranking via PageRank
    - Cross-repository relationship analysis
    - Intelligent caching with invalidation
    """

    def __init__(self, config: Optional[RepositoryIntelligenceConfig] = None):
        self.config = config or RepositoryIntelligenceConfig()
        self.cache = IntelligenceCache(
            self.config.cache_dir,
            self.config.cache_ttl_hours,
        )
        self.parser = CodeParser()
        self._graphs: Dict[str, RepositoryGraph] = {}
        self._file_cache: Dict[str, FileInfo] = {}
        self._tag_cache: Dict[str, List[Tag]] = {}
        self._initialized = False
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Initialize the repository mapper."""
        async with self._lock:
            if self._initialized:
                return

            # Ensure cache directory exists
            self.config.cache_dir.mkdir(parents=True, exist_ok=True)

            # Initialize graphs for each repository
            for repo_name, repo_config in self.config.repositories.items():
                if repo_config.enabled and repo_config.path.exists():
                    self._graphs[repo_name] = RepositoryGraph(self.config)

            self._initialized = True
            logger.info(f"Repository Intelligence initialized with {len(self._graphs)} repositories")

    async def get_repo_map(
        self,
        repository: str,
        max_tokens: Optional[int] = None,
        mentioned_files: Optional[Set[str]] = None,
        mentioned_symbols: Optional[Set[str]] = None,
        force_refresh: bool = False,
    ) -> RepoMapResult:
        """
        Generate a compressed repository map for LLM context.

        Args:
            repository: Name of the repository to map
            max_tokens: Maximum tokens for the map (default from config)
            mentioned_files: Files to prioritize in the map
            mentioned_symbols: Symbols to prioritize
            force_refresh: Force cache refresh

        Returns:
            RepoMapResult with the map content and metadata
        """
        await self.initialize()

        start_time = time.time()
        max_tokens = max_tokens or self.config.max_map_tokens

        # Check cache
        cache_key = self.cache._make_key(
            repository, max_tokens,
            tuple(sorted(mentioned_files or [])),
            tuple(sorted(mentioned_symbols or [])),
        )

        if not force_refresh:
            cached = await self.cache.get(cache_key)
            if cached:
                return RepoMapResult(
                    map_content=cached["content"],
                    token_count=cached["tokens"],
                    files_included=cached["files"],
                    symbols_extracted=cached["symbols"],
                    generation_time_ms=0,
                    cache_hit=True,
                    repository=repository,
                )

        # Get repository config
        repo_config = self.config.repositories.get(repository)
        if not repo_config or not repo_config.enabled:
            return RepoMapResult(
                map_content="",
                token_count=0,
                files_included=0,
                symbols_extracted=0,
                generation_time_ms=0,
                cache_hit=False,
                repository=repository,
            )

        # Scan files
        files = await self._scan_repository(repo_config)

        # Parse files and extract tags
        all_tags = await self._parse_files(files, repo_config)

        # Build graph and compute rankings
        graph = self._graphs.get(repository, RepositoryGraph(self.config))
        personalization = {}

        # Boost mentioned files
        if mentioned_files:
            for f in mentioned_files:
                personalization[f] = 10.0

        # Boost files containing mentioned symbols
        if mentioned_symbols:
            for tag in all_tags:
                if tag.name in mentioned_symbols:
                    personalization[tag.rel_fname] = personalization.get(tag.rel_fname, 1.0) + 5.0

        # Compute PageRank
        rankings = graph.compute_pagerank(personalization if personalization else None)

        # Sort files by rank
        ranked_files = sorted(
            [(f, rankings.get(f.relative_path, 0.0)) for f in files],
            key=lambda x: -x[1]
        )

        # Generate map content
        map_content = await self._generate_map_content(ranked_files, all_tags, max_tokens)

        # Estimate token count (rough: 4 chars per token)
        token_count = len(map_content) // 4

        generation_time = (time.time() - start_time) * 1000

        result = RepoMapResult(
            map_content=map_content,
            token_count=token_count,
            files_included=len([f for f, _ in ranked_files if f.relative_path in map_content]),
            symbols_extracted=len(all_tags),
            generation_time_ms=generation_time,
            cache_hit=False,
            repository=repository,
        )

        # Cache result
        await self.cache.set(cache_key, {
            "content": map_content,
            "tokens": token_count,
            "files": result.files_included,
            "symbols": result.symbols_extracted,
        })

        return result

    async def get_cross_repo_map(
        self,
        repositories: Optional[List[str]] = None,
        max_tokens: Optional[int] = None,
        focus_area: Optional[str] = None,
    ) -> str:
        """
        Generate a map spanning multiple repositories.

        Args:
            repositories: List of repository names (default: all enabled)
            max_tokens: Maximum total tokens
            focus_area: Area to focus on (e.g., "voice_auth", "training")

        Returns:
            Combined repository map
        """
        await self.initialize()

        repos = repositories or [
            name for name, cfg in self.config.repositories.items()
            if cfg.enabled
        ]

        tokens_per_repo = (max_tokens or self.config.max_map_tokens) // len(repos)

        maps = []
        for repo in repos:
            result = await self.get_repo_map(
                repo,
                max_tokens=tokens_per_repo,
                mentioned_symbols={focus_area} if focus_area else None,
            )
            if result.map_content:
                maps.append(f"## Repository: {repo}\n\n{result.map_content}")

        return "\n\n".join(maps)

    async def analyze_cross_repo_dependencies(self) -> CrossRepoAnalysis:
        """
        Analyze dependencies across all repositories.

        Returns:
            CrossRepoAnalysis with shared symbols, dependencies, and recommendations
        """
        await self.initialize()
        start_time = time.time()

        # Collect all symbols from all repos
        repo_symbols: Dict[str, Set[str]] = defaultdict(set)
        symbol_locations: Dict[str, List[str]] = defaultdict(list)

        for repo_name, repo_config in self.config.repositories.items():
            if not repo_config.enabled or not repo_config.path.exists():
                continue

            files = await self._scan_repository(repo_config)
            tags = await self._parse_files(files, repo_config)

            for tag in tags:
                if tag.kind == "def":
                    repo_symbols[repo_name].add(tag.name)
                    symbol_locations[tag.name].append(repo_name)

        # Find shared symbols (defined in multiple repos)
        shared_symbols = [
            name for name, repos in symbol_locations.items()
            if len(set(repos)) > 1
        ]

        # Build dependency graph
        dependency_graph = {}
        for repo_name in repo_symbols:
            deps = []
            for other_repo, symbols in repo_symbols.items():
                if other_repo != repo_name:
                    # Check for references
                    shared = repo_symbols[repo_name] & symbols
                    if shared:
                        deps.append(other_repo)
            dependency_graph[repo_name] = deps

        # Identify integration points
        integration_points = []
        for symbol in shared_symbols[:10]:  # Top 10
            integration_points.append({
                "symbol": symbol,
                "repositories": list(set(symbol_locations[symbol])),
                "type": "shared_definition",
            })

        # Generate recommendations
        recommendations = []
        if shared_symbols:
            recommendations.append(
                f"Consider consolidating {len(shared_symbols)} shared symbols into a common module"
            )
        if any(len(deps) > 2 for deps in dependency_graph.values()):
            recommendations.append(
                "Complex cross-repo dependencies detected - consider using event bridge for loose coupling"
            )

        return CrossRepoAnalysis(
            repositories=list(repo_symbols.keys()),
            shared_symbols=shared_symbols,
            dependency_graph=dependency_graph,
            integration_points=integration_points,
            recommendations=recommendations,
            analysis_time_ms=(time.time() - start_time) * 1000,
        )

    async def find_symbol(
        self,
        symbol_name: str,
        repositories: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Find a symbol across repositories.

        Args:
            symbol_name: Name of the symbol to find
            repositories: Repositories to search (default: all)

        Returns:
            List of symbol locations with metadata
        """
        await self.initialize()

        repos = repositories or list(self.config.repositories.keys())
        results = []

        for repo_name in repos:
            repo_config = self.config.repositories.get(repo_name)
            if not repo_config or not repo_config.enabled:
                continue

            files = await self._scan_repository(repo_config)
            tags = await self._parse_files(files, repo_config)

            for tag in tags:
                if tag.name == symbol_name and tag.kind == "def":
                    results.append({
                        "repository": repo_name,
                        "file": tag.rel_fname,
                        "line": tag.line,
                        "kind": tag.kind,
                    })

        return results

    async def get_file_context(
        self,
        file_path: str,
        repository: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Get context for a specific file.

        Args:
            file_path: Path to the file
            repository: Repository name (auto-detected if not provided)

        Returns:
            File context including symbols, dependencies, dependents
        """
        await self.initialize()

        # Auto-detect repository
        if not repository:
            for repo_name, repo_config in self.config.repositories.items():
                if str(repo_config.path) in file_path:
                    repository = repo_name
                    break

        if not repository:
            return {"error": "Could not determine repository for file"}

        graph = self._graphs.get(repository)
        if not graph:
            return {"error": f"Repository {repository} not initialized"}

        return {
            "file": file_path,
            "repository": repository,
            "dependencies": graph.get_file_dependencies(file_path),
            "dependents": graph.get_file_dependents(file_path),
            "in_circular_dependency": any(
                file_path in component
                for component in graph.get_strongly_connected_components()
            ),
        }

    # ========================================================================
    # Private Methods
    # ========================================================================

    async def _scan_repository(self, repo_config: RepositoryConfig) -> List[FileInfo]:
        """Scan a repository for source files."""
        import fnmatch

        files = []
        root = repo_config.path

        for pattern in repo_config.include_patterns:
            for file_path in root.glob(pattern):
                if not file_path.is_file():
                    continue

                # Check exclusions
                rel_path = str(file_path.relative_to(root))
                if any(fnmatch.fnmatch(rel_path, exc) for exc in repo_config.exclude_patterns):
                    continue

                # Check size
                try:
                    stat = file_path.stat()
                    if stat.st_size > repo_config.max_file_size_kb * 1024:
                        continue

                    # Read line count
                    try:
                        line_count = sum(1 for _ in file_path.open(encoding="utf-8", errors="ignore"))
                    except Exception:
                        line_count = 0

                    language = self.parser.get_language(file_path) or "unknown"

                    files.append(FileInfo(
                        path=str(file_path),
                        relative_path=rel_path,
                        repository=repo_config.name,
                        size_bytes=stat.st_size,
                        mtime=stat.st_mtime,
                        line_count=line_count,
                        language=language,
                        symbols_count=0,
                        imports_count=0,
                    ))
                except Exception as e:
                    logger.debug(f"Failed to stat {file_path}: {e}")

        return files[:self.config.max_files_per_repo]

    async def _parse_files(
        self,
        files: List[FileInfo],
        repo_config: RepositoryConfig,
    ) -> List[Tag]:
        """Parse multiple files in parallel."""
        semaphore = asyncio.Semaphore(self.config.max_concurrent_parses)

        async def parse_one(file_info: FileInfo) -> List[Tag]:
            async with semaphore:
                return await self.parser.parse_file(Path(file_info.path))

        tasks = [parse_one(f) for f in files]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_tags = []
        for result in results:
            if isinstance(result, list):
                all_tags.extend(result)
            elif isinstance(result, Exception):
                logger.debug(f"Parse error: {result}")

        return all_tags

    async def _generate_map_content(
        self,
        ranked_files: List[Tuple[FileInfo, float]],
        tags: List[Tag],
        max_tokens: int,
    ) -> str:
        """Generate the map content string."""
        lines = []
        current_tokens = 0
        max_chars = max_tokens * 4  # Rough token-to-char ratio

        # Group tags by file
        tags_by_file: Dict[str, List[Tag]] = defaultdict(list)
        for tag in tags:
            tags_by_file[tag.rel_fname].append(tag)

        for file_info, rank in ranked_files:
            if current_tokens >= max_tokens:
                break

            file_tags = tags_by_file.get(file_info.relative_path, [])
            definitions = [t for t in file_tags if t.kind == "def"]

            if definitions:
                file_line = f"\n{file_info.relative_path}:"
                lines.append(file_line)
                current_tokens += len(file_line) // 4

                for tag in sorted(definitions, key=lambda t: t.line)[:20]:
                    tag_line = f"  {tag.line}: {tag.name}"
                    if current_tokens + len(tag_line) // 4 > max_tokens:
                        break
                    lines.append(tag_line)
                    current_tokens += len(tag_line) // 4
            else:
                # Just list the file
                file_line = f"\n{file_info.relative_path}"
                if current_tokens + len(file_line) // 4 <= max_tokens:
                    lines.append(file_line)
                    current_tokens += len(file_line) // 4

        return "\n".join(lines)


# ============================================================================
# LangGraph Reasoning Integration
# ============================================================================

# Dynamically create RepositoryReasoningState based on pydantic availability
if PYDANTIC_AVAILABLE and BaseModel is not None:
    class RepositoryReasoningState(BaseModel):
        """State for repository reasoning graph (Pydantic version)."""
        query: str = ""
        repositories: List[str] = PydanticField(default_factory=list)
        current_phase: str = "analyze"
        findings: List[Dict[str, Any]] = PydanticField(default_factory=list)
        recommendations: List[str] = PydanticField(default_factory=list)
        confidence: float = 0.0
        error: Optional[str] = None
else:
    # Fallback to dataclass when pydantic is not available
    @dataclass
    class RepositoryReasoningState:  # type: ignore[no-redef]
        """State for repository reasoning graph (dataclass fallback)."""
        query: str = ""
        repositories: List[str] = field(default_factory=list)
        current_phase: str = "analyze"
        findings: List[Dict[str, Any]] = field(default_factory=list)
        recommendations: List[str] = field(default_factory=list)
        confidence: float = 0.0
        error: Optional[str] = None


class RepositoryReasoningGraph:
    """
    LangGraph-based reasoning about repository structure.

    Uses chain-of-thought reasoning to:
    - Answer questions about code architecture
    - Suggest integration points
    - Identify potential issues
    """

    def __init__(self, mapper: RepositoryMapper):
        self.mapper = mapper
        self._graph: Optional[Any] = None

        if LANGGRAPH_AVAILABLE:
            self._build_graph()

    def _build_graph(self) -> None:
        """Build the LangGraph state machine."""
        if not LANGGRAPH_AVAILABLE:
            return

        builder = StateGraph(RepositoryReasoningState)

        # Add nodes
        builder.add_node("analyze_query", self._analyze_query)
        builder.add_node("gather_context", self._gather_context)
        builder.add_node("reason", self._reason)
        builder.add_node("synthesize", self._synthesize)

        # Add edges
        builder.set_entry_point("analyze_query")
        builder.add_edge("analyze_query", "gather_context")
        builder.add_edge("gather_context", "reason")
        builder.add_edge("reason", "synthesize")
        builder.add_edge("synthesize", END)

        self._graph = builder.compile()

    async def _analyze_query(self, state: RepositoryReasoningState) -> Dict[str, Any]:
        """Analyze what the query is asking for."""
        query_lower = state.query.lower()

        # Determine which repositories to search
        repos = []
        if "jarvis" in query_lower and "prime" not in query_lower:
            repos.append("jarvis")
        if "prime" in query_lower:
            repos.append("jarvis_prime")
        if "reactor" in query_lower or "training" in query_lower:
            repos.append("reactor_core")

        if not repos:
            repos = list(self.mapper.config.repositories.keys())

        return {"repositories": repos, "current_phase": "gather_context"}

    async def _gather_context(self, state: RepositoryReasoningState) -> Dict[str, Any]:
        """Gather relevant context from repositories."""
        findings = []

        for repo in state.repositories:
            try:
                result = await self.mapper.get_repo_map(repo, max_tokens=1000)
                findings.append({
                    "repository": repo,
                    "files": result.files_included,
                    "symbols": result.symbols_extracted,
                    "content_preview": result.map_content[:500] if result.map_content else "",
                })
            except Exception as e:
                findings.append({
                    "repository": repo,
                    "error": str(e),
                })

        return {"findings": findings, "current_phase": "reason"}

    async def _reason(self, state: RepositoryReasoningState) -> Dict[str, Any]:
        """Reason about the findings."""
        # Simple reasoning - can be enhanced with LLM calls
        recommendations = []

        total_files = sum(f.get("files", 0) for f in state.findings)
        total_symbols = sum(f.get("symbols", 0) for f in state.findings)

        if total_files > 100:
            recommendations.append(
                f"Large codebase ({total_files} files) - consider focusing queries on specific areas"
            )

        if len(state.repositories) > 1:
            recommendations.append(
                "Multi-repo query - check cross-repository dependencies"
            )

        confidence = 0.7 if state.findings else 0.3

        return {
            "recommendations": recommendations,
            "confidence": confidence,
            "current_phase": "synthesize",
        }

    async def _synthesize(self, state: RepositoryReasoningState) -> Dict[str, Any]:
        """Synthesize final answer."""
        return {"current_phase": "complete"}

    async def query(self, query: str) -> RepositoryReasoningState:
        """
        Query the repository with natural language.

        Args:
            query: Natural language question about the codebase

        Returns:
            RepositoryReasoningState with findings and recommendations
        """
        if not self._graph:
            return RepositoryReasoningState(
                query=query,
                error="LangGraph not available",
            )

        initial_state = RepositoryReasoningState(query=query)
        result = await self._graph.ainvoke(initial_state)
        return RepositoryReasoningState(**result)


# ============================================================================
# Singleton Instance and Convenience Functions
# ============================================================================

_mapper_instance: Optional[RepositoryMapper] = None
_reasoning_instance: Optional[RepositoryReasoningGraph] = None


async def get_repository_mapper() -> RepositoryMapper:
    """Get the singleton RepositoryMapper instance."""
    global _mapper_instance
    if _mapper_instance is None:
        _mapper_instance = RepositoryMapper()
        await _mapper_instance.initialize()
    return _mapper_instance


async def get_repository_reasoning() -> RepositoryReasoningGraph:
    """Get the singleton RepositoryReasoningGraph instance."""
    global _reasoning_instance
    if _reasoning_instance is None:
        mapper = await get_repository_mapper()
        _reasoning_instance = RepositoryReasoningGraph(mapper)
    return _reasoning_instance


async def get_repo_map(
    repository: str = "jarvis",
    max_tokens: int = 4096,
    mentioned_files: Optional[Set[str]] = None,
    mentioned_symbols: Optional[Set[str]] = None,
) -> str:
    """
    Convenience function to get a repository map.

    Example:
        map_content = await get_repo_map("jarvis", mentioned_symbols={"AgenticTaskRunner"})
    """
    mapper = await get_repository_mapper()
    result = await mapper.get_repo_map(
        repository,
        max_tokens=max_tokens,
        mentioned_files=mentioned_files,
        mentioned_symbols=mentioned_symbols,
    )
    return result.map_content


async def query_codebase(query: str) -> Dict[str, Any]:
    """
    Query the codebase with natural language.

    Example:
        result = await query_codebase("How does voice authentication work?")
    """
    reasoning = await get_repository_reasoning()
    state = await reasoning.query(query)
    return {
        "query": state.query,
        "repositories": state.repositories,
        "findings": state.findings,
        "recommendations": state.recommendations,
        "confidence": state.confidence,
        "error": state.error,
    }


# ============================================================================
# Module Exports
# ============================================================================

__all__ = [
    # Availability Flags (for graceful degradation)
    "TREE_SITTER_AVAILABLE",
    "NETWORKX_AVAILABLE",
    "LANGGRAPH_AVAILABLE",
    "DISKCACHE_AVAILABLE",
    "PYDANTIC_AVAILABLE",

    # Configuration
    "RepositoryConfig",
    "RepositoryIntelligenceConfig",

    # Enums
    "SymbolKind",
    "RelationshipType",
    "MapRefreshMode",

    # Data Classes
    "CodeSymbol",
    "CodeRelationship",
    "FileInfo",
    "Tag",
    "IntelligenceResult",
    "RepoMapResult",
    "CrossRepoAnalysis",

    # Main Classes
    "RepositoryMapper",
    "RepositoryGraph",
    "CodeParser",
    "IntelligenceCache",
    "RepositoryReasoningGraph",
    "RepositoryReasoningState",

    # Convenience Functions
    "get_repository_mapper",
    "get_repository_reasoning",
    "get_repo_map",
    "query_codebase",
]
