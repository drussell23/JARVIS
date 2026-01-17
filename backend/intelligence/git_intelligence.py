"""
Git Intelligence v1.0 - Version Control Awareness System
========================================================

Enterprise-grade git history analysis and pattern learning system.
Understands version control history to avoid repeating mistakes
and learn from successful patterns.

Features:
- Git history analysis for commit patterns
- Change frequency hotspot detection
- Code ownership and expertise mapping
- Refactoring pattern recognition
- Commit message analysis
- Branch relationship understanding
- Merge conflict prediction
- Cross-repository change correlation

Architecture:
    ┌─────────────────────────────────────────────────────────────────────────┐
    │                      Git Intelligence v1.0                               │
    ├─────────────────────────────────────────────────────────────────────────┤
    │                                                                          │
    │   ┌─────────────────┐   ┌─────────────────┐   ┌─────────────────┐       │
    │   │  Commit Analyzer│   │ Hotspot Detector│   │ Pattern Learner │       │
    │   │  (History)      │──▶│  (Frequency)    │──▶│ (ML-ready)      │       │
    │   └─────────────────┘   └─────────────────┘   └─────────────────┘       │
    │           │                     │                     │                  │
    │           └─────────────────────┴─────────────────────┘                  │
    │                                 │                                        │
    │                    ┌────────────▼────────────┐                           │
    │                    │   Knowledge Synthesizer  │                          │
    │                    │   (Insights + Warnings)  │                          │
    │                    └────────────┬────────────┘                           │
    │                                 │                                        │
    │   ┌──────────────┬──────────────┼──────────────┬──────────────┐         │
    │   │              │              │              │              │         │
    │   ▼              ▼              ▼              ▼              ▼         │
    │ Ownership    Refactoring    Merge Risk    Change         Cross-Repo    │
    │ Mapper       Detector       Predictor     Correlator     Analyzer      │
    │                                                                          │
    └─────────────────────────────────────────────────────────────────────────┘

Author: JARVIS AI System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
import time
from abc import ABC, abstractmethod
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from functools import lru_cache
from pathlib import Path
from typing import (
    Any, AsyncGenerator, Callable, Coroutine, DefaultDict, Dict,
    FrozenSet, Generator, Generic, Iterable, Iterator, List, Literal,
    Mapping, NamedTuple, Optional, Protocol, Sequence, Set, Tuple, Type,
    TypeVar, Union
)

logger = logging.getLogger(__name__)


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


class GitIntelligenceConfig:
    """Configuration for git intelligence."""

    # History analysis
    MAX_COMMITS_ANALYZE: int = _get_env_int("GIT_MAX_COMMITS", 1000)
    RECENT_COMMITS_WINDOW: int = _get_env_int("GIT_RECENT_WINDOW", 100)
    HOTSPOT_THRESHOLD: int = _get_env_int("GIT_HOTSPOT_THRESHOLD", 10)

    # Pattern detection
    PATTERN_MIN_OCCURRENCES: int = _get_env_int("GIT_PATTERN_MIN_OCCUR", 3)
    OWNERSHIP_THRESHOLD: float = _get_env_float("GIT_OWNERSHIP_THRESHOLD", 0.3)

    # Caching
    CACHE_DIR: Path = Path(_get_env("GIT_CACHE_DIR", str(Path.home() / ".jarvis/git_cache")))
    CACHE_TTL_SECONDS: int = _get_env_int("GIT_CACHE_TTL", 3600)

    # Cross-repo
    CROSS_REPO_ENABLED: bool = _get_env_bool("GIT_CROSS_REPO", True)

    # Repository paths
    JARVIS_REPO: Path = Path(_get_env("JARVIS_REPO", str(Path.home() / "Documents/repos/JARVIS-AI-Agent")))
    PRIME_REPO: Path = Path(_get_env("PRIME_REPO", str(Path.home() / "Documents/repos/jarvis-prime")))
    REACTOR_REPO: Path = Path(_get_env("REACTOR_REPO", str(Path.home() / "Documents/repos/reactor-core")))


# =============================================================================
# ENUMS AND TYPES
# =============================================================================

class CommitType(Enum):
    """Types of commits based on conventional commits."""
    FEATURE = "feat"
    FIX = "fix"
    REFACTOR = "refactor"
    DOCS = "docs"
    STYLE = "style"
    TEST = "test"
    CHORE = "chore"
    PERF = "perf"
    BUILD = "build"
    CI = "ci"
    REVERT = "revert"
    UNKNOWN = "unknown"


class ChangeType(Enum):
    """Types of file changes."""
    ADDED = "A"
    MODIFIED = "M"
    DELETED = "D"
    RENAMED = "R"
    COPIED = "C"
    TYPE_CHANGED = "T"


class RefactoringPattern(Enum):
    """Types of refactoring patterns detected."""
    EXTRACT_METHOD = "extract_method"
    INLINE_VARIABLE = "inline_variable"
    RENAME = "rename"
    MOVE_FILE = "move_file"
    SPLIT_FILE = "split_file"
    MERGE_FILES = "merge_files"
    ADD_PARAMETER = "add_parameter"
    REMOVE_PARAMETER = "remove_parameter"
    CHANGE_SIGNATURE = "change_signature"
    EXTRACT_CLASS = "extract_class"


class RiskLevel(Enum):
    """Risk level for changes."""
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class GitCommit:
    """Represents a git commit."""
    hash: str
    short_hash: str
    author_name: str
    author_email: str
    timestamp: datetime
    message: str
    files_changed: List["FileChange"]
    commit_type: CommitType
    scope: Optional[str] = None
    is_breaking: bool = False
    parent_hashes: List[str] = field(default_factory=list)

    @property
    def is_merge(self) -> bool:
        return len(self.parent_hashes) > 1

    @classmethod
    def parse_type_from_message(cls, message: str) -> Tuple[CommitType, Optional[str], bool]:
        """Parse commit type from conventional commit message."""
        # Match conventional commit format: type(scope)!: description
        pattern = r'^(\w+)(?:\(([^)]+)\))?(!)?\s*:\s*(.+)$'
        match = re.match(pattern, message.split('\n')[0])

        if match:
            type_str, scope, breaking, _ = match.groups()
            try:
                commit_type = CommitType(type_str.lower())
            except ValueError:
                commit_type = CommitType.UNKNOWN
            return commit_type, scope, breaking == '!'

        return CommitType.UNKNOWN, None, False


@dataclass
class FileChange:
    """Represents a file change in a commit."""
    file_path: Path
    change_type: ChangeType
    additions: int = 0
    deletions: int = 0
    old_path: Optional[Path] = None  # For renames

    @property
    def total_changes(self) -> int:
        return self.additions + self.deletions


@dataclass
class FileHotspot:
    """A frequently changed file (hotspot)."""
    file_path: Path
    change_count: int
    unique_authors: int
    recent_changes: int  # Changes in recent window
    risk_level: RiskLevel
    last_changed: datetime
    common_co_changes: List[Path]  # Files often changed together

    def __lt__(self, other: "FileHotspot") -> bool:
        return self.change_count < other.change_count


@dataclass
class CodeOwnership:
    """Code ownership information for a file."""
    file_path: Path
    primary_owner: str
    ownership_percentage: float
    contributors: Dict[str, int]  # author -> commit count
    last_contributor: str
    expertise_score: float  # 0-1 based on recency and frequency


@dataclass
class RefactoringEvent:
    """A detected refactoring event."""
    commit_hash: str
    pattern: RefactoringPattern
    affected_files: List[Path]
    description: str
    timestamp: datetime
    author: str
    confidence: float  # 0-1


@dataclass
class ChangeCorrelation:
    """Correlation between file changes."""
    file_a: Path
    file_b: Path
    co_change_count: int
    correlation_strength: float  # 0-1
    recent_co_changes: int


@dataclass
class CommitPattern:
    """A detected pattern in commits."""
    pattern_type: str
    occurrences: int
    examples: List[str]  # commit hashes
    files_involved: Set[Path]
    authors_involved: Set[str]
    description: str


@dataclass
class MergeRiskAssessment:
    """Risk assessment for potential merge."""
    target_branch: str
    source_branch: str
    conflicting_files: List[Path]
    risk_level: RiskLevel
    hotspots_touched: List[FileHotspot]
    suggestions: List[str]


@dataclass
class GitInsight:
    """An insight derived from git history."""
    insight_type: str
    title: str
    description: str
    severity: RiskLevel
    related_files: List[Path]
    related_commits: List[str]
    recommendation: Optional[str] = None


# =============================================================================
# GIT COMMAND EXECUTOR
# =============================================================================

class GitCommandExecutor:
    """
    Executes git commands asynchronously with caching.

    Provides a safe, async interface to git operations.
    """

    def __init__(self, repo_path: Path):
        self.repo_path = repo_path
        self._cache: Dict[str, Tuple[Any, float]] = {}

    async def execute(
        self,
        args: List[str],
        cache_key: Optional[str] = None,
        cache_ttl: int = GitIntelligenceConfig.CACHE_TTL_SECONDS,
    ) -> Tuple[bool, str, str]:
        """
        Execute a git command.

        Returns (success, stdout, stderr).
        """
        # Check cache
        if cache_key:
            cached = self._cache.get(cache_key)
            if cached and time.time() - cached[1] < cache_ttl:
                return cached[0]

        try:
            process = await asyncio.create_subprocess_exec(
                "git", *args,
                cwd=self.repo_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60.0)

            result = (
                process.returncode == 0,
                stdout.decode('utf-8', errors='replace'),
                stderr.decode('utf-8', errors='replace'),
            )

            # Cache result
            if cache_key:
                self._cache[cache_key] = (result, time.time())

            return result

        except asyncio.TimeoutError:
            return False, "", "Command timed out"
        except Exception as e:
            return False, "", str(e)

    async def get_commits(
        self,
        max_count: int = GitIntelligenceConfig.MAX_COMMITS_ANALYZE,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        file_path: Optional[Path] = None,
    ) -> List[GitCommit]:
        """Get commits with detailed information."""
        args = [
            "log",
            f"--max-count={max_count}",
            "--format=%H|%h|%an|%ae|%aI|%s|%P",
            "--name-status",
        ]

        if since:
            args.append(f"--since={since.isoformat()}")
        if until:
            args.append(f"--until={until.isoformat()}")
        if file_path:
            args.append("--")
            args.append(str(file_path))

        success, stdout, _ = await self.execute(args)
        if not success:
            return []

        return self._parse_commits(stdout)

    def _parse_commits(self, log_output: str) -> List[GitCommit]:
        """Parse git log output into GitCommit objects."""
        commits = []
        current_commit = None
        file_changes = []

        for line in log_output.strip().split('\n'):
            if not line:
                if current_commit:
                    current_commit.files_changed = file_changes
                    commits.append(current_commit)
                    current_commit = None
                    file_changes = []
                continue

            if '|' in line and line.count('|') >= 5:
                # Commit line
                if current_commit:
                    current_commit.files_changed = file_changes
                    commits.append(current_commit)
                    file_changes = []

                parts = line.split('|')
                if len(parts) >= 7:
                    hash_full, hash_short, author_name, author_email, timestamp_str, message, parents = parts[:7]

                    try:
                        timestamp = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                    except ValueError:
                        timestamp = datetime.now()

                    commit_type, scope, is_breaking = GitCommit.parse_type_from_message(message)

                    current_commit = GitCommit(
                        hash=hash_full,
                        short_hash=hash_short,
                        author_name=author_name,
                        author_email=author_email,
                        timestamp=timestamp,
                        message=message,
                        files_changed=[],
                        commit_type=commit_type,
                        scope=scope,
                        is_breaking=is_breaking,
                        parent_hashes=parents.split() if parents else [],
                    )

            elif line and current_commit:
                # File change line
                parts = line.split('\t')
                if len(parts) >= 2:
                    change_type_char = parts[0][0] if parts[0] else 'M'
                    try:
                        change_type = ChangeType(change_type_char)
                    except ValueError:
                        change_type = ChangeType.MODIFIED

                    file_path = Path(parts[-1])
                    old_path = Path(parts[1]) if len(parts) > 2 and change_type == ChangeType.RENAMED else None

                    file_changes.append(FileChange(
                        file_path=file_path,
                        change_type=change_type,
                        old_path=old_path,
                    ))

        # Don't forget the last commit
        if current_commit:
            current_commit.files_changed = file_changes
            commits.append(current_commit)

        return commits

    async def get_file_blame(self, file_path: Path) -> Dict[str, List[int]]:
        """Get blame information for a file (author -> lines)."""
        args = ["blame", "--line-porcelain", str(file_path)]
        success, stdout, _ = await self.execute(args)

        if not success:
            return {}

        author_lines: DefaultDict[str, List[int]] = defaultdict(list)
        current_author = None
        current_line = 0

        for line in stdout.split('\n'):
            if line.startswith('author '):
                current_author = line[7:]
            elif line.startswith('\t') and current_author:
                current_line += 1
                author_lines[current_author].append(current_line)

        return dict(author_lines)

    async def get_diff_stats(self, commit_hash: str) -> Dict[str, Tuple[int, int]]:
        """Get diff stats for a commit (file -> (additions, deletions))."""
        args = ["diff", "--numstat", f"{commit_hash}^", commit_hash]
        success, stdout, _ = await self.execute(args)

        if not success:
            return {}

        stats = {}
        for line in stdout.strip().split('\n'):
            if line:
                parts = line.split('\t')
                if len(parts) >= 3:
                    try:
                        additions = int(parts[0]) if parts[0] != '-' else 0
                        deletions = int(parts[1]) if parts[1] != '-' else 0
                        file_path = parts[2]
                        stats[file_path] = (additions, deletions)
                    except ValueError:
                        continue

        return stats

    async def get_current_branch(self) -> str:
        """Get current branch name."""
        success, stdout, _ = await self.execute(["rev-parse", "--abbrev-ref", "HEAD"])
        return stdout.strip() if success else "unknown"

    async def get_branches(self) -> List[str]:
        """Get all branch names."""
        success, stdout, _ = await self.execute(["branch", "-a", "--format=%(refname:short)"])
        return stdout.strip().split('\n') if success else []


# =============================================================================
# HOTSPOT DETECTOR
# =============================================================================

class HotspotDetector:
    """
    Detects frequently changed files (hotspots).

    Hotspots are files that change often and may indicate:
    - High complexity code
    - Technical debt
    - Bug-prone areas
    - Areas needing refactoring
    """

    def __init__(self, commits: List[GitCommit]):
        self._commits = commits
        self._file_changes: DefaultDict[Path, List[GitCommit]] = defaultdict(list)
        self._co_changes: DefaultDict[Tuple[Path, Path], int] = defaultdict(int)
        self._build_indexes()

    def _build_indexes(self) -> None:
        """Build change frequency indexes."""
        for commit in self._commits:
            files_in_commit = [fc.file_path for fc in commit.files_changed]

            for fc in commit.files_changed:
                self._file_changes[fc.file_path].append(commit)

            # Track co-changes
            for i, file_a in enumerate(files_in_commit):
                for file_b in files_in_commit[i + 1:]:
                    key = tuple(sorted([file_a, file_b], key=str))
                    self._co_changes[key] += 1

    def detect_hotspots(
        self,
        threshold: int = GitIntelligenceConfig.HOTSPOT_THRESHOLD,
        recent_window: int = GitIntelligenceConfig.RECENT_COMMITS_WINDOW,
    ) -> List[FileHotspot]:
        """Detect file hotspots based on change frequency."""
        hotspots = []
        now = datetime.now()
        recent_cutoff = now - timedelta(days=30)

        for file_path, commits in self._file_changes.items():
            if len(commits) < threshold:
                continue

            # Count unique authors
            authors = set(c.author_email for c in commits)

            # Count recent changes
            recent_changes = sum(1 for c in commits if c.timestamp > recent_cutoff)

            # Determine risk level
            risk_level = self._calculate_risk_level(
                total_changes=len(commits),
                recent_changes=recent_changes,
                unique_authors=len(authors),
            )

            # Find common co-changes
            co_changes = self._get_common_co_changes(file_path, limit=5)

            hotspots.append(FileHotspot(
                file_path=file_path,
                change_count=len(commits),
                unique_authors=len(authors),
                recent_changes=recent_changes,
                risk_level=risk_level,
                last_changed=max(c.timestamp for c in commits),
                common_co_changes=co_changes,
            ))

        # Sort by change count
        hotspots.sort(reverse=True)
        return hotspots

    def _calculate_risk_level(
        self,
        total_changes: int,
        recent_changes: int,
        unique_authors: int,
    ) -> RiskLevel:
        """Calculate risk level for a file."""
        # High change velocity + many authors = higher risk
        velocity_score = recent_changes / max(1, total_changes / 10)
        author_score = 1 if unique_authors > 3 else 0.5

        combined_score = (velocity_score + author_score) / 2

        if combined_score > 0.8 and total_changes > 50:
            return RiskLevel.CRITICAL
        elif combined_score > 0.6 and total_changes > 30:
            return RiskLevel.HIGH
        elif combined_score > 0.4 or total_changes > 20:
            return RiskLevel.MEDIUM
        return RiskLevel.LOW

    def _get_common_co_changes(self, file_path: Path, limit: int = 5) -> List[Path]:
        """Get files that commonly change together with the given file."""
        co_change_counts = []

        for (file_a, file_b), count in self._co_changes.items():
            if file_a == file_path:
                co_change_counts.append((file_b, count))
            elif file_b == file_path:
                co_change_counts.append((file_a, count))

        co_change_counts.sort(key=lambda x: x[1], reverse=True)
        return [f for f, _ in co_change_counts[:limit]]

    def get_change_correlations(
        self,
        min_correlation: float = 0.3,
    ) -> List[ChangeCorrelation]:
        """Get file change correlations."""
        correlations = []

        for (file_a, file_b), count in self._co_changes.items():
            # Calculate correlation strength
            total_a = len(self._file_changes[file_a])
            total_b = len(self._file_changes[file_b])

            if total_a == 0 or total_b == 0:
                continue

            # Jaccard-like correlation
            union = total_a + total_b - count
            correlation = count / union if union > 0 else 0

            if correlation >= min_correlation:
                # Count recent co-changes
                recent_cutoff = datetime.now() - timedelta(days=30)
                recent = sum(
                    1 for c in self._commits
                    if c.timestamp > recent_cutoff and
                    any(fc.file_path == file_a for fc in c.files_changed) and
                    any(fc.file_path == file_b for fc in c.files_changed)
                )

                correlations.append(ChangeCorrelation(
                    file_a=file_a,
                    file_b=file_b,
                    co_change_count=count,
                    correlation_strength=correlation,
                    recent_co_changes=recent,
                ))

        correlations.sort(key=lambda x: x.correlation_strength, reverse=True)
        return correlations


# =============================================================================
# OWNERSHIP ANALYZER
# =============================================================================

class OwnershipAnalyzer:
    """
    Analyzes code ownership and expertise.

    Determines who knows the code best based on:
    - Commit frequency
    - Recency of commits
    - Lines of code contributed
    """

    def __init__(self, commits: List[GitCommit], git_executor: GitCommandExecutor):
        self._commits = commits
        self._git = git_executor
        self._file_authors: DefaultDict[Path, Counter] = defaultdict(Counter)
        self._build_indexes()

    def _build_indexes(self) -> None:
        """Build ownership indexes."""
        for commit in self._commits:
            for fc in commit.files_changed:
                self._file_authors[fc.file_path][commit.author_email] += 1

    async def get_ownership(self, file_path: Path) -> Optional[CodeOwnership]:
        """Get code ownership for a file."""
        if file_path not in self._file_authors:
            return None

        contributors = dict(self._file_authors[file_path])
        total_commits = sum(contributors.values())

        if total_commits == 0:
            return None

        # Find primary owner
        primary_owner = max(contributors, key=contributors.get)
        ownership_percentage = contributors[primary_owner] / total_commits

        # Find last contributor
        last_commit = None
        for commit in self._commits:
            if any(fc.file_path == file_path for fc in commit.files_changed):
                last_commit = commit
                break

        last_contributor = last_commit.author_email if last_commit else primary_owner

        # Calculate expertise score (weighted by recency)
        expertise_score = self._calculate_expertise_score(file_path, primary_owner)

        return CodeOwnership(
            file_path=file_path,
            primary_owner=primary_owner,
            ownership_percentage=ownership_percentage,
            contributors=contributors,
            last_contributor=last_contributor,
            expertise_score=expertise_score,
        )

    def _calculate_expertise_score(self, file_path: Path, author: str) -> float:
        """Calculate expertise score for an author on a file."""
        relevant_commits = [
            c for c in self._commits
            if c.author_email == author and
            any(fc.file_path == file_path for fc in c.files_changed)
        ]

        if not relevant_commits:
            return 0.0

        # Weight by recency (more recent = higher weight)
        now = datetime.now()
        total_weight = 0.0
        max_weight = 0.0

        for commit in relevant_commits:
            days_ago = (now - commit.timestamp).days
            weight = 1.0 / (1 + days_ago / 30)  # Decay over 30 days
            total_weight += weight
            max_weight += 1.0

        return total_weight / max_weight if max_weight > 0 else 0.0

    def get_team_expertise(self) -> Dict[str, Dict[str, float]]:
        """Get expertise mapping for all team members."""
        expertise: DefaultDict[str, Dict[str, float]] = defaultdict(dict)

        all_files = set(self._file_authors.keys())
        all_authors = set()
        for authors in self._file_authors.values():
            all_authors.update(authors.keys())

        for author in all_authors:
            for file_path in all_files:
                score = self._calculate_expertise_score(file_path, author)
                if score > 0.1:  # Only track meaningful expertise
                    expertise[author][str(file_path)] = score

        return dict(expertise)


# =============================================================================
# REFACTORING DETECTOR
# =============================================================================

class RefactoringDetector:
    """
    Detects refactoring patterns in git history.

    Identifies common refactoring operations:
    - Extract method/class
    - Rename operations
    - File moves/splits
    - Signature changes
    """

    def __init__(self, commits: List[GitCommit], git_executor: GitCommandExecutor):
        self._commits = commits
        self._git = git_executor

    async def detect_refactorings(self) -> List[RefactoringEvent]:
        """Detect refactoring events in commit history."""
        refactorings = []

        for commit in self._commits:
            # Check for rename in commit message
            if commit.commit_type == CommitType.REFACTOR or 'refactor' in commit.message.lower():
                events = await self._analyze_refactoring_commit(commit)
                refactorings.extend(events)

            # Check for file renames
            for fc in commit.files_changed:
                if fc.change_type == ChangeType.RENAMED and fc.old_path:
                    refactorings.append(RefactoringEvent(
                        commit_hash=commit.hash,
                        pattern=RefactoringPattern.RENAME,
                        affected_files=[fc.old_path, fc.file_path],
                        description=f"Renamed {fc.old_path} to {fc.file_path}",
                        timestamp=commit.timestamp,
                        author=commit.author_email,
                        confidence=0.95,
                    ))

            # Check for file splits (new file + modified file with similar name)
            await self._detect_file_splits(commit, refactorings)

        return refactorings

    async def _analyze_refactoring_commit(self, commit: GitCommit) -> List[RefactoringEvent]:
        """Analyze a commit marked as refactoring."""
        events = []

        # Get detailed diff for the commit
        diff_stats = await self._git.get_diff_stats(commit.hash)

        # Detect extract patterns (new file created from existing)
        new_files = [fc for fc in commit.files_changed if fc.change_type == ChangeType.ADDED]
        modified_files = [fc for fc in commit.files_changed if fc.change_type == ChangeType.MODIFIED]

        # If we have both new and modified files in same directory, might be extract
        for new_file in new_files:
            for mod_file in modified_files:
                if new_file.file_path.parent == mod_file.file_path.parent:
                    events.append(RefactoringEvent(
                        commit_hash=commit.hash,
                        pattern=RefactoringPattern.EXTRACT_METHOD,
                        affected_files=[mod_file.file_path, new_file.file_path],
                        description=f"Possible extraction from {mod_file.file_path} to {new_file.file_path}",
                        timestamp=commit.timestamp,
                        author=commit.author_email,
                        confidence=0.6,
                    ))

        return events

    async def _detect_file_splits(
        self,
        commit: GitCommit,
        refactorings: List[RefactoringEvent],
    ) -> None:
        """Detect file split patterns."""
        new_files = [fc.file_path for fc in commit.files_changed if fc.change_type == ChangeType.ADDED]
        deleted_files = [fc.file_path for fc in commit.files_changed if fc.change_type == ChangeType.DELETED]

        # Check if a file was deleted and multiple new files created in same dir
        for deleted in deleted_files:
            new_in_same_dir = [f for f in new_files if f.parent == deleted.parent]
            if len(new_in_same_dir) >= 2:
                refactorings.append(RefactoringEvent(
                    commit_hash=commit.hash,
                    pattern=RefactoringPattern.SPLIT_FILE,
                    affected_files=[deleted] + new_in_same_dir,
                    description=f"File {deleted} split into {len(new_in_same_dir)} files",
                    timestamp=commit.timestamp,
                    author=commit.author_email,
                    confidence=0.7,
                ))

    def find_similar_refactorings(
        self,
        pattern: RefactoringPattern,
        limit: int = 5,
    ) -> List[RefactoringEvent]:
        """Find similar refactoring events in history."""
        # This would be called after detect_refactorings
        # For now, return empty - would need stored refactorings
        return []


# =============================================================================
# PATTERN LEARNER
# =============================================================================

class PatternLearner:
    """
    Learns patterns from git history.

    Identifies:
    - Common change patterns
    - Bug fix patterns
    - Feature patterns
    - Anti-patterns to avoid
    """

    def __init__(self, commits: List[GitCommit]):
        self._commits = commits

    def learn_patterns(self) -> List[CommitPattern]:
        """Learn common patterns from commit history."""
        patterns = []

        # Learn commit message patterns
        patterns.extend(self._learn_message_patterns())

        # Learn file change patterns
        patterns.extend(self._learn_file_patterns())

        # Learn time-based patterns
        patterns.extend(self._learn_temporal_patterns())

        return patterns

    def _learn_message_patterns(self) -> List[CommitPattern]:
        """Learn patterns from commit messages."""
        patterns = []

        # Group by commit type
        type_groups: DefaultDict[CommitType, List[GitCommit]] = defaultdict(list)
        for commit in self._commits:
            type_groups[commit.commit_type].append(commit)

        for commit_type, commits in type_groups.items():
            if len(commits) >= GitIntelligenceConfig.PATTERN_MIN_OCCURRENCES:
                files = set()
                authors = set()
                for c in commits:
                    files.update(fc.file_path for fc in c.files_changed)
                    authors.add(c.author_email)

                patterns.append(CommitPattern(
                    pattern_type=f"commit_type_{commit_type.value}",
                    occurrences=len(commits),
                    examples=[c.hash for c in commits[:5]],
                    files_involved=files,
                    authors_involved=authors,
                    description=f"Found {len(commits)} {commit_type.value} commits",
                ))

        return patterns

    def _learn_file_patterns(self) -> List[CommitPattern]:
        """Learn patterns from file changes."""
        patterns = []

        # Find files that are often changed together
        file_pairs: Counter = Counter()

        for commit in self._commits:
            files = [fc.file_path for fc in commit.files_changed]
            for i, file_a in enumerate(files):
                for file_b in files[i + 1:]:
                    key = tuple(sorted([str(file_a), str(file_b)]))
                    file_pairs[key] += 1

        # Report frequent pairs
        for (file_a, file_b), count in file_pairs.most_common(10):
            if count >= GitIntelligenceConfig.PATTERN_MIN_OCCURRENCES:
                patterns.append(CommitPattern(
                    pattern_type="co_change_pattern",
                    occurrences=count,
                    examples=[],
                    files_involved={Path(file_a), Path(file_b)},
                    authors_involved=set(),
                    description=f"{file_a} and {file_b} change together {count} times",
                ))

        return patterns

    def _learn_temporal_patterns(self) -> List[CommitPattern]:
        """Learn time-based patterns."""
        patterns = []

        # Group commits by day of week
        day_groups: DefaultDict[int, List[GitCommit]] = defaultdict(list)
        for commit in self._commits:
            day_groups[commit.timestamp.weekday()].append(commit)

        # Find most active days
        most_active_day = max(day_groups, key=lambda d: len(day_groups[d]))
        day_names = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']

        patterns.append(CommitPattern(
            pattern_type="temporal_pattern",
            occurrences=len(day_groups[most_active_day]),
            examples=[],
            files_involved=set(),
            authors_involved=set(),
            description=f"Most active day: {day_names[most_active_day]} with {len(day_groups[most_active_day])} commits",
        ))

        return patterns

    def get_anti_patterns(self) -> List[GitInsight]:
        """Identify anti-patterns to avoid."""
        insights = []

        # Check for large commits
        large_commits = [c for c in self._commits if len(c.files_changed) > 20]
        if large_commits:
            insights.append(GitInsight(
                insight_type="anti_pattern",
                title="Large commits detected",
                description=f"Found {len(large_commits)} commits with 20+ files changed",
                severity=RiskLevel.MEDIUM,
                related_files=[],
                related_commits=[c.hash for c in large_commits[:5]],
                recommendation="Consider breaking large changes into smaller, focused commits",
            ))

        # Check for commits without conventional format
        non_conventional = [c for c in self._commits if c.commit_type == CommitType.UNKNOWN]
        if len(non_conventional) > len(self._commits) * 0.5:
            insights.append(GitInsight(
                insight_type="anti_pattern",
                title="Non-conventional commit messages",
                description=f"{len(non_conventional)} commits don't follow conventional commit format",
                severity=RiskLevel.LOW,
                related_files=[],
                related_commits=[c.hash for c in non_conventional[:5]],
                recommendation="Use conventional commit format: type(scope): description",
            ))

        return insights


# =============================================================================
# GIT INTELLIGENCE ENGINE
# =============================================================================

class GitIntelligenceEngine:
    """
    Main git intelligence engine.

    Coordinates all git analysis components to provide
    comprehensive version control awareness.
    """

    def __init__(self, repo_path: Path):
        self.repo_path = repo_path
        self._git = GitCommandExecutor(repo_path)
        self._commits: List[GitCommit] = []
        self._hotspot_detector: Optional[HotspotDetector] = None
        self._ownership_analyzer: Optional[OwnershipAnalyzer] = None
        self._refactoring_detector: Optional[RefactoringDetector] = None
        self._pattern_learner: Optional[PatternLearner] = None
        self._initialized = False
        self._lock = asyncio.Lock()

    async def initialize(self) -> bool:
        """Initialize the git intelligence engine."""
        async with self._lock:
            if self._initialized:
                return True

            logger.info(f"Initializing Git Intelligence for {self.repo_path}...")

            # Load commits
            self._commits = await self._git.get_commits(
                max_count=GitIntelligenceConfig.MAX_COMMITS_ANALYZE
            )

            if not self._commits:
                logger.warning(f"No commits found in {self.repo_path}")
                return False

            logger.info(f"  Loaded {len(self._commits)} commits")

            # Initialize analyzers
            self._hotspot_detector = HotspotDetector(self._commits)
            self._ownership_analyzer = OwnershipAnalyzer(self._commits, self._git)
            self._refactoring_detector = RefactoringDetector(self._commits, self._git)
            self._pattern_learner = PatternLearner(self._commits)

            self._initialized = True
            return True

    async def get_hotspots(self, threshold: int = None) -> List[FileHotspot]:
        """Get file hotspots."""
        if not self._initialized:
            await self.initialize()

        threshold = threshold or GitIntelligenceConfig.HOTSPOT_THRESHOLD
        return self._hotspot_detector.detect_hotspots(threshold)

    async def get_ownership(self, file_path: Path) -> Optional[CodeOwnership]:
        """Get code ownership for a file."""
        if not self._initialized:
            await self.initialize()

        return await self._ownership_analyzer.get_ownership(file_path)

    async def get_refactorings(self) -> List[RefactoringEvent]:
        """Get detected refactoring events."""
        if not self._initialized:
            await self.initialize()

        return await self._refactoring_detector.detect_refactorings()

    async def get_patterns(self) -> List[CommitPattern]:
        """Get learned patterns."""
        if not self._initialized:
            await self.initialize()

        return self._pattern_learner.learn_patterns()

    async def get_insights(self) -> List[GitInsight]:
        """Get all git insights."""
        if not self._initialized:
            await self.initialize()

        insights = []

        # Get anti-patterns
        insights.extend(self._pattern_learner.get_anti_patterns())

        # Get hotspot insights
        hotspots = await self.get_hotspots()
        critical_hotspots = [h for h in hotspots if h.risk_level in (RiskLevel.CRITICAL, RiskLevel.HIGH)]

        if critical_hotspots:
            insights.append(GitInsight(
                insight_type="hotspot_warning",
                title=f"{len(critical_hotspots)} high-risk hotspots detected",
                description="These files change frequently and may need attention",
                severity=RiskLevel.HIGH,
                related_files=[h.file_path for h in critical_hotspots[:5]],
                related_commits=[],
                recommendation="Consider refactoring or adding tests to stabilize these files",
            ))

        return insights

    async def get_recent_changes(
        self,
        file_path: Optional[Path] = None,
        days: int = 7,
    ) -> List[GitCommit]:
        """Get recent changes, optionally filtered by file."""
        since = datetime.now() - timedelta(days=days)
        return await self._git.get_commits(
            max_count=GitIntelligenceConfig.RECENT_COMMITS_WINDOW,
            since=since,
            file_path=file_path,
        )

    async def assess_change_risk(self, file_path: Path) -> RiskLevel:
        """Assess risk level for changing a file."""
        if not self._initialized:
            await self.initialize()

        # Check if it's a hotspot
        hotspots = await self.get_hotspots()
        for hotspot in hotspots:
            if hotspot.file_path == file_path:
                return hotspot.risk_level

        # Default to low risk
        return RiskLevel.LOW

    def get_stats(self) -> Dict[str, Any]:
        """Get engine statistics."""
        return {
            "initialized": self._initialized,
            "repo_path": str(self.repo_path),
            "commits_loaded": len(self._commits),
            "date_range": {
                "oldest": self._commits[-1].timestamp.isoformat() if self._commits else None,
                "newest": self._commits[0].timestamp.isoformat() if self._commits else None,
            },
        }


# =============================================================================
# CROSS-REPO GIT INTELLIGENCE
# =============================================================================

class CrossRepoGitIntelligence:
    """
    Git intelligence across multiple repositories.

    Correlates changes across JARVIS, JARVIS-Prime, and Reactor-Core.
    """

    def __init__(self):
        self._repos: Dict[str, Path] = {
            "jarvis": GitIntelligenceConfig.JARVIS_REPO,
            "prime": GitIntelligenceConfig.PRIME_REPO,
            "reactor": GitIntelligenceConfig.REACTOR_REPO,
        }
        self._engines: Dict[str, GitIntelligenceEngine] = {}
        self._lock = asyncio.Lock()

    async def initialize(self) -> bool:
        """Initialize git intelligence for all repositories."""
        logger.info("Initializing Cross-Repo Git Intelligence...")

        for repo_name, repo_path in self._repos.items():
            if not repo_path.exists():
                logger.warning(f"Repository not found: {repo_name} at {repo_path}")
                continue

            engine = GitIntelligenceEngine(repo_path)
            success = await engine.initialize()

            if success:
                self._engines[repo_name] = engine
                logger.info(f"  ✓ {repo_name}: initialized")
            else:
                logger.warning(f"  ✗ {repo_name}: failed to initialize")

        return len(self._engines) > 0

    async def get_all_hotspots(self) -> Dict[str, List[FileHotspot]]:
        """Get hotspots from all repositories."""
        results = {}
        for repo_name, engine in self._engines.items():
            results[repo_name] = await engine.get_hotspots()
        return results

    async def get_all_insights(self) -> Dict[str, List[GitInsight]]:
        """Get insights from all repositories."""
        results = {}
        for repo_name, engine in self._engines.items():
            results[repo_name] = await engine.get_insights()
        return results

    async def correlate_changes(
        self,
        time_window: timedelta = timedelta(hours=24),
    ) -> List[Dict[str, Any]]:
        """Find correlated changes across repositories."""
        correlations = []

        # Get recent commits from all repos
        repo_commits: Dict[str, List[GitCommit]] = {}
        for repo_name, engine in self._engines.items():
            repo_commits[repo_name] = await engine.get_recent_changes(days=7)

        # Find commits that happened around the same time
        for repo_a, commits_a in repo_commits.items():
            for repo_b, commits_b in repo_commits.items():
                if repo_a >= repo_b:
                    continue

                for commit_a in commits_a:
                    for commit_b in commits_b:
                        time_diff = abs((commit_a.timestamp - commit_b.timestamp).total_seconds())
                        if time_diff < time_window.total_seconds():
                            correlations.append({
                                "repo_a": repo_a,
                                "repo_b": repo_b,
                                "commit_a": commit_a.hash,
                                "commit_b": commit_b.hash,
                                "time_diff_seconds": time_diff,
                                "messages": [commit_a.message, commit_b.message],
                            })

        return correlations

    def get_stats(self) -> Dict[str, Any]:
        """Get cross-repo statistics."""
        stats = {"repositories": {}}
        for repo_name, engine in self._engines.items():
            stats["repositories"][repo_name] = engine.get_stats()
        return stats


# =============================================================================
# SINGLETON ACCESSORS
# =============================================================================

_git_intelligence: Optional[GitIntelligenceEngine] = None
_cross_repo_git: Optional[CrossRepoGitIntelligence] = None


def get_git_intelligence(repo_path: Optional[Path] = None) -> GitIntelligenceEngine:
    """Get the singleton git intelligence engine."""
    global _git_intelligence
    repo_path = repo_path or GitIntelligenceConfig.JARVIS_REPO
    if _git_intelligence is None or _git_intelligence.repo_path != repo_path:
        _git_intelligence = GitIntelligenceEngine(repo_path)
    return _git_intelligence


def get_cross_repo_git_intelligence() -> CrossRepoGitIntelligence:
    """Get the singleton cross-repo git intelligence."""
    global _cross_repo_git
    if _cross_repo_git is None:
        _cross_repo_git = CrossRepoGitIntelligence()
    return _cross_repo_git


async def initialize_git_intelligence() -> bool:
    """Initialize the cross-repo git intelligence."""
    git_intel = get_cross_repo_git_intelligence()
    return await git_intel.initialize()
