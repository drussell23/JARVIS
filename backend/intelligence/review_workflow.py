"""
Review Workflow System - PR/MR Integration
============================================

Production-grade review workflow system with:
- GitHub/GitLab PR/MR integration
- Automated review requests based on ownership
- Review status tracking and enforcement
- CI/CD pipeline integration
- Merge requirement validation

Architecture:
    ┌─────────────────────────────────────────────────────────────────────────┐
    │                    Review Workflow System v1.0                           │
    ├─────────────────────────────────────────────────────────────────────────┤
    │                                                                          │
    │   ┌─────────────┐     ┌─────────────┐     ┌─────────────┐               │
    │   │   GitHub    │     │   Review    │     │   GitLab    │               │
    │   │   Client    │────▶│   Engine    │◀────│   Client    │               │
    │   └─────────────┘     └─────────────┘     └─────────────┘               │
    │          │                   │                   │                      │
    │          └───────────────────┴───────────────────┘                      │
    │                              │                                          │
    │                    ┌─────────▼─────────┐                                │
    │                    │  Workflow Engine  │                                │
    │                    │                   │                                │
    │                    │ • Auto-assign     │                                │
    │                    │ • Status tracking │                                │
    │                    │ • Merge checks    │                                │
    │                    └───────────────────┘                                │
    │                                                                          │
    └─────────────────────────────────────────────────────────────────────────┘

Author: JARVIS Intelligence System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Protocol,
    Set,
    Tuple,
    TypeVar,
    Union,
)
from urllib.parse import urlparse

logger = logging.getLogger("JARVIS.ReviewWorkflow")


# =============================================================================
# CONFIGURATION
# =============================================================================

@dataclass(frozen=True)
class ReviewWorkflowConfig:
    """Environment-driven review workflow configuration."""

    # Platform settings
    platform: str = os.getenv("REVIEW_PLATFORM", "github")  # github, gitlab
    api_token: str = os.getenv("REVIEW_API_TOKEN", "")
    api_url: str = os.getenv("REVIEW_API_URL", "https://api.github.com")

    # Review settings
    required_approvals: int = int(os.getenv("REVIEW_REQUIRED_APPROVALS", "1"))
    require_codeowner_approval: bool = os.getenv("REVIEW_REQUIRE_CODEOWNER", "true").lower() == "true"
    auto_assign_reviewers: bool = os.getenv("REVIEW_AUTO_ASSIGN", "true").lower() == "true"
    dismiss_stale_reviews: bool = os.getenv("REVIEW_DISMISS_STALE", "true").lower() == "true"

    # Branch settings
    protected_branches: Tuple[str, ...] = tuple(
        os.getenv("REVIEW_PROTECTED_BRANCHES", "main,master,develop").split(",")
    )
    require_branch_up_to_date: bool = os.getenv("REVIEW_REQUIRE_UP_TO_DATE", "true").lower() == "true"

    # CI/CD settings
    require_ci_pass: bool = os.getenv("REVIEW_REQUIRE_CI_PASS", "true").lower() == "true"
    ci_timeout: int = int(os.getenv("REVIEW_CI_TIMEOUT", "3600"))

    # Webhook settings
    webhook_secret: str = os.getenv("REVIEW_WEBHOOK_SECRET", "")
    webhook_port: int = int(os.getenv("REVIEW_WEBHOOK_PORT", "8080"))

    # Cross-repo
    enable_cross_repo: bool = os.getenv("REVIEW_CROSS_REPO_ENABLED", "true").lower() == "true"

    @classmethod
    def from_env(cls) -> "ReviewWorkflowConfig":
        """Create configuration from environment."""
        return cls()


# =============================================================================
# ENUMS
# =============================================================================

class ReviewPlatform(Enum):
    """Supported review platforms."""
    GITHUB = "github"
    GITLAB = "gitlab"
    BITBUCKET = "bitbucket"


class PullRequestState(Enum):
    """State of a pull request."""
    OPEN = "open"
    CLOSED = "closed"
    MERGED = "merged"
    DRAFT = "draft"


class ReviewState(Enum):
    """State of a review."""
    PENDING = "pending"
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    COMMENTED = "commented"
    DISMISSED = "dismissed"


class CheckStatus(Enum):
    """Status of a CI check."""
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class MergeStatus(Enum):
    """Overall merge readiness status."""
    READY = "ready"
    BLOCKED = "blocked"
    PENDING = "pending"
    CONFLICTED = "conflicted"


class MergeBlockReason(Enum):
    """Reasons a PR cannot be merged."""
    INSUFFICIENT_APPROVALS = "insufficient_approvals"
    CODEOWNER_APPROVAL_REQUIRED = "codeowner_approval_required"
    CHANGES_REQUESTED = "changes_requested"
    CI_FAILED = "ci_failed"
    CI_PENDING = "ci_pending"
    MERGE_CONFLICTS = "merge_conflicts"
    BRANCH_OUT_OF_DATE = "branch_out_of_date"
    DRAFT_PR = "draft_pr"
    REVIEW_REQUIRED = "review_required"


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class User:
    """Represents a platform user."""
    id: str
    username: str
    email: Optional[str] = None
    name: Optional[str] = None
    avatar_url: Optional[str] = None


@dataclass
class Repository:
    """Represents a repository."""
    id: str
    owner: str
    name: str
    full_name: str
    default_branch: str = "main"
    platform: ReviewPlatform = ReviewPlatform.GITHUB
    url: Optional[str] = None


@dataclass
class Branch:
    """Represents a branch."""
    name: str
    sha: str
    is_protected: bool = False


@dataclass
class FileChange:
    """Represents a changed file in a PR."""
    path: str
    status: str  # added, modified, removed, renamed
    additions: int = 0
    deletions: int = 0
    previous_path: Optional[str] = None  # For renames


@dataclass
class Review:
    """Represents a review on a PR."""
    id: str
    user: User
    state: ReviewState
    body: str = ""
    submitted_at: Optional[float] = None
    is_codeowner: bool = False


@dataclass
class CheckRun:
    """Represents a CI check run."""
    id: str
    name: str
    status: CheckStatus
    conclusion: Optional[str] = None
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    details_url: Optional[str] = None


@dataclass
class PullRequest:
    """Represents a pull request."""
    id: str
    number: int
    title: str
    body: str
    state: PullRequestState
    author: User
    repository: Repository
    source_branch: Branch
    target_branch: Branch
    files_changed: List[FileChange] = field(default_factory=list)
    reviews: List[Review] = field(default_factory=list)
    check_runs: List[CheckRun] = field(default_factory=list)
    labels: List[str] = field(default_factory=list)
    assignees: List[User] = field(default_factory=list)
    reviewers: List[User] = field(default_factory=list)
    created_at: Optional[float] = None
    updated_at: Optional[float] = None
    merged_at: Optional[float] = None
    merge_commit_sha: Optional[str] = None
    url: Optional[str] = None
    is_draft: bool = False
    mergeable: Optional[bool] = None
    mergeable_state: Optional[str] = None

    def get_approvals(self) -> List[Review]:
        """Get all approval reviews."""
        return [r for r in self.reviews if r.state == ReviewState.APPROVED]

    def get_codeowner_approvals(self) -> List[Review]:
        """Get approvals from code owners."""
        return [r for r in self.reviews if r.state == ReviewState.APPROVED and r.is_codeowner]

    def has_changes_requested(self) -> bool:
        """Check if any reviewer requested changes."""
        return any(r.state == ReviewState.CHANGES_REQUESTED for r in self.reviews)


@dataclass
class MergeRequirements:
    """Requirements for merging a PR."""
    can_merge: bool
    status: MergeStatus
    block_reasons: List[MergeBlockReason] = field(default_factory=list)
    required_approvals: int = 1
    current_approvals: int = 0
    required_codeowner_approval: bool = True
    has_codeowner_approval: bool = False
    ci_status: CheckStatus = CheckStatus.QUEUED
    is_up_to_date: bool = True
    has_conflicts: bool = False
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ReviewComment:
    """A comment on a PR review."""
    id: str
    body: str
    path: Optional[str] = None
    line: Optional[int] = None
    user: Optional[User] = None
    created_at: Optional[float] = None


# =============================================================================
# PLATFORM CLIENTS
# =============================================================================

class PlatformClient(ABC):
    """Abstract base class for platform clients."""

    @abstractmethod
    async def get_pull_request(
        self,
        repo: Repository,
        pr_number: int,
    ) -> Optional[PullRequest]:
        """Get a pull request by number."""
        pass

    @abstractmethod
    async def list_pull_requests(
        self,
        repo: Repository,
        state: PullRequestState = PullRequestState.OPEN,
    ) -> List[PullRequest]:
        """List pull requests for a repository."""
        pass

    @abstractmethod
    async def create_pull_request(
        self,
        repo: Repository,
        title: str,
        body: str,
        source_branch: str,
        target_branch: str,
    ) -> Optional[PullRequest]:
        """Create a new pull request."""
        pass

    @abstractmethod
    async def request_reviewers(
        self,
        pr: PullRequest,
        reviewers: List[str],
    ) -> bool:
        """Request reviewers for a PR."""
        pass

    @abstractmethod
    async def submit_review(
        self,
        pr: PullRequest,
        state: ReviewState,
        body: str,
        comments: Optional[List[ReviewComment]] = None,
    ) -> Optional[Review]:
        """Submit a review on a PR."""
        pass

    @abstractmethod
    async def get_check_runs(
        self,
        pr: PullRequest,
    ) -> List[CheckRun]:
        """Get CI check runs for a PR."""
        pass

    @abstractmethod
    async def merge_pull_request(
        self,
        pr: PullRequest,
        merge_method: str = "merge",
        commit_message: Optional[str] = None,
    ) -> bool:
        """Merge a pull request."""
        pass


class GitHubClient(PlatformClient):
    """GitHub API client."""

    def __init__(self, config: ReviewWorkflowConfig):
        self.config = config
        self._session = None
        self._headers = {
            "Authorization": f"Bearer {config.api_token}",
            "Accept": "application/vnd.github.v3+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def _request(
        self,
        method: str,
        endpoint: str,
        data: Optional[Dict] = None,
    ) -> Optional[Dict]:
        """Make an API request."""
        try:
            import aiohttp

            url = f"{self.config.api_url}{endpoint}"

            async with aiohttp.ClientSession() as session:
                async with session.request(
                    method,
                    url,
                    headers=self._headers,
                    json=data,
                ) as response:
                    if response.status >= 400:
                        error_text = await response.text()
                        logger.error(f"GitHub API error: {response.status} - {error_text}")
                        return None
                    return await response.json()

        except ImportError:
            logger.error("aiohttp not installed - GitHub client unavailable")
            return None
        except Exception as e:
            logger.error(f"GitHub API request failed: {e}")
            return None

    async def get_pull_request(
        self,
        repo: Repository,
        pr_number: int,
    ) -> Optional[PullRequest]:
        """Get a pull request by number."""
        data = await self._request("GET", f"/repos/{repo.full_name}/pulls/{pr_number}")
        if not data:
            return None

        # Get additional data
        files_data = await self._request("GET", f"/repos/{repo.full_name}/pulls/{pr_number}/files")
        reviews_data = await self._request("GET", f"/repos/{repo.full_name}/pulls/{pr_number}/reviews")

        return self._parse_pull_request(data, repo, files_data or [], reviews_data or [])

    async def list_pull_requests(
        self,
        repo: Repository,
        state: PullRequestState = PullRequestState.OPEN,
    ) -> List[PullRequest]:
        """List pull requests for a repository."""
        state_param = state.value if state != PullRequestState.MERGED else "closed"
        data = await self._request(
            "GET",
            f"/repos/{repo.full_name}/pulls?state={state_param}&per_page=100"
        )
        if not data:
            return []

        return [self._parse_pull_request(pr_data, repo) for pr_data in data]

    async def create_pull_request(
        self,
        repo: Repository,
        title: str,
        body: str,
        source_branch: str,
        target_branch: str,
    ) -> Optional[PullRequest]:
        """Create a new pull request."""
        data = await self._request(
            "POST",
            f"/repos/{repo.full_name}/pulls",
            {
                "title": title,
                "body": body,
                "head": source_branch,
                "base": target_branch,
            }
        )
        if not data:
            return None

        return self._parse_pull_request(data, repo)

    async def request_reviewers(
        self,
        pr: PullRequest,
        reviewers: List[str],
    ) -> bool:
        """Request reviewers for a PR."""
        result = await self._request(
            "POST",
            f"/repos/{pr.repository.full_name}/pulls/{pr.number}/requested_reviewers",
            {"reviewers": reviewers}
        )
        return result is not None

    async def submit_review(
        self,
        pr: PullRequest,
        state: ReviewState,
        body: str,
        comments: Optional[List[ReviewComment]] = None,
    ) -> Optional[Review]:
        """Submit a review on a PR."""
        event_map = {
            ReviewState.APPROVED: "APPROVE",
            ReviewState.CHANGES_REQUESTED: "REQUEST_CHANGES",
            ReviewState.COMMENTED: "COMMENT",
        }

        data = {
            "body": body,
            "event": event_map.get(state, "COMMENT"),
        }

        if comments:
            data["comments"] = [
                {"path": c.path, "line": c.line, "body": c.body}
                for c in comments if c.path and c.line
            ]

        result = await self._request(
            "POST",
            f"/repos/{pr.repository.full_name}/pulls/{pr.number}/reviews",
            data
        )

        if not result:
            return None

        return Review(
            id=str(result.get("id")),
            user=User(
                id=str(result.get("user", {}).get("id")),
                username=result.get("user", {}).get("login", ""),
            ),
            state=state,
            body=body,
            submitted_at=time.time(),
        )

    async def get_check_runs(
        self,
        pr: PullRequest,
    ) -> List[CheckRun]:
        """Get CI check runs for a PR."""
        data = await self._request(
            "GET",
            f"/repos/{pr.repository.full_name}/commits/{pr.source_branch.sha}/check-runs"
        )
        if not data:
            return []

        runs = []
        for run_data in data.get("check_runs", []):
            status_map = {
                "queued": CheckStatus.QUEUED,
                "in_progress": CheckStatus.IN_PROGRESS,
                "completed": CheckStatus.SUCCESS,
            }

            if run_data.get("conclusion") == "failure":
                status = CheckStatus.FAILURE
            elif run_data.get("conclusion") == "cancelled":
                status = CheckStatus.CANCELLED
            else:
                status = status_map.get(run_data.get("status", ""), CheckStatus.PENDING)

            runs.append(CheckRun(
                id=str(run_data.get("id")),
                name=run_data.get("name", ""),
                status=status,
                conclusion=run_data.get("conclusion"),
                details_url=run_data.get("details_url"),
            ))

        return runs

    async def merge_pull_request(
        self,
        pr: PullRequest,
        merge_method: str = "merge",
        commit_message: Optional[str] = None,
    ) -> bool:
        """Merge a pull request."""
        data = {"merge_method": merge_method}
        if commit_message:
            data["commit_message"] = commit_message

        result = await self._request(
            "PUT",
            f"/repos/{pr.repository.full_name}/pulls/{pr.number}/merge",
            data
        )
        return result is not None and result.get("merged", False)

    def _parse_pull_request(
        self,
        data: Dict,
        repo: Repository,
        files_data: Optional[List] = None,
        reviews_data: Optional[List] = None,
    ) -> PullRequest:
        """Parse pull request data from GitHub API."""
        # Parse author
        author = User(
            id=str(data.get("user", {}).get("id")),
            username=data.get("user", {}).get("login", ""),
            avatar_url=data.get("user", {}).get("avatar_url"),
        )

        # Parse state
        state_map = {
            "open": PullRequestState.OPEN,
            "closed": PullRequestState.CLOSED,
        }
        state = state_map.get(data.get("state", ""), PullRequestState.OPEN)
        if data.get("merged"):
            state = PullRequestState.MERGED
        if data.get("draft"):
            state = PullRequestState.DRAFT

        # Parse branches
        source_branch = Branch(
            name=data.get("head", {}).get("ref", ""),
            sha=data.get("head", {}).get("sha", ""),
        )
        target_branch = Branch(
            name=data.get("base", {}).get("ref", ""),
            sha=data.get("base", {}).get("sha", ""),
            is_protected=data.get("base", {}).get("ref") in self.config.protected_branches,
        )

        # Parse files
        files_changed = []
        for file_data in (files_data or []):
            files_changed.append(FileChange(
                path=file_data.get("filename", ""),
                status=file_data.get("status", ""),
                additions=file_data.get("additions", 0),
                deletions=file_data.get("deletions", 0),
                previous_path=file_data.get("previous_filename"),
            ))

        # Parse reviews
        reviews = []
        for review_data in (reviews_data or []):
            state_map = {
                "APPROVED": ReviewState.APPROVED,
                "CHANGES_REQUESTED": ReviewState.CHANGES_REQUESTED,
                "COMMENTED": ReviewState.COMMENTED,
                "PENDING": ReviewState.PENDING,
                "DISMISSED": ReviewState.DISMISSED,
            }
            reviews.append(Review(
                id=str(review_data.get("id")),
                user=User(
                    id=str(review_data.get("user", {}).get("id")),
                    username=review_data.get("user", {}).get("login", ""),
                ),
                state=state_map.get(review_data.get("state", ""), ReviewState.PENDING),
                body=review_data.get("body", ""),
            ))

        return PullRequest(
            id=str(data.get("id")),
            number=data.get("number", 0),
            title=data.get("title", ""),
            body=data.get("body", ""),
            state=state,
            author=author,
            repository=repo,
            source_branch=source_branch,
            target_branch=target_branch,
            files_changed=files_changed,
            reviews=reviews,
            labels=[l.get("name") for l in data.get("labels", [])],
            is_draft=data.get("draft", False),
            mergeable=data.get("mergeable"),
            mergeable_state=data.get("mergeable_state"),
            url=data.get("html_url"),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
        )


class GitLabClient(PlatformClient):
    """GitLab API client."""

    def __init__(self, config: ReviewWorkflowConfig):
        self.config = config
        self._headers = {
            "PRIVATE-TOKEN": config.api_token,
        }

    async def _request(
        self,
        method: str,
        endpoint: str,
        data: Optional[Dict] = None,
    ) -> Optional[Dict]:
        """Make an API request."""
        try:
            import aiohttp

            url = f"{self.config.api_url}{endpoint}"

            async with aiohttp.ClientSession() as session:
                async with session.request(
                    method,
                    url,
                    headers=self._headers,
                    json=data,
                ) as response:
                    if response.status >= 400:
                        return None
                    return await response.json()

        except ImportError:
            logger.error("aiohttp not installed - GitLab client unavailable")
            return None
        except Exception as e:
            logger.error(f"GitLab API request failed: {e}")
            return None

    async def get_pull_request(
        self,
        repo: Repository,
        pr_number: int,
    ) -> Optional[PullRequest]:
        """Get a merge request by IID."""
        # GitLab uses project ID and MR IID
        data = await self._request(
            "GET",
            f"/projects/{repo.id}/merge_requests/{pr_number}"
        )
        if not data:
            return None
        return self._parse_merge_request(data, repo)

    async def list_pull_requests(
        self,
        repo: Repository,
        state: PullRequestState = PullRequestState.OPEN,
    ) -> List[PullRequest]:
        """List merge requests for a project."""
        state_map = {
            PullRequestState.OPEN: "opened",
            PullRequestState.CLOSED: "closed",
            PullRequestState.MERGED: "merged",
        }
        state_param = state_map.get(state, "opened")

        data = await self._request(
            "GET",
            f"/projects/{repo.id}/merge_requests?state={state_param}"
        )
        if not data:
            return []

        return [self._parse_merge_request(mr_data, repo) for mr_data in data]

    async def create_pull_request(
        self,
        repo: Repository,
        title: str,
        body: str,
        source_branch: str,
        target_branch: str,
    ) -> Optional[PullRequest]:
        """Create a new merge request."""
        data = await self._request(
            "POST",
            f"/projects/{repo.id}/merge_requests",
            {
                "title": title,
                "description": body,
                "source_branch": source_branch,
                "target_branch": target_branch,
            }
        )
        if not data:
            return None
        return self._parse_merge_request(data, repo)

    async def request_reviewers(
        self,
        pr: PullRequest,
        reviewers: List[str],
    ) -> bool:
        """Request reviewers for an MR."""
        # GitLab requires user IDs, not usernames
        # This would need a user lookup first
        logger.warning("GitLab reviewer assignment requires user ID lookup")
        return False

    async def submit_review(
        self,
        pr: PullRequest,
        state: ReviewState,
        body: str,
        comments: Optional[List[ReviewComment]] = None,
    ) -> Optional[Review]:
        """Submit a review (note) on an MR."""
        # Add a note
        result = await self._request(
            "POST",
            f"/projects/{pr.repository.id}/merge_requests/{pr.number}/notes",
            {"body": body}
        )

        if not result:
            return None

        # If approving, also approve the MR
        if state == ReviewState.APPROVED:
            await self._request(
                "POST",
                f"/projects/{pr.repository.id}/merge_requests/{pr.number}/approve"
            )

        return Review(
            id=str(result.get("id")),
            user=User(
                id=str(result.get("author", {}).get("id")),
                username=result.get("author", {}).get("username", ""),
            ),
            state=state,
            body=body,
            submitted_at=time.time(),
        )

    async def get_check_runs(
        self,
        pr: PullRequest,
    ) -> List[CheckRun]:
        """Get CI pipeline jobs for an MR."""
        data = await self._request(
            "GET",
            f"/projects/{pr.repository.id}/merge_requests/{pr.number}/pipelines"
        )
        if not data:
            return []

        runs = []
        for pipeline in data:
            status_map = {
                "pending": CheckStatus.QUEUED,
                "running": CheckStatus.IN_PROGRESS,
                "success": CheckStatus.SUCCESS,
                "failed": CheckStatus.FAILURE,
                "canceled": CheckStatus.CANCELLED,
                "skipped": CheckStatus.SKIPPED,
            }
            runs.append(CheckRun(
                id=str(pipeline.get("id")),
                name=f"Pipeline #{pipeline.get('id')}",
                status=status_map.get(pipeline.get("status", ""), CheckStatus.PENDING),
                details_url=pipeline.get("web_url"),
            ))

        return runs

    async def merge_pull_request(
        self,
        pr: PullRequest,
        merge_method: str = "merge",
        commit_message: Optional[str] = None,
    ) -> bool:
        """Merge a merge request."""
        data = {}
        if commit_message:
            data["merge_commit_message"] = commit_message
        if merge_method == "squash":
            data["squash"] = True

        result = await self._request(
            "PUT",
            f"/projects/{pr.repository.id}/merge_requests/{pr.number}/merge",
            data
        )
        return result is not None

    def _parse_merge_request(
        self,
        data: Dict,
        repo: Repository,
    ) -> PullRequest:
        """Parse merge request data from GitLab API."""
        author = User(
            id=str(data.get("author", {}).get("id")),
            username=data.get("author", {}).get("username", ""),
            name=data.get("author", {}).get("name"),
        )

        state_map = {
            "opened": PullRequestState.OPEN,
            "closed": PullRequestState.CLOSED,
            "merged": PullRequestState.MERGED,
        }

        return PullRequest(
            id=str(data.get("id")),
            number=data.get("iid", 0),
            title=data.get("title", ""),
            body=data.get("description", ""),
            state=state_map.get(data.get("state", ""), PullRequestState.OPEN),
            author=author,
            repository=repo,
            source_branch=Branch(
                name=data.get("source_branch", ""),
                sha=data.get("sha", ""),
            ),
            target_branch=Branch(
                name=data.get("target_branch", ""),
                sha="",
                is_protected=data.get("target_branch") in self.config.protected_branches,
            ),
            url=data.get("web_url"),
            is_draft=data.get("work_in_progress", False),
        )


# =============================================================================
# REVIEW ENGINE
# =============================================================================

class ReviewEngine:
    """
    Core review engine that evaluates PRs and manages review workflows.

    Integrates with:
    - Code ownership for reviewer assignment
    - CI/CD for status checks
    - Platform API for review operations
    """

    def __init__(
        self,
        config: ReviewWorkflowConfig,
        client: PlatformClient,
    ):
        self.config = config
        self.client = client

    async def evaluate_merge_requirements(
        self,
        pr: PullRequest,
        codeowners: Optional[List[str]] = None,
    ) -> MergeRequirements:
        """Evaluate if a PR meets all merge requirements."""
        block_reasons = []
        details = {}

        # Check draft status
        if pr.is_draft or pr.state == PullRequestState.DRAFT:
            block_reasons.append(MergeBlockReason.DRAFT_PR)

        # Check for merge conflicts
        if pr.mergeable is False or pr.mergeable_state == "dirty":
            block_reasons.append(MergeBlockReason.MERGE_CONFLICTS)

        # Count approvals
        approvals = pr.get_approvals()
        current_approvals = len(approvals)
        details["approvals"] = [r.user.username for r in approvals]

        if current_approvals < self.config.required_approvals:
            block_reasons.append(MergeBlockReason.INSUFFICIENT_APPROVALS)

        # Check for changes requested
        if pr.has_changes_requested():
            block_reasons.append(MergeBlockReason.CHANGES_REQUESTED)

        # Check codeowner approvals
        has_codeowner_approval = False
        if self.config.require_codeowner_approval and codeowners:
            codeowner_approvals = [
                r for r in approvals
                if r.user.username in codeowners
            ]
            has_codeowner_approval = len(codeowner_approvals) > 0
            if not has_codeowner_approval:
                block_reasons.append(MergeBlockReason.CODEOWNER_APPROVAL_REQUIRED)
            details["codeowner_approvals"] = [r.user.username for r in codeowner_approvals]

        # Check CI status
        check_runs = pr.check_runs or await self.client.get_check_runs(pr)
        ci_status = self._aggregate_check_status(check_runs)
        details["ci_checks"] = [{"name": c.name, "status": c.status.value} for c in check_runs]

        if self.config.require_ci_pass:
            if ci_status == CheckStatus.FAILURE:
                block_reasons.append(MergeBlockReason.CI_FAILED)
            elif ci_status in (CheckStatus.QUEUED, CheckStatus.IN_PROGRESS):
                block_reasons.append(MergeBlockReason.CI_PENDING)

        # Check if branch is up to date
        is_up_to_date = pr.mergeable_state != "behind"
        if self.config.require_branch_up_to_date and not is_up_to_date:
            block_reasons.append(MergeBlockReason.BRANCH_OUT_OF_DATE)

        # Determine overall status
        if not block_reasons:
            status = MergeStatus.READY
            can_merge = True
        elif MergeBlockReason.MERGE_CONFLICTS in block_reasons:
            status = MergeStatus.CONFLICTED
            can_merge = False
        elif any(r in block_reasons for r in [
            MergeBlockReason.CI_PENDING,
            MergeBlockReason.REVIEW_REQUIRED,
        ]):
            status = MergeStatus.PENDING
            can_merge = False
        else:
            status = MergeStatus.BLOCKED
            can_merge = False

        return MergeRequirements(
            can_merge=can_merge,
            status=status,
            block_reasons=block_reasons,
            required_approvals=self.config.required_approvals,
            current_approvals=current_approvals,
            required_codeowner_approval=self.config.require_codeowner_approval,
            has_codeowner_approval=has_codeowner_approval,
            ci_status=ci_status,
            is_up_to_date=is_up_to_date,
            has_conflicts=pr.mergeable is False,
            details=details,
        )

    def _aggregate_check_status(self, check_runs: List[CheckRun]) -> CheckStatus:
        """Aggregate check run statuses into a single status."""
        if not check_runs:
            return CheckStatus.SUCCESS

        statuses = [c.status for c in check_runs]

        if any(s == CheckStatus.FAILURE for s in statuses):
            return CheckStatus.FAILURE
        if any(s == CheckStatus.IN_PROGRESS for s in statuses):
            return CheckStatus.IN_PROGRESS
        if any(s == CheckStatus.QUEUED for s in statuses):
            return CheckStatus.QUEUED
        if all(s in (CheckStatus.SUCCESS, CheckStatus.SKIPPED) for s in statuses):
            return CheckStatus.SUCCESS

        return CheckStatus.PENDING

    async def get_suggested_reviewers(
        self,
        pr: PullRequest,
        codeowners: Optional[Dict[str, List[str]]] = None,
        max_reviewers: int = 3,
    ) -> List[str]:
        """Get suggested reviewers for a PR based on changed files."""
        suggested = set()

        if codeowners:
            for file_change in pr.files_changed:
                owners = codeowners.get(file_change.path, [])
                suggested.update(owners)

        # Don't suggest the PR author
        suggested.discard(pr.author.username)

        return list(suggested)[:max_reviewers]

    async def auto_assign_reviewers(
        self,
        pr: PullRequest,
        codeowners: Optional[Dict[str, List[str]]] = None,
    ) -> bool:
        """Automatically assign reviewers to a PR."""
        if not self.config.auto_assign_reviewers:
            return False

        suggested = await self.get_suggested_reviewers(pr, codeowners)
        if not suggested:
            return False

        return await self.client.request_reviewers(pr, suggested)


# =============================================================================
# REVIEW WORKFLOW ENGINE
# =============================================================================

class ReviewWorkflowEngine:
    """
    Main review workflow engine coordinating all components.

    Provides high-level API for:
    - PR lifecycle management
    - Review workflow automation
    - Merge requirement enforcement
    """

    def __init__(self, config: Optional[ReviewWorkflowConfig] = None):
        self.config = config or ReviewWorkflowConfig.from_env()

        # Initialize platform client
        if self.config.platform == "github":
            self.client = GitHubClient(self.config)
        elif self.config.platform == "gitlab":
            self.client = GitLabClient(self.config)
        else:
            raise ValueError(f"Unsupported platform: {self.config.platform}")

        self.review_engine = ReviewEngine(self.config, self.client)
        self._running = False
        self._event_handlers: Dict[str, List[Callable]] = {}

    async def initialize(self) -> bool:
        """Initialize the workflow engine."""
        try:
            self._running = True
            logger.info("Review workflow engine initialized")
            return True
        except Exception as e:
            logger.error(f"Failed to initialize review workflow: {e}")
            return False

    async def shutdown(self) -> None:
        """Shutdown the workflow engine."""
        self._running = False
        logger.info("Review workflow engine shutdown")

    async def create_pull_request(
        self,
        repo_owner: str,
        repo_name: str,
        title: str,
        body: str,
        source_branch: str,
        target_branch: str = "main",
        auto_assign: bool = True,
        codeowners: Optional[Dict[str, List[str]]] = None,
    ) -> Optional[PullRequest]:
        """
        Create a new pull request with automatic reviewer assignment.
        """
        repo = Repository(
            id="",
            owner=repo_owner,
            name=repo_name,
            full_name=f"{repo_owner}/{repo_name}",
            default_branch=target_branch,
        )

        pr = await self.client.create_pull_request(
            repo, title, body, source_branch, target_branch
        )

        if pr and auto_assign and codeowners:
            await self.review_engine.auto_assign_reviewers(pr, codeowners)

        return pr

    async def get_pull_request(
        self,
        repo_owner: str,
        repo_name: str,
        pr_number: int,
    ) -> Optional[PullRequest]:
        """Get a pull request."""
        repo = Repository(
            id="",
            owner=repo_owner,
            name=repo_name,
            full_name=f"{repo_owner}/{repo_name}",
        )

        return await self.client.get_pull_request(repo, pr_number)

    async def check_merge_readiness(
        self,
        pr: PullRequest,
        codeowners: Optional[List[str]] = None,
    ) -> MergeRequirements:
        """Check if a PR is ready to merge."""
        return await self.review_engine.evaluate_merge_requirements(pr, codeowners)

    async def submit_review(
        self,
        pr: PullRequest,
        state: ReviewState,
        body: str,
        comments: Optional[List[ReviewComment]] = None,
    ) -> Optional[Review]:
        """Submit a review on a PR."""
        return await self.client.submit_review(pr, state, body, comments)

    async def approve(
        self,
        pr: PullRequest,
        body: str = "LGTM!",
    ) -> Optional[Review]:
        """Approve a PR."""
        return await self.submit_review(pr, ReviewState.APPROVED, body)

    async def request_changes(
        self,
        pr: PullRequest,
        body: str,
        comments: Optional[List[ReviewComment]] = None,
    ) -> Optional[Review]:
        """Request changes on a PR."""
        return await self.submit_review(pr, ReviewState.CHANGES_REQUESTED, body, comments)

    async def merge(
        self,
        pr: PullRequest,
        merge_method: str = "merge",
        commit_message: Optional[str] = None,
        force: bool = False,
    ) -> Tuple[bool, Optional[str]]:
        """
        Merge a PR if requirements are met.

        Returns (success, error_message) tuple.
        """
        if not force:
            requirements = await self.check_merge_readiness(pr)
            if not requirements.can_merge:
                reasons = ", ".join(r.value for r in requirements.block_reasons)
                return False, f"Cannot merge: {reasons}"

        success = await self.client.merge_pull_request(pr, merge_method, commit_message)

        if success:
            return True, None
        return False, "Merge failed"

    def on_event(self, event_type: str, handler: Callable) -> None:
        """Register an event handler."""
        if event_type not in self._event_handlers:
            self._event_handlers[event_type] = []
        self._event_handlers[event_type].append(handler)

    def get_stats(self) -> Dict[str, Any]:
        """Get workflow engine statistics."""
        return {
            "running": self._running,
            "platform": self.config.platform,
            "required_approvals": self.config.required_approvals,
        }


# =============================================================================
# CROSS-REPO REVIEW COORDINATOR
# =============================================================================

class CrossRepoReviewCoordinator:
    """
    Coordinates reviews across JARVIS, JARVIS-Prime, and Reactor-Core.

    Enables:
    - Cross-repo PR creation
    - Coordinated reviews
    - Dependency tracking between PRs
    """

    def __init__(self, config: Optional[ReviewWorkflowConfig] = None):
        self.config = config or ReviewWorkflowConfig.from_env()
        self._engines: Dict[str, ReviewWorkflowEngine] = {}
        self._pr_dependencies: Dict[str, Set[str]] = {}  # pr_id -> dependent_pr_ids
        self._running = False

    async def initialize(self) -> bool:
        """Initialize cross-repo review."""
        if not self.config.enable_cross_repo:
            return True

        try:
            self._running = True
            logger.info("Cross-repo review coordinator initialized")
            return True
        except Exception as e:
            logger.error(f"Failed to initialize cross-repo review: {e}")
            return False

    async def create_coordinated_prs(
        self,
        changes: Dict[str, Dict[str, Any]],  # repo_id -> {title, body, branch, files}
    ) -> Dict[str, Optional[PullRequest]]:
        """Create coordinated PRs across multiple repositories."""
        results = {}

        for repo_id, change in changes.items():
            engine = self._get_or_create_engine(repo_id)

            pr = await engine.create_pull_request(
                repo_owner=change.get("owner", ""),
                repo_name=change.get("name", ""),
                title=change.get("title", ""),
                body=change.get("body", ""),
                source_branch=change.get("branch", ""),
                target_branch=change.get("target", "main"),
            )

            results[repo_id] = pr

        # Track dependencies
        pr_ids = [pr.id for pr in results.values() if pr]
        for pr_id in pr_ids:
            self._pr_dependencies[pr_id] = set(pr_ids) - {pr_id}

        return results

    def _get_or_create_engine(self, repo_id: str) -> ReviewWorkflowEngine:
        """Get or create a review engine for a repository."""
        if repo_id not in self._engines:
            self._engines[repo_id] = ReviewWorkflowEngine(self.config)
        return self._engines[repo_id]

    async def shutdown(self) -> None:
        """Shutdown cross-repo review."""
        for engine in self._engines.values():
            await engine.shutdown()
        self._running = False


# =============================================================================
# GLOBAL INSTANCES
# =============================================================================

_review_workflow_engine: Optional[ReviewWorkflowEngine] = None
_cross_repo_coordinator: Optional[CrossRepoReviewCoordinator] = None


def get_review_workflow_engine(
    config: Optional[ReviewWorkflowConfig] = None
) -> ReviewWorkflowEngine:
    """
    Get or create the global review workflow engine.

    Args:
        config: Optional configuration. If provided and engine doesn't exist,
               uses this config. If engine exists, config is ignored.

    Returns:
        The global ReviewWorkflowEngine instance.
    """
    global _review_workflow_engine
    if _review_workflow_engine is None:
        _review_workflow_engine = ReviewWorkflowEngine(config=config)
    return _review_workflow_engine


def get_cross_repo_review_coordinator() -> CrossRepoReviewCoordinator:
    """Get the global cross-repo review coordinator."""
    global _cross_repo_coordinator
    if _cross_repo_coordinator is None:
        _cross_repo_coordinator = CrossRepoReviewCoordinator()
    return _cross_repo_coordinator


async def initialize_review_workflow() -> bool:
    """Initialize review workflow system."""
    engine = get_review_workflow_engine()
    success = await engine.initialize()

    if success:
        coordinator = get_cross_repo_review_coordinator()
        await coordinator.initialize()

    return success


async def shutdown_review_workflow() -> None:
    """Shutdown review workflow system."""
    global _review_workflow_engine, _cross_repo_coordinator

    if _cross_repo_coordinator:
        await _cross_repo_coordinator.shutdown()
        _cross_repo_coordinator = None

    if _review_workflow_engine:
        await _review_workflow_engine.shutdown()
        _review_workflow_engine = None
