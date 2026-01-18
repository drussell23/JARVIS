"""
Code Ownership System - Permission-Aware Code Management
==========================================================

Production-grade code ownership system with:
- CODEOWNERS file parsing and enforcement
- Git blame-based ownership inference
- Permission levels and access control
- Team-based ownership hierarchies
- Ownership transfer and delegation

Architecture:
    ┌─────────────────────────────────────────────────────────────────────────┐
    │                    Code Ownership System v1.0                           │
    ├─────────────────────────────────────────────────────────────────────────┤
    │                                                                         │
    │   ┌─────────────┐     ┌─────────────┐     ┌─────────────┐               │
    │   │ CODEOWNERS  │     │  Git Blame  │     │ Permission  │               │
    │   │   Parser    │────▶│  Analyzer   │────▶│   Engine    │               │
    │   └─────────────┘     └─────────────┘     └─────────────┘               │
    │          │                   │                   │                      │
    │          └───────────────────┴───────────────────┘                      │
    │                              │                                          │
    │                    ┌─────────▼─────────┐                                │
    │                    │  Ownership Graph  │                                │
    │                    │                   │                                │
    │                    │ • File → Owners   │                                │
    │                    │ • Team hierarchy  │                                │
    │                    │ • Permission map  │                                │
    │                    └───────────────────┘                                │
    │                                                                         │
    └─────────────────────────────────────────────────────────────────────────┘

Author: JARVIS Intelligence System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    FrozenSet,
    List,
    Optional,
    Pattern,
    Set,
    Tuple,
    Union,
)

logger = logging.getLogger("JARVIS.CodeOwnership")


# =============================================================================
# CONFIGURATION
# =============================================================================

@dataclass(frozen=True)
class OwnershipConfig:
    """Environment-driven ownership configuration."""

    # Ownership sources
    codeowners_paths: Tuple[str, ...] = tuple(
        os.getenv("OWNERSHIP_CODEOWNERS_PATHS", "CODEOWNERS,.github/CODEOWNERS,docs/CODEOWNERS").split(",")
    )
    use_git_blame: bool = os.getenv("OWNERSHIP_USE_GIT_BLAME", "true").lower() == "true"
    blame_threshold: float = float(os.getenv("OWNERSHIP_BLAME_THRESHOLD", "0.3"))  # Min 30% of lines

    # Permission settings
    default_permission: str = os.getenv("OWNERSHIP_DEFAULT_PERMISSION", "read")
    strict_mode: bool = os.getenv("OWNERSHIP_STRICT_MODE", "false").lower() == "true"

    # Caching
    cache_ttl: int = int(os.getenv("OWNERSHIP_CACHE_TTL", "3600"))
    enable_cache: bool = os.getenv("OWNERSHIP_ENABLE_CACHE", "true").lower() == "true"

    # Team configuration
    teams_file: Optional[Path] = Path(os.getenv("OWNERSHIP_TEAMS_FILE", "")) if os.getenv("OWNERSHIP_TEAMS_FILE") else None

    # Cross-repo
    enable_cross_repo: bool = os.getenv("OWNERSHIP_CROSS_REPO_ENABLED", "true").lower() == "true"

    @classmethod
    def from_env(cls) -> "OwnershipConfig":
        """Create configuration from environment."""
        return cls()


# =============================================================================
# ENUMS
# =============================================================================

class PermissionLevel(Enum):
    """Permission levels for code access."""
    NONE = 0
    READ = 1
    SUGGEST = 2  # Can suggest changes but not approve
    WRITE = 3
    APPROVE = 4  # Can approve PRs
    ADMIN = 5    # Full control including ownership transfer

    def __ge__(self, other: "PermissionLevel") -> bool:
        return self.value >= other.value

    def __gt__(self, other: "PermissionLevel") -> bool:
        return self.value > other.value

    def __le__(self, other: "PermissionLevel") -> bool:
        return self.value <= other.value

    def __lt__(self, other: "PermissionLevel") -> bool:
        return self.value < other.value


class OwnerType(Enum):
    """Type of owner."""
    USER = "user"
    TEAM = "team"
    GROUP = "group"  # LDAP/AD group
    BOT = "bot"


class OwnershipSource(Enum):
    """Source of ownership information."""
    CODEOWNERS = "codeowners"
    GIT_BLAME = "git_blame"
    EXPLICIT = "explicit"
    INHERITED = "inherited"
    DEFAULT = "default"


class AccessDecision(Enum):
    """Result of an access check."""
    ALLOWED = "allowed"
    DENIED = "denied"
    REQUIRES_APPROVAL = "requires_approval"
    REQUIRES_REVIEW = "requires_review"


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass(frozen=True)
class Owner:
    """Represents a code owner."""
    id: str
    owner_type: OwnerType
    email: Optional[str] = None
    name: Optional[str] = None
    metadata: FrozenSet[Tuple[str, Any]] = frozenset()

    def __hash__(self) -> int:
        return hash((self.id, self.owner_type))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Owner):
            return False
        return self.id == other.id and self.owner_type == other.owner_type

    @classmethod
    def user(cls, username: str, email: Optional[str] = None) -> "Owner":
        """Create a user owner."""
        return cls(id=username, owner_type=OwnerType.USER, email=email)

    @classmethod
    def team(cls, team_name: str) -> "Owner":
        """Create a team owner."""
        return cls(id=team_name, owner_type=OwnerType.TEAM)


@dataclass
class OwnershipRule:
    """A single ownership rule from CODEOWNERS."""
    pattern: str
    owners: List[Owner]
    line_number: int
    source_file: Path
    is_negation: bool = False
    comment: Optional[str] = None

    _compiled_pattern: Optional[Pattern] = field(default=None, repr=False)

    def __post_init__(self):
        """Compile the pattern for matching."""
        self._compile_pattern()

    def _compile_pattern(self) -> None:
        """Compile glob pattern to regex."""
        # Handle negation
        pattern = self.pattern
        if pattern.startswith("!"):
            pattern = pattern[1:]
            self.is_negation = True

        # Convert glob to regex
        regex_pattern = self._glob_to_regex(pattern)
        self._compiled_pattern = re.compile(regex_pattern)

    def _glob_to_regex(self, pattern: str) -> str:
        """Convert CODEOWNERS glob pattern to regex."""
        # Handle root-relative patterns
        if pattern.startswith("/"):
            pattern = pattern[1:]
            anchor = "^"
        else:
            anchor = "(^|/)"

        # Escape regex special chars except * and ?
        escaped = ""
        i = 0
        while i < len(pattern):
            c = pattern[i]
            if c == "*":
                if i + 1 < len(pattern) and pattern[i + 1] == "*":
                    # ** matches any path
                    escaped += ".*"
                    i += 2
                    # Handle trailing /
                    if i < len(pattern) and pattern[i] == "/":
                        i += 1
                    continue
                else:
                    # * matches anything except /
                    escaped += "[^/]*"
            elif c == "?":
                escaped += "[^/]"
            elif c in ".^$+{}[]|()":
                escaped += "\\" + c
            else:
                escaped += c
            i += 1

        # Handle directory patterns
        if pattern.endswith("/"):
            escaped += ".*"

        return anchor + escaped + "$"

    def matches(self, file_path: str) -> bool:
        """Check if this rule matches a file path."""
        if self._compiled_pattern is None:
            return False

        # Normalize path
        normalized = file_path.lstrip("/")
        return bool(self._compiled_pattern.search(normalized))


@dataclass
class FileOwnership:
    """Ownership information for a specific file."""
    file_path: Path
    owners: List[Owner]
    permission_level: PermissionLevel
    source: OwnershipSource
    rule: Optional[OwnershipRule] = None
    blame_stats: Optional[Dict[str, float]] = None  # user -> percentage
    confidence: float = 1.0
    last_updated: float = field(default_factory=time.time)

    def is_owned_by(self, user_id: str) -> bool:
        """Check if file is owned by a specific user."""
        return any(o.id == user_id for o in self.owners)


@dataclass
class Team:
    """Represents a team of users."""
    id: str
    name: str
    members: Set[str]
    parent_team: Optional[str] = None
    child_teams: Set[str] = field(default_factory=set)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def has_member(self, user_id: str) -> bool:
        """Check if user is a member of this team."""
        return user_id in self.members


@dataclass
class AccessRequest:
    """A request for access to code."""
    user_id: str
    file_path: Path
    requested_permission: PermissionLevel
    reason: str = ""
    timestamp: float = field(default_factory=time.time)


@dataclass
class AccessResult:
    """Result of an access check."""
    decision: AccessDecision
    granted_permission: PermissionLevel
    required_approvers: List[Owner] = field(default_factory=list)
    reason: str = ""
    ownership: Optional[FileOwnership] = None


# =============================================================================
# CODEOWNERS PARSER
# =============================================================================

class CodeownersParser:
    """
    Parses CODEOWNERS files following GitHub/GitLab format.

    Supports:
    - User and team owners (@user, @org/team)
    - Glob patterns (*.py, src/**/tests/)
    - Negation patterns (!)
    - Comments
    - Multiple CODEOWNERS files with precedence
    """

    def __init__(self, config: OwnershipConfig):
        self.config = config
        self._rules: List[OwnershipRule] = []
        self._file_cache: Dict[Path, List[OwnershipRule]] = {}

    async def parse_repository(self, repo_path: Path) -> List[OwnershipRule]:
        """Parse all CODEOWNERS files in a repository."""
        all_rules = []

        for codeowners_path in self.config.codeowners_paths:
            full_path = repo_path / codeowners_path
            if await asyncio.to_thread(full_path.exists):
                rules = await self.parse_file(full_path)
                all_rules.extend(rules)
                logger.info(f"Parsed {len(rules)} rules from {full_path}")

        self._rules = all_rules
        return all_rules

    async def parse_file(self, file_path: Path) -> List[OwnershipRule]:
        """Parse a single CODEOWNERS file."""
        if file_path in self._file_cache:
            return self._file_cache[file_path]

        try:
            content = await asyncio.to_thread(file_path.read_text)
        except Exception as e:
            logger.warning(f"Failed to read CODEOWNERS file {file_path}: {e}")
            return []

        rules = []
        for line_number, line in enumerate(content.splitlines(), 1):
            rule = self._parse_line(line, line_number, file_path)
            if rule:
                rules.append(rule)

        self._file_cache[file_path] = rules
        return rules

    def _parse_line(
        self,
        line: str,
        line_number: int,
        source_file: Path,
    ) -> Optional[OwnershipRule]:
        """Parse a single line from CODEOWNERS."""
        # Remove comments
        comment_idx = line.find("#")
        comment = None
        if comment_idx >= 0:
            comment = line[comment_idx + 1:].strip()
            line = line[:comment_idx]

        line = line.strip()

        # Skip empty lines
        if not line:
            return None

        # Split into pattern and owners
        parts = line.split()
        if len(parts) < 2:
            return None  # Need at least pattern + one owner

        pattern = parts[0]
        owner_strs = parts[1:]

        # Parse owners
        owners = []
        for owner_str in owner_strs:
            owner = self._parse_owner(owner_str)
            if owner:
                owners.append(owner)

        if not owners:
            return None

        return OwnershipRule(
            pattern=pattern,
            owners=owners,
            line_number=line_number,
            source_file=source_file,
            comment=comment,
        )

    def _parse_owner(self, owner_str: str) -> Optional[Owner]:
        """Parse an owner string (@user or @org/team)."""
        if not owner_str.startswith("@"):
            # Could be an email
            if "@" in owner_str:
                return Owner.user(owner_str.split("@")[0], email=owner_str)
            return None

        owner_str = owner_str[1:]  # Remove @

        if "/" in owner_str:
            # Team: @org/team-name
            return Owner.team(owner_str)
        else:
            # User: @username
            return Owner.user(owner_str)

    def get_owners_for_file(self, file_path: str) -> List[Owner]:
        """Get owners for a specific file."""
        # Later rules take precedence
        matching_rule = None
        for rule in self._rules:
            if rule.matches(file_path):
                if rule.is_negation:
                    matching_rule = None
                else:
                    matching_rule = rule

        return matching_rule.owners if matching_rule else []

    def get_rule_for_file(self, file_path: str) -> Optional[OwnershipRule]:
        """Get the matching rule for a file."""
        matching_rule = None
        for rule in self._rules:
            if rule.matches(file_path):
                if rule.is_negation:
                    matching_rule = None
                else:
                    matching_rule = rule
        return matching_rule


# =============================================================================
# GIT BLAME ANALYZER
# =============================================================================

class GitBlameAnalyzer:
    """
    Analyzes git blame to infer code ownership.

    Uses line authorship to determine who has contributed most to a file.
    """

    def __init__(self, config: OwnershipConfig):
        self.config = config
        self._cache: Dict[Path, Dict[str, float]] = {}
        self._cache_times: Dict[Path, float] = {}

    async def analyze_file(self, file_path: Path) -> Dict[str, float]:
        """
        Analyze git blame for a file.

        Returns dict of user -> percentage of lines authored.
        """
        # Check cache
        if self.config.enable_cache:
            if file_path in self._cache:
                cache_age = time.time() - self._cache_times.get(file_path, 0)
                if cache_age < self.config.cache_ttl:
                    return self._cache[file_path]

        try:
            result = await asyncio.create_subprocess_exec(
                "git", "blame", "--line-porcelain", str(file_path),
                cwd=file_path.parent,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(result.communicate(), timeout=30.0)

            if result.returncode != 0:
                return {}

            # Parse blame output
            author_lines = self._parse_blame_output(stdout.decode())

            # Calculate percentages
            total_lines = sum(author_lines.values())
            if total_lines == 0:
                return {}

            percentages = {
                author: count / total_lines
                for author, count in author_lines.items()
            }

            # Cache result
            if self.config.enable_cache:
                self._cache[file_path] = percentages
                self._cache_times[file_path] = time.time()

            return percentages

        except asyncio.TimeoutError:
            logger.warning(f"Git blame timed out for {file_path}")
            return {}
        except Exception as e:
            logger.warning(f"Git blame failed for {file_path}: {e}")
            return {}

    def _parse_blame_output(self, output: str) -> Dict[str, int]:
        """Parse git blame --line-porcelain output."""
        author_lines: Dict[str, int] = defaultdict(int)
        current_author = None

        for line in output.splitlines():
            if line.startswith("author "):
                current_author = line[7:]
            elif line.startswith("author-mail "):
                # Get email if author name is generic
                if current_author in ("Not Committed Yet", "Unknown"):
                    email = line[12:].strip("<>")
                    if email:
                        current_author = email.split("@")[0]
            elif line.startswith("\t") and current_author:
                # This is a content line
                author_lines[current_author] += 1

        return dict(author_lines)

    async def get_primary_owner(self, file_path: Path) -> Optional[Owner]:
        """Get the primary owner based on git blame."""
        percentages = await self.analyze_file(file_path)
        if not percentages:
            return None

        # Get author with highest percentage above threshold
        for author, percentage in sorted(
            percentages.items(),
            key=lambda x: x[1],
            reverse=True
        ):
            if percentage >= self.config.blame_threshold:
                return Owner.user(author)

        return None

    async def get_contributors(
        self,
        file_path: Path,
        min_percentage: float = 0.05,
    ) -> List[Tuple[Owner, float]]:
        """Get all significant contributors to a file."""
        percentages = await self.analyze_file(file_path)

        contributors = []
        for author, percentage in sorted(
            percentages.items(),
            key=lambda x: x[1],
            reverse=True
        ):
            if percentage >= min_percentage:
                contributors.append((Owner.user(author), percentage))

        return contributors


# =============================================================================
# TEAM MANAGER
# =============================================================================

class TeamManager:
    """
    Manages team hierarchies and membership.

    Supports:
    - Nested teams
    - Dynamic membership
    - Team-based permissions
    """

    def __init__(self, config: OwnershipConfig):
        self.config = config
        self._teams: Dict[str, Team] = {}
        self._user_teams: Dict[str, Set[str]] = defaultdict(set)
        self._loaded = False

    async def load_teams(self) -> None:
        """Load team definitions from configuration."""
        if self.config.teams_file and self.config.teams_file.exists():
            try:
                content = await asyncio.to_thread(self.config.teams_file.read_text)
                data = json.loads(content)
                self._parse_teams_config(data)
            except Exception as e:
                logger.warning(f"Failed to load teams file: {e}")

        self._loaded = True

    def _parse_teams_config(self, data: Dict[str, Any]) -> None:
        """Parse teams configuration."""
        for team_id, team_data in data.get("teams", {}).items():
            team = Team(
                id=team_id,
                name=team_data.get("name", team_id),
                members=set(team_data.get("members", [])),
                parent_team=team_data.get("parent"),
                metadata=team_data.get("metadata", {}),
            )
            self._teams[team_id] = team

            # Build reverse index
            for member in team.members:
                self._user_teams[member].add(team_id)

        # Build child team relationships
        for team_id, team in self._teams.items():
            if team.parent_team and team.parent_team in self._teams:
                self._teams[team.parent_team].child_teams.add(team_id)

    def get_team(self, team_id: str) -> Optional[Team]:
        """Get a team by ID."""
        return self._teams.get(team_id)

    def get_user_teams(self, user_id: str) -> Set[str]:
        """Get all teams a user belongs to (including parent teams)."""
        direct_teams = self._user_teams.get(user_id, set())
        all_teams = set(direct_teams)

        # Add parent teams
        for team_id in direct_teams:
            all_teams.update(self._get_ancestor_teams(team_id))

        return all_teams

    def _get_ancestor_teams(self, team_id: str) -> Set[str]:
        """Get all ancestor teams of a team."""
        ancestors = set()
        team = self._teams.get(team_id)

        while team and team.parent_team:
            ancestors.add(team.parent_team)
            team = self._teams.get(team.parent_team)

        return ancestors

    def is_member(self, user_id: str, team_owner: Owner) -> bool:
        """Check if user is a member of a team (directly or via hierarchy)."""
        if team_owner.owner_type != OwnerType.TEAM:
            return False

        user_teams = self.get_user_teams(user_id)
        return team_owner.id in user_teams

    def add_team(self, team: Team) -> None:
        """Add or update a team."""
        self._teams[team.id] = team
        for member in team.members:
            self._user_teams[member].add(team.id)

    def add_user_to_team(self, user_id: str, team_id: str) -> bool:
        """Add a user to a team."""
        team = self._teams.get(team_id)
        if not team:
            return False

        team.members.add(user_id)
        self._user_teams[user_id].add(team_id)
        return True


# =============================================================================
# PERMISSION ENGINE
# =============================================================================

class PermissionEngine:
    """
    Evaluates and enforces permissions.

    Handles:
    - Permission level checks
    - Approval requirements
    - Permission inheritance
    """

    def __init__(
        self,
        config: OwnershipConfig,
        team_manager: TeamManager,
    ):
        self.config = config
        self.team_manager = team_manager

        # Default permission levels for different owner types
        self._default_permissions: Dict[OwnerType, PermissionLevel] = {
            OwnerType.USER: PermissionLevel.WRITE,
            OwnerType.TEAM: PermissionLevel.WRITE,
            OwnerType.GROUP: PermissionLevel.READ,
            OwnerType.BOT: PermissionLevel.SUGGEST,
        }

    async def check_access(
        self,
        user_id: str,
        ownership: FileOwnership,
        requested_permission: PermissionLevel,
    ) -> AccessResult:
        """
        Check if a user has access to perform an action.

        Returns AccessResult with decision and details.
        """
        # Admin always has access
        if await self._is_admin(user_id):
            return AccessResult(
                decision=AccessDecision.ALLOWED,
                granted_permission=PermissionLevel.ADMIN,
                reason="User is admin",
                ownership=ownership,
            )

        # Check if user is an owner
        if ownership.is_owned_by(user_id):
            return AccessResult(
                decision=AccessDecision.ALLOWED,
                granted_permission=PermissionLevel.APPROVE,
                reason="User is file owner",
                ownership=ownership,
            )

        # Check team membership
        for owner in ownership.owners:
            if owner.owner_type == OwnerType.TEAM:
                if self.team_manager.is_member(user_id, owner):
                    granted = self._default_permissions.get(
                        OwnerType.TEAM,
                        PermissionLevel.WRITE
                    )
                    if granted >= requested_permission:
                        return AccessResult(
                            decision=AccessDecision.ALLOWED,
                            granted_permission=granted,
                            reason=f"User is member of team {owner.id}",
                            ownership=ownership,
                        )

        # Check default permission
        default_perm = PermissionLevel[self.config.default_permission.upper()]
        if default_perm >= requested_permission:
            return AccessResult(
                decision=AccessDecision.ALLOWED,
                granted_permission=default_perm,
                reason="Default permission allows access",
                ownership=ownership,
            )

        # Determine if requires approval or is denied
        if requested_permission <= PermissionLevel.SUGGEST:
            return AccessResult(
                decision=AccessDecision.REQUIRES_REVIEW,
                granted_permission=PermissionLevel.SUGGEST,
                required_approvers=ownership.owners,
                reason="Changes require review by code owners",
                ownership=ownership,
            )

        if self.config.strict_mode:
            return AccessResult(
                decision=AccessDecision.DENIED,
                granted_permission=PermissionLevel.NONE,
                reason="Access denied in strict mode",
                ownership=ownership,
            )

        return AccessResult(
            decision=AccessDecision.REQUIRES_APPROVAL,
            granted_permission=PermissionLevel.NONE,
            required_approvers=ownership.owners,
            reason="Changes require approval from code owners",
            ownership=ownership,
        )

    async def _is_admin(self, user_id: str) -> bool:
        """Check if user has admin privileges."""
        # Could check against a list of admins
        admin_users = os.getenv("OWNERSHIP_ADMIN_USERS", "").split(",")
        return user_id in admin_users

    def get_required_approvers(
        self,
        ownership: FileOwnership,
        num_required: int = 1,
    ) -> List[Owner]:
        """Get required approvers for a file."""
        return ownership.owners[:num_required]


# =============================================================================
# CODE OWNERSHIP ENGINE
# =============================================================================

class CodeOwnershipEngine:
    """
    Main code ownership engine coordinating all components.

    Provides high-level API for:
    - Ownership lookup
    - Permission checking
    - Ownership transfer
    """

    def __init__(self, config: Optional[OwnershipConfig] = None):
        self.config = config or OwnershipConfig.from_env()
        self.codeowners_parser = CodeownersParser(self.config)
        self.git_blame_analyzer = GitBlameAnalyzer(self.config)
        self.team_manager = TeamManager(self.config)
        self.permission_engine = PermissionEngine(self.config, self.team_manager)
        self._ownership_cache: Dict[Path, FileOwnership] = {}
        self._repo_roots: Set[Path] = set()
        self._running = False

    async def initialize(self, repo_path: Optional[Path] = None) -> bool:
        """Initialize the ownership engine."""
        try:
            await self.team_manager.load_teams()

            if repo_path:
                await self.index_repository(repo_path)

            self._running = True
            logger.info("Code ownership engine initialized")
            return True
        except Exception as e:
            logger.error(f"Failed to initialize ownership engine: {e}")
            return False

    async def index_repository(self, repo_path: Path) -> int:
        """Index a repository for ownership information."""
        await self.codeowners_parser.parse_repository(repo_path)
        self._repo_roots.add(repo_path)
        logger.info(f"Indexed repository: {repo_path}")
        return len(self.codeowners_parser._rules)

    async def get_ownership(self, file_path: Path) -> FileOwnership:
        """Get ownership information for a file."""
        # Check cache
        if self.config.enable_cache and file_path in self._ownership_cache:
            cached = self._ownership_cache[file_path]
            if time.time() - cached.last_updated < self.config.cache_ttl:
                return cached

        # Get ownership from CODEOWNERS
        relative_path = self._get_relative_path(file_path)
        owners = self.codeowners_parser.get_owners_for_file(str(relative_path))
        rule = self.codeowners_parser.get_rule_for_file(str(relative_path))

        source = OwnershipSource.CODEOWNERS if owners else OwnershipSource.DEFAULT
        confidence = 1.0

        # Fall back to git blame if no CODEOWNERS match
        if not owners and self.config.use_git_blame:
            blame_owner = await self.git_blame_analyzer.get_primary_owner(file_path)
            if blame_owner:
                owners = [blame_owner]
                source = OwnershipSource.GIT_BLAME
                blame_stats = await self.git_blame_analyzer.analyze_file(file_path)
                confidence = blame_stats.get(blame_owner.id, 0.5)

        # Create ownership object
        ownership = FileOwnership(
            file_path=file_path,
            owners=owners,
            permission_level=PermissionLevel.WRITE if owners else PermissionLevel.READ,
            source=source,
            rule=rule,
            confidence=confidence,
        )

        # Add blame stats if available
        if source == OwnershipSource.GIT_BLAME:
            ownership.blame_stats = await self.git_blame_analyzer.analyze_file(file_path)

        # Cache result
        if self.config.enable_cache:
            self._ownership_cache[file_path] = ownership

        return ownership

    def _get_relative_path(self, file_path: Path) -> Path:
        """Get relative path from repository root."""
        for repo_root in self._repo_roots:
            try:
                return file_path.relative_to(repo_root)
            except ValueError:
                continue
        return file_path

    async def check_access(
        self,
        user_id: str,
        file_path: Path,
        permission: PermissionLevel,
    ) -> AccessResult:
        """Check if a user has permission to access a file."""
        ownership = await self.get_ownership(file_path)
        return await self.permission_engine.check_access(user_id, ownership, permission)

    async def get_files_owned_by(
        self,
        owner: Owner,
        repo_path: Optional[Path] = None,
    ) -> List[Path]:
        """Get all files owned by a specific owner."""
        owned_files = []

        if repo_path is None:
            repo_paths = list(self._repo_roots)
        else:
            repo_paths = [repo_path]

        for root in repo_paths:
            async for file_path in self._walk_files(root):
                ownership = await self.get_ownership(file_path)
                if owner in ownership.owners:
                    owned_files.append(file_path)

        return owned_files

    async def _walk_files(self, root: Path):
        """Async generator to walk files in a directory."""
        # Get list of files
        def walk():
            for path in root.rglob("*"):
                if path.is_file() and not any(
                    part.startswith(".") for part in path.parts
                ):
                    yield path

        for path in await asyncio.to_thread(lambda: list(walk())):
            yield path

    async def transfer_ownership(
        self,
        file_path: Path,
        from_owner: Owner,
        to_owner: Owner,
        requester_id: str,
    ) -> bool:
        """Transfer ownership of a file."""
        # Check if requester has permission
        access = await self.check_access(
            requester_id,
            file_path,
            PermissionLevel.ADMIN,
        )

        if access.decision != AccessDecision.ALLOWED:
            logger.warning(
                f"Ownership transfer denied for {file_path}: "
                f"requester {requester_id} lacks admin permission"
            )
            return False

        # Update ownership
        ownership = await self.get_ownership(file_path)
        if from_owner in ownership.owners:
            # Remove old owner and add new
            new_owners = [o for o in ownership.owners if o != from_owner]
            new_owners.append(to_owner)

            # Update cache
            ownership.owners[:] = new_owners
            self._ownership_cache[file_path] = ownership

            logger.info(
                f"Transferred ownership of {file_path} from "
                f"{from_owner.id} to {to_owner.id}"
            )
            return True

        return False

    async def get_ownership_report(
        self,
        repo_path: Path,
    ) -> Dict[str, Any]:
        """Generate ownership report for a repository."""
        report = {
            "repository": str(repo_path),
            "total_files": 0,
            "owned_files": 0,
            "unowned_files": 0,
            "owners": defaultdict(int),
            "sources": defaultdict(int),
        }

        async for file_path in self._walk_files(repo_path):
            report["total_files"] += 1
            ownership = await self.get_ownership(file_path)

            if ownership.owners:
                report["owned_files"] += 1
                for owner in ownership.owners:
                    report["owners"][owner.id] += 1
            else:
                report["unowned_files"] += 1

            report["sources"][ownership.source.value] += 1

        # Convert defaultdicts to regular dicts
        report["owners"] = dict(report["owners"])
        report["sources"] = dict(report["sources"])

        return report

    def get_stats(self) -> Dict[str, Any]:
        """Get ownership engine statistics."""
        return {
            "running": self._running,
            "indexed_repos": len(self._repo_roots),
            "cached_ownerships": len(self._ownership_cache),
            "codeowners_rules": len(self.codeowners_parser._rules),
            "teams": len(self.team_manager._teams),
        }


# =============================================================================
# CROSS-REPO OWNERSHIP
# =============================================================================

class CrossRepoOwnershipCoordinator:
    """
    Coordinates ownership across JARVIS, JARVIS-Prime, and Reactor-Core.

    Enables:
    - Cross-repo ownership lookup
    - Unified team management
    - Ownership synchronization
    """

    def __init__(self, config: Optional[OwnershipConfig] = None):
        self.config = config or OwnershipConfig.from_env()
        self._engines: Dict[str, CodeOwnershipEngine] = {}
        self._running = False

    async def initialize(self) -> bool:
        """Initialize cross-repo ownership."""
        if not self.config.enable_cross_repo:
            return True

        try:
            from backend.core.ouroboros.cross_repo import (
                CrossRepoConfig,
                RepoType,
            )

            # Initialize engine for each repo
            repos = {
                "jarvis": CrossRepoConfig.JARVIS_REPO,
                "prime": CrossRepoConfig.PRIME_REPO,
                "reactor": CrossRepoConfig.REACTOR_REPO,
            }

            for repo_id, repo_path in repos.items():
                if repo_path.exists():
                    engine = CodeOwnershipEngine(self.config)
                    await engine.initialize(repo_path)
                    self._engines[repo_id] = engine

            self._running = True
            logger.info("Cross-repo ownership coordinator initialized")
            return True
        except ImportError:
            logger.warning("Cross-repo module not available")
            return False

    async def get_ownership(
        self,
        repo_id: str,
        file_path: Path,
    ) -> Optional[FileOwnership]:
        """Get ownership from a specific repository."""
        engine = self._engines.get(repo_id)
        if not engine:
            return None
        return await engine.get_ownership(file_path)

    async def check_cross_repo_access(
        self,
        user_id: str,
        file_paths: Dict[str, Path],  # repo_id -> file_path
        permission: PermissionLevel,
    ) -> Dict[str, AccessResult]:
        """Check access across multiple repositories."""
        results = {}

        for repo_id, file_path in file_paths.items():
            engine = self._engines.get(repo_id)
            if engine:
                results[repo_id] = await engine.check_access(
                    user_id, file_path, permission
                )

        return results

    async def shutdown(self) -> None:
        """Shutdown cross-repo ownership."""
        self._running = False


# =============================================================================
# GLOBAL INSTANCES
# =============================================================================

_ownership_engine: Optional[CodeOwnershipEngine] = None
_cross_repo_coordinator: Optional[CrossRepoOwnershipCoordinator] = None


def get_ownership_engine(
    config: Optional[OwnershipConfig] = None
) -> CodeOwnershipEngine:
    """
    Get or create the global ownership engine.

    Args:
        config: Optional configuration. If provided and engine doesn't exist,
               uses this config. If engine exists, config is ignored.

    Returns:
        The global CodeOwnershipEngine instance.
    """
    global _ownership_engine
    if _ownership_engine is None:
        _ownership_engine = CodeOwnershipEngine(config=config)
    return _ownership_engine


def get_cross_repo_ownership_coordinator() -> CrossRepoOwnershipCoordinator:
    """Get the global cross-repo ownership coordinator."""
    global _cross_repo_coordinator
    if _cross_repo_coordinator is None:
        _cross_repo_coordinator = CrossRepoOwnershipCoordinator()
    return _cross_repo_coordinator


async def initialize_ownership(repo_path: Optional[Path] = None) -> bool:
    """Initialize ownership system."""
    engine = get_ownership_engine()
    success = await engine.initialize(repo_path)

    if success:
        coordinator = get_cross_repo_ownership_coordinator()
        await coordinator.initialize()

    return success


async def shutdown_ownership() -> None:
    """Shutdown ownership system."""
    global _ownership_engine, _cross_repo_coordinator

    if _cross_repo_coordinator:
        await _cross_repo_coordinator.shutdown()
        _cross_repo_coordinator = None

    _ownership_engine = None
