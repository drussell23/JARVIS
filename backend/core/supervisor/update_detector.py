#!/usr/bin/env python3
"""
JARVIS Update Detector v2.0
=============================

Async GitHub polling and LOCAL change detection for the Self-Updating Lifecycle Manager.

Features:
- Remote update detection (GitHub API + git fetch)
- LOCAL change awareness (detects your commits, pushes, and code changes)
- Intelligent restart recommendations
- File change monitoring with debouncing
- Parallel async operations for performance

Author: JARVIS System
Version: 2.0.0
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

import aiohttp

from .supervisor_config import SupervisorConfig, UpdateSource, get_supervisor_config

logger = logging.getLogger(__name__)


class ChangeType(str, Enum):
    """Types of detected changes."""
    REMOTE_UPDATE = "remote_update"      # Remote has commits we don't have
    LOCAL_COMMIT = "local_commit"         # User made a new local commit
    LOCAL_PUSH = "local_push"             # User pushed to remote
    UNCOMMITTED = "uncommitted"           # Uncommitted local changes
    STASH = "stash"                        # Changes stashed
    BRANCH_SWITCH = "branch_switch"       # Branch was switched


@dataclass
class CommitInfo:
    """Information about a git commit."""
    sha: str
    message: str
    author: str
    date: datetime
    url: Optional[str] = None
    is_local: bool = False  # True if this is a local commit not yet pushed


@dataclass
class LocalChangeInfo:
    """Information about local repository changes."""
    has_changes: bool = False
    change_type: Optional[ChangeType] = None
    commits_since_start: int = 0
    new_commits: list[CommitInfo] = field(default_factory=list)
    uncommitted_files: int = 0
    modified_files: list[str] = field(default_factory=list)
    current_branch: Optional[str] = None
    started_on_commit: Optional[str] = None
    started_on_branch: Optional[str] = None
    summary: str = ""
    restart_recommended: bool = False
    restart_reason: Optional[str] = None
    detected_at: datetime = field(default_factory=datetime.now)


@dataclass
class UpdateInfo:
    """Information about an available update."""
    available: bool = False
    current_version: Optional[str] = None
    remote_version: Optional[str] = None
    commits_behind: int = 0
    commits: list[CommitInfo] = field(default_factory=list)
    summary: str = ""
    is_release: bool = False
    release_tag: Optional[str] = None
    release_notes: Optional[str] = None
    checked_at: datetime = field(default_factory=datetime.now)
    # v2.0: Local change awareness
    local_changes: Optional[LocalChangeInfo] = None


class UpdateDetector:
    """
    Async GitHub polling for update detection with LOCAL change awareness.

    Features:
    - Configurable polling intervals
    - GitHub API rate limit awareness
    - Local cache of last known commit
    - Semantic version comparison
    - Release and commit-based detection
    - LOCAL CHANGE AWARENESS (v2.0):
      - Detects commits made since JARVIS started
      - Detects pushes to remote
      - Tracks uncommitted changes
      - Intelligent restart recommendations
      - Parallel async operations

    Example:
        >>> detector = UpdateDetector(config)
        >>> update_info = await detector.check_for_updates()
        >>> if update_info.available:
        ...     print(f"Update available: {update_info.summary}")
        >>> if update_info.local_changes and update_info.local_changes.has_changes:
        ...     print(f"Local changes: {update_info.local_changes.summary}")
    """

    # GitHub API base
    GITHUB_API = "https://api.github.com"

    # Files that trigger restart recommendation
    RESTART_TRIGGER_PATTERNS = [
        r".*\.py$",           # Python files
        r".*\.yaml$",         # Config files
        r".*\.json$",         # JSON configs
        r"requirements.*\.txt$",  # Dependencies
        r"pyproject\.toml$",  # Project config
    ]

    # Files to ignore for restart recommendation
    IGNORE_PATTERNS = [
        r".*\.md$",           # Documentation
        r".*\.txt$",          # Text files (except requirements)
        r"__pycache__",       # Cache
        r"\.pyc$",            # Compiled Python
        r"logs/",             # Log files
        r"\.git/",            # Git internals
    ]

    def __init__(
        self,
        config: Optional[SupervisorConfig] = None,
        repo_path: Optional[Path] = None,
    ):
        """
        Initialize the update detector.

        Args:
            config: Supervisor configuration
            repo_path: Path to local git repository
        """
        self.config = config or get_supervisor_config()
        self.repo_path = repo_path or self._detect_repo_path()

        self._session: Optional[aiohttp.ClientSession] = None
        self._last_check: Optional[datetime] = None
        self._cached_update: Optional[UpdateInfo] = None
        self._rate_limit_reset: Optional[datetime] = None

        # Extract owner/repo from git remote
        self._owner: Optional[str] = None
        self._repo: Optional[str] = None

        # v2.0: Local change awareness state
        self._startup_commit: Optional[str] = None
        self._startup_branch: Optional[str] = None
        self._startup_time: datetime = datetime.now()
        self._last_known_commit: Optional[str] = None
        self._cached_local_changes: Optional[LocalChangeInfo] = None
        self._local_change_callbacks: list[Callable[[LocalChangeInfo], None]] = []

        # Track announced changes to avoid spam
        self._announced_commits: set[str] = set()

        logger.info("🔧 Update detector v2.0 initialized with local change awareness")
    
    def _detect_repo_path(self) -> Path:
        """Detect the git repository path."""
        current = Path(__file__).resolve()
        for parent in current.parents:
            if (parent / ".git").exists():
                return parent
        return Path.cwd()
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self._session is None or self._session.closed:
            headers = {
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "JARVIS-Update-Detector/1.0",
            }
            
            # Add GitHub token if available
            token = os.environ.get("GITHUB_TOKEN")
            if token:
                headers["Authorization"] = f"token {token}"
            
            self._session = aiohttp.ClientSession(
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            )
        
        return self._session
    
    async def _run_git_command(
        self,
        command: str,
        timeout: int = 30,
    ) -> tuple[bool, str]:
        """Run a git command asynchronously."""
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=str(self.repo_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout,
            )
            
            if process.returncode == 0:
                return True, stdout.decode().strip()
            else:
                return False, stderr.decode().strip()
                
        except asyncio.TimeoutError:
            return False, "Command timed out"
        except Exception as e:
            return False, str(e)
    
    async def _get_remote_info(self) -> tuple[Optional[str], Optional[str]]:
        """Get owner and repo from git remote."""
        if self._owner and self._repo:
            return self._owner, self._repo
        
        remote = self.config.update.remote
        success, output = await self._run_git_command(
            f"git remote get-url {remote}"
        )
        
        if not success:
            return None, None
        
        # Parse GitHub URL
        # SSH: git@github.com:owner/repo.git
        # HTTPS: https://github.com/owner/repo.git
        patterns = [
            r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?$",
        ]
        
        for pattern in patterns:
            match = re.search(pattern, output)
            if match:
                self._owner = match.group(1)
                self._repo = match.group(2)
                return self._owner, self._repo
        
        return None, None
    
    async def get_current_commit(self) -> Optional[str]:
        """Get the current local commit."""
        success, output = await self._run_git_command("git rev-parse HEAD")
        return output if success else None
    
    async def get_local_commits(self, limit: int = 10) -> list[CommitInfo]:
        """Get recent local commits."""
        success, output = await self._run_git_command(
            f'git log -n {limit} --format="%H|%s|%an|%aI"'
        )
        
        if not success:
            return []
        
        commits = []
        for line in output.split("\n"):
            if line:
                parts = line.split("|", 3)
                if len(parts) >= 4:
                    commits.append(CommitInfo(
                        sha=parts[0],
                        message=parts[1],
                        author=parts[2],
                        date=datetime.fromisoformat(parts[3]),
                    ))
        
        return commits
    
    async def _check_rate_limit(self) -> bool:
        """Check if we're within GitHub rate limits."""
        if self._rate_limit_reset:
            if datetime.now() < self._rate_limit_reset:
                remaining_seconds = (
                    self._rate_limit_reset - datetime.now()
                ).total_seconds()
                logger.debug(f"Rate limited, {remaining_seconds:.0f}s remaining")
                return False
            else:
                self._rate_limit_reset = None
        return True
    
    async def _handle_rate_limit(
        self,
        response: aiohttp.ClientResponse,
    ) -> None:
        """Handle rate limit headers from GitHub."""
        remaining = response.headers.get("X-RateLimit-Remaining", "")
        reset = response.headers.get("X-RateLimit-Reset", "")
        
        if remaining and int(remaining) == 0 and reset:
            self._rate_limit_reset = datetime.fromtimestamp(int(reset))
            logger.warning(
                f"⚠️ GitHub rate limit reached, resets at {self._rate_limit_reset}"
            )
    
    async def check_github_api(self) -> Optional[UpdateInfo]:
        """Check for updates using GitHub API."""
        if not await self._check_rate_limit():
            return self._cached_update
        
        owner, repo = await self._get_remote_info()
        if not owner or not repo:
            logger.warning("Could not determine GitHub owner/repo")
            return None
        
        session = await self._get_session()
        branch = self.config.update.branch
        
        try:
            # Get latest commit from remote branch
            url = f"{self.GITHUB_API}/repos/{owner}/{repo}/branches/{branch}"
            
            async with session.get(url) as response:
                await self._handle_rate_limit(response)
                
                if response.status == 200:
                    data = await response.json()
                    remote_sha = data.get("commit", {}).get("sha", "")
                    
                    current_sha = await self.get_current_commit()
                    
                    if current_sha and remote_sha and current_sha != remote_sha:
                        # Get commits between current and remote
                        compare_url = (
                            f"{self.GITHUB_API}/repos/{owner}/{repo}"
                            f"/compare/{current_sha[:12]}...{remote_sha[:12]}"
                        )
                        
                        async with session.get(compare_url) as compare_response:
                            if compare_response.status == 200:
                                compare_data = await compare_response.json()
                                commits_data = compare_data.get("commits", [])
                                
                                commits = []
                                for c in commits_data[:10]:  # Limit to 10
                                    commits.append(CommitInfo(
                                        sha=c.get("sha", ""),
                                        message=c.get("commit", {}).get("message", "").split("\n")[0],
                                        author=c.get("commit", {}).get("author", {}).get("name", ""),
                                        date=datetime.fromisoformat(
                                            c.get("commit", {}).get("author", {}).get("date", "").replace("Z", "+00:00")
                                        ),
                                        url=c.get("html_url"),
                                    ))
                                
                                return UpdateInfo(
                                    available=True,
                                    current_version=current_sha[:12],
                                    remote_version=remote_sha[:12],
                                    commits_behind=compare_data.get("ahead_by", len(commits)),
                                    commits=commits,
                                    summary=self._generate_summary(commits),
                                    checked_at=datetime.now(),
                                )
                    
                    # No update available
                    return UpdateInfo(
                        available=False,
                        current_version=current_sha[:12] if current_sha else None,
                        remote_version=remote_sha[:12],
                        checked_at=datetime.now(),
                    )
                
                elif response.status == 404:
                    logger.warning(f"Repository or branch not found: {owner}/{repo}/{branch}")
                    return None
                else:
                    logger.warning(f"GitHub API error: {response.status}")
                    return None
                    
        except aiohttp.ClientError as e:
            logger.error(f"GitHub API request failed: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error checking GitHub: {e}")
            return None
    
    async def check_git_fetch(self) -> Optional[UpdateInfo]:
        """Check for updates using local git fetch."""
        remote = self.config.update.remote
        branch = self.config.update.branch
        
        # Fetch from remote
        success, _ = await self._run_git_command(
            f"git fetch {remote}",
            timeout=60,
        )
        
        if not success:
            logger.warning("Git fetch failed")
            return None
        
        # Check commits behind
        success, output = await self._run_git_command(
            f"git rev-list HEAD..{remote}/{branch} --count"
        )
        
        if not success:
            return None
        
        try:
            commits_behind = int(output.strip())
        except ValueError:
            commits_behind = 0
        
        if commits_behind == 0:
            current = await self.get_current_commit()
            return UpdateInfo(
                available=False,
                current_version=current[:12] if current else None,
                checked_at=datetime.now(),
            )
        
        # Get log of new commits
        success, log_output = await self._run_git_command(
            f'git log HEAD..{remote}/{branch} --format="%H|%s|%an|%aI" -n 10'
        )
        
        commits = []
        if success:
            for line in log_output.split("\n"):
                if line:
                    parts = line.split("|", 3)
                    if len(parts) >= 4:
                        commits.append(CommitInfo(
                            sha=parts[0],
                            message=parts[1],
                            author=parts[2],
                            date=datetime.fromisoformat(parts[3]),
                        ))
        
        current = await self.get_current_commit()
        success, remote_head = await self._run_git_command(
            f"git rev-parse {remote}/{branch}"
        )
        
        return UpdateInfo(
            available=True,
            current_version=current[:12] if current else None,
            remote_version=remote_head[:12] if success else None,
            commits_behind=commits_behind,
            commits=commits,
            summary=self._generate_summary(commits),
            checked_at=datetime.now(),
        )
    
    def _generate_summary(self, commits: list[CommitInfo]) -> str:
        """Generate a human-readable summary of commits."""
        if not commits:
            return "Updates available"
        
        if len(commits) == 1:
            return commits[0].message
        
        # Categorize commits
        features = []
        fixes = []
        other = []
        
        for commit in commits:
            msg = commit.message.lower()
            if any(kw in msg for kw in ["feat", "add", "new", "implement"]):
                features.append(commit.message)
            elif any(kw in msg for kw in ["fix", "bug", "patch", "resolve"]):
                fixes.append(commit.message)
            else:
                other.append(commit.message)
        
        parts = []
        if features:
            parts.append(f"{len(features)} new feature(s)")
        if fixes:
            parts.append(f"{len(fixes)} fix(es)")
        if other:
            parts.append(f"{len(other)} other change(s)")
        
        return ", ".join(parts) if parts else f"{len(commits)} commits"
    
    async def check_for_updates(
        self,
        force: bool = False,
    ) -> Optional[UpdateInfo]:
        """
        Check for available updates.
        
        Args:
            force: Force check even if recently checked
            
        Returns:
            UpdateInfo with update details
        """
        # Check cache
        if not force and self._cached_update:
            elapsed = (datetime.now() - self._cached_update.checked_at).total_seconds()
            if elapsed < self.config.update.check.interval_seconds:
                return self._cached_update
        
        logger.info("🔍 Checking for updates...")
        
        # Try GitHub API first if using GitHub
        if self.config.update.source_type == UpdateSource.GITHUB:
            update_info = await self.check_github_api()
            if update_info:
                self._cached_update = update_info
                
                if update_info.available:
                    logger.info(
                        f"📦 Update available: {update_info.commits_behind} commit(s) behind"
                    )
                else:
                    logger.info("✅ Already up to date")
                
                return update_info
        
        # Fallback to git fetch
        update_info = await self.check_git_fetch()
        if update_info:
            self._cached_update = update_info
            
            if update_info.available:
                logger.info(
                    f"📦 Update available: {update_info.commits_behind} commit(s) behind"
                )
            else:
                logger.info("✅ Already up to date")
        
        return update_info
    
    def get_cached_update(self) -> Optional[UpdateInfo]:
        """Get the cached update info (if any)."""
        return self._cached_update

    # =========================================================================
    # v2.0: LOCAL CHANGE AWARENESS
    # =========================================================================

    async def initialize_baseline(self) -> None:
        """
        Initialize the baseline state for local change detection.
        Call this when JARVIS starts to record the initial commit/branch.
        """
        # Get current commit and branch in parallel
        commit_task = asyncio.create_task(self.get_current_commit())
        branch_task = asyncio.create_task(self._get_current_branch())

        self._startup_commit, self._startup_branch = await asyncio.gather(
            commit_task, branch_task
        )
        self._last_known_commit = self._startup_commit
        self._startup_time = datetime.now()

        if self._startup_commit:
            self._announced_commits.add(self._startup_commit)

        logger.info(
            f"📍 Baseline established: {self._startup_commit[:12] if self._startup_commit else 'unknown'} "
            f"on branch '{self._startup_branch}'"
        )

    async def _get_current_branch(self) -> Optional[str]:
        """Get the current git branch name."""
        success, output = await self._run_git_command("git branch --show-current")
        return output if success else None

    async def _get_uncommitted_changes(self) -> tuple[int, list[str]]:
        """Get count and list of uncommitted changes."""
        success, output = await self._run_git_command(
            "git status --porcelain"
        )
        if not success or not output:
            return 0, []

        files = []
        for line in output.split("\n"):
            if line.strip():
                # Format: "XY filename" - extract filename
                parts = line.strip().split(maxsplit=1)
                if len(parts) >= 2:
                    files.append(parts[1])

        return len(files), files

    async def _get_commits_since(self, since_commit: str) -> list[CommitInfo]:
        """Get all commits since a specific commit."""
        success, output = await self._run_git_command(
            f'git log {since_commit}..HEAD --format="%H|%s|%an|%aI" -n 20'
        )

        if not success or not output:
            return []

        commits = []
        for line in output.split("\n"):
            if line:
                parts = line.split("|", 3)
                if len(parts) >= 4:
                    commits.append(CommitInfo(
                        sha=parts[0],
                        message=parts[1],
                        author=parts[2],
                        date=datetime.fromisoformat(parts[3]),
                        is_local=True,  # These are local commits
                    ))

        return commits

    async def _check_if_pushed(self, commit_sha: str) -> bool:
        """Check if a commit has been pushed to remote."""
        remote = self.config.update.remote
        branch = self.config.update.branch

        # Check if commit is an ancestor of remote branch
        success, _ = await self._run_git_command(
            f"git merge-base --is-ancestor {commit_sha} {remote}/{branch}"
        )
        return success

    def _should_recommend_restart(self, files: list[str]) -> tuple[bool, Optional[str]]:
        """
        Determine if a restart should be recommended based on changed files.

        Returns:
            Tuple of (should_restart, reason)
        """
        if not files:
            return False, None

        # Check for restart triggers
        python_files = []
        config_files = []
        dependency_files = []

        for file in files:
            # Skip ignored patterns
            if any(re.search(pattern, file) for pattern in self.IGNORE_PATTERNS):
                continue

            if file.endswith('.py'):
                python_files.append(file)
            elif file.endswith(('.yaml', '.yml', '.json')):
                config_files.append(file)
            elif 'requirements' in file or file == 'pyproject.toml':
                dependency_files.append(file)

        # Priority reasons
        if dependency_files:
            return True, f"Dependencies changed: {', '.join(dependency_files[:3])}"
        if config_files:
            return True, f"Config files changed: {', '.join(config_files[:3])}"
        if len(python_files) >= 3:
            return True, f"{len(python_files)} Python files modified"
        if python_files:
            # Check for critical files
            critical_patterns = ['main.py', 'supervisor', 'startup', 'config']
            for pf in python_files:
                if any(crit in pf.lower() for crit in critical_patterns):
                    return True, f"Critical file modified: {pf}"

        return False, None

    def _generate_local_summary(self, info: LocalChangeInfo) -> str:
        """Generate a human-readable summary of local changes."""
        parts = []

        if info.commits_since_start > 0:
            parts.append(f"{info.commits_since_start} new commit(s)")

        if info.uncommitted_files > 0:
            parts.append(f"{info.uncommitted_files} uncommitted file(s)")

        if info.change_type == ChangeType.BRANCH_SWITCH:
            parts.append(f"branch switched to '{info.current_branch}'")

        if info.change_type == ChangeType.LOCAL_PUSH:
            parts.append("pushed to remote")

        if not parts:
            return "No significant changes"

        return ", ".join(parts)

    async def check_local_changes(self, force: bool = False) -> LocalChangeInfo:
        """
        Check for local repository changes since JARVIS started.

        This detects:
        - New commits made since startup
        - Uncommitted changes
        - Branch switches
        - Pushes to remote

        Args:
            force: Force check even if recently checked

        Returns:
            LocalChangeInfo with detailed change information
        """
        # Initialize baseline if not done
        if not self._startup_commit:
            await self.initialize_baseline()
            return LocalChangeInfo(
                has_changes=False,
                started_on_commit=self._startup_commit,
                started_on_branch=self._startup_branch,
                summary="Baseline just established",
            )

        # Run parallel git queries for performance
        current_commit_task = asyncio.create_task(self.get_current_commit())
        current_branch_task = asyncio.create_task(self._get_current_branch())
        uncommitted_task = asyncio.create_task(self._get_uncommitted_changes())

        current_commit, current_branch, (uncommitted_count, modified_files) = await asyncio.gather(
            current_commit_task, current_branch_task, uncommitted_task
        )

        # Detect change type
        change_type: Optional[ChangeType] = None
        new_commits: list[CommitInfo] = []
        commits_since_start = 0

        # Check for branch switch
        if current_branch != self._startup_branch:
            change_type = ChangeType.BRANCH_SWITCH
            logger.info(f"🔀 Branch switch detected: {self._startup_branch} → {current_branch}")

        # Check for new commits
        if current_commit and current_commit != self._last_known_commit:
            new_commits = await self._get_commits_since(self._startup_commit)
            commits_since_start = len(new_commits)

            if commits_since_start > 0:
                # Check if the latest commit was pushed
                is_pushed = await self._check_if_pushed(current_commit)
                if is_pushed:
                    change_type = ChangeType.LOCAL_PUSH
                else:
                    change_type = ChangeType.LOCAL_COMMIT

                # Log new commits (avoiding duplicates)
                for commit in new_commits:
                    if commit.sha not in self._announced_commits:
                        self._announced_commits.add(commit.sha)
                        logger.info(
                            f"📝 New commit detected: {commit.sha[:8]} - {commit.message[:50]}"
                        )

            self._last_known_commit = current_commit

        # Check for uncommitted changes
        elif uncommitted_count > 0 and change_type is None:
            change_type = ChangeType.UNCOMMITTED

        # Determine if restart is recommended
        all_changed_files = modified_files.copy()
        for commit in new_commits:
            # Get files changed in each commit
            success, files_output = await self._run_git_command(
                f"git diff-tree --no-commit-id --name-only -r {commit.sha}"
            )
            if success and files_output:
                all_changed_files.extend(files_output.split("\n"))

        restart_recommended, restart_reason = self._should_recommend_restart(all_changed_files)

        # Build result
        has_changes = (
            commits_since_start > 0 or
            uncommitted_count > 0 or
            change_type == ChangeType.BRANCH_SWITCH
        )

        info = LocalChangeInfo(
            has_changes=has_changes,
            change_type=change_type,
            commits_since_start=commits_since_start,
            new_commits=new_commits,
            uncommitted_files=uncommitted_count,
            modified_files=modified_files,
            current_branch=current_branch,
            started_on_commit=self._startup_commit[:12] if self._startup_commit else None,
            started_on_branch=self._startup_branch,
            restart_recommended=restart_recommended,
            restart_reason=restart_reason,
            detected_at=datetime.now(),
        )
        info.summary = self._generate_local_summary(info)

        self._cached_local_changes = info

        # Trigger callbacks if changes detected
        if has_changes:
            await self._notify_local_changes(info)

        return info

    def on_local_change(self, callback: Callable[[LocalChangeInfo], None]) -> None:
        """Register a callback for local change notifications."""
        self._local_change_callbacks.append(callback)

    async def _notify_local_changes(self, info: LocalChangeInfo) -> None:
        """Notify all registered callbacks about local changes."""
        for callback in self._local_change_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(info)
                else:
                    callback(info)
            except Exception as e:
                logger.error(f"Local change callback error: {e}")

    async def check_for_updates(
        self,
        force: bool = False,
    ) -> Optional[UpdateInfo]:
        """
        Check for available updates AND local changes.

        Args:
            force: Force check even if recently checked

        Returns:
            UpdateInfo with update details and local change awareness
        """
        # Check cache
        if not force and self._cached_update:
            elapsed = (datetime.now() - self._cached_update.checked_at).total_seconds()
            if elapsed < self.config.update.check.interval_seconds:
                return self._cached_update

        logger.info("🔍 Checking for updates...")

        # Run remote check and local check in parallel for performance
        remote_task = asyncio.create_task(self._check_remote_updates())
        local_task = asyncio.create_task(self.check_local_changes())

        update_info, local_changes = await asyncio.gather(
            remote_task, local_task
        )

        # Merge results
        if update_info:
            update_info.local_changes = local_changes
            self._cached_update = update_info

            if update_info.available:
                logger.info(
                    f"📦 Remote update available: {update_info.commits_behind} commit(s) behind"
                )
            else:
                logger.info("✅ Remote is up to date")

            if local_changes.has_changes:
                logger.info(f"📝 Local changes: {local_changes.summary}")
                if local_changes.restart_recommended:
                    logger.info(f"🔄 Restart recommended: {local_changes.restart_reason}")

            return update_info

        # If remote check failed, still return local changes
        return UpdateInfo(
            available=False,
            checked_at=datetime.now(),
            local_changes=local_changes,
        )

    async def _check_remote_updates(self) -> Optional[UpdateInfo]:
        """Check for remote updates (original logic, now async helper)."""
        # Try GitHub API first if using GitHub
        if self.config.update.source_type == UpdateSource.GITHUB:
            update_info = await self.check_github_api()
            if update_info:
                return update_info

        # Fallback to git fetch
        return await self.check_git_fetch()

    def get_local_changes(self) -> Optional[LocalChangeInfo]:
        """Get the cached local change info (if any)."""
        return self._cached_local_changes

    def get_startup_info(self) -> dict[str, Any]:
        """Get information about when JARVIS started."""
        return {
            "startup_commit": self._startup_commit[:12] if self._startup_commit else None,
            "startup_branch": self._startup_branch,
            "startup_time": self._startup_time.isoformat(),
            "uptime_seconds": (datetime.now() - self._startup_time).total_seconds(),
        }

    async def close(self) -> None:
        """Close resources."""
        if self._session and not self._session.closed:
            await self._session.close()
