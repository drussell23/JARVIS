"""
GitHubIssueSensor — Proactive issue discovery and auto-resolution across Trinity repos.

Polls GitHub Issues API for open issues across JARVIS, J-Prime, and Reactor Core
repositories. Classifies issues by label and content, emits IntentEnvelopes for
issues that Ouroboros can resolve autonomously (bug fixes, test failures,
dependency updates, documentation gaps).

Boundary Principle:
  Deterministic: gh CLI invocation (argv-based, no shell), JSON parsing,
  label classification, deduplication by issue number, staleness detection.
  Agentic: Fix generation and PR creation routed through Ouroboros pipeline.

Requires: gh CLI authenticated (gh auth login).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from backend.core.ouroboros.governance.intake.intent_envelope import make_envelope

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_POLL_INTERVAL_S = float(
    os.environ.get("JARVIS_GITHUB_ISSUE_INTERVAL_S", "3600")
)
_MAX_ISSUES_PER_SCAN = int(
    os.environ.get("JARVIS_GITHUB_ISSUE_MAX_PER_SCAN", "10")
)
# Circuit breaker: skip scans for N seconds after consecutive failures.
# Battle test bt-2026-04-13-031119 hit GraphQL TLS cert failures on every
# scan and burned log volume without any payoff. Tripping at 3 failures
# gives transient network hiccups two free retries, then cools off.
_BREAKER_THRESHOLD = int(
    os.environ.get("JARVIS_GITHUB_BREAKER_THRESHOLD", "3")
)
_BREAKER_COOLDOWN_S = float(
    os.environ.get("JARVIS_GITHUB_BREAKER_COOLDOWN_S", "600")
)
# Error substrings that trip the breaker (TLS/DNS/network, not "issue
# not found"). Case-insensitive match on stderr/exception message.
_BREAKER_TRIGGERS: Tuple[str, ...] = (
    "certificate", "tls", "ssl", "x509",
    "network is unreachable", "temporary failure in name resolution",
    "getaddrinfo", "connection refused", "connection reset",
)

# ---------------------------------------------------------------------------
# Per-issue exhaustion cooldown registry (bt-2026-04-15 findings)
# ---------------------------------------------------------------------------
#
# When an op sourced from a github_issue exhausts its providers, we mark the
# issue as "recently exhausted" and suppress emission on subsequent scans
# until the cooldown window closes. Prevents a chronic unresolvable issue
# (e.g. #16501 "Unlock Test Suite Failed" in bt-2026-04-15-012736 and
# bt-2026-04-15-013455) from single-handedly driving the organism toward
# hibernation — 3 chronic-noise exhaustions trip ProviderExhaustionWatcher's
# global counter even when the reflex path is healthy.
#
# State is module-level (not instance-level) so any caller — CandidateGenerator
# on exhaustion, orchestrator POSTMORTEM handler, tests — can register a
# cooldown without needing a handle to the sensor instance. Process-scoped:
# cleared on restart. Env-gated for full reversibility.
#
# Env gate: ``JARVIS_GITHUB_ISSUE_EXHAUSTION_COOLDOWN_S`` (default 900s).
# Set to 0 or a negative value to disable the registry entirely.

_ISSUE_EXHAUSTION_COOLDOWN_S: float = float(
    os.environ.get("JARVIS_GITHUB_ISSUE_EXHAUSTION_COOLDOWN_S", "900")
)

# issue_key -> monotonic deadline after which the cooldown is released
_issue_exhaustion_cooldowns: Dict[str, float] = {}


def register_issue_exhaustion(issue_key: str, reason: str = "") -> None:
    """Record that an op sourced from ``issue_key`` exhausted its providers.

    Next scan will suppress emission of ``issue_key`` until the cooldown
    window closes. No-op when ``JARVIS_GITHUB_ISSUE_EXHAUSTION_COOLDOWN_S``
    is 0 or negative.

    Parameters
    ----------
    issue_key:
        The dedup key used by the sensor, format ``"{repo}:{issue_number}"``.
        Must match exactly what ``scan_once`` computes on line
        ``dedup_key = f"{finding.repo}:{finding.issue_number}"``.
    reason:
        Short free-text explanation for logs (e.g. the exhaustion cause).
        Not used for matching — diagnostic only.
    """
    if _ISSUE_EXHAUSTION_COOLDOWN_S <= 0:
        return  # disabled
    if not issue_key:
        return
    cooldown_until = time.monotonic() + _ISSUE_EXHAUSTION_COOLDOWN_S
    _issue_exhaustion_cooldowns[issue_key] = cooldown_until
    logger.info(
        "[GitHubIssueSensor] Cooldown set for %s (%.0fs): %s",
        issue_key,
        _ISSUE_EXHAUSTION_COOLDOWN_S,
        reason[:120],
    )


def _issue_cooldown_active(issue_key: str) -> bool:
    """Return True when ``issue_key`` is currently within its cooldown window.

    Transparently expires stale entries — if the deadline has passed, the
    entry is removed and the function returns False. Runs in O(1).
    """
    deadline = _issue_exhaustion_cooldowns.get(issue_key)
    if deadline is None:
        return False
    if time.monotonic() >= deadline:
        _issue_exhaustion_cooldowns.pop(issue_key, None)
        return False
    return True


def clear_issue_cooldowns() -> None:
    """Clear the entire cooldown registry. Intended for tests only."""
    _issue_exhaustion_cooldowns.clear()


# Trinity repository mapping
_TRINITY_REPOS: Tuple[Tuple[str, str, str], ...] = (
    ("jarvis", "drussell23/JARVIS", "backend/"),
    ("jarvis-prime", "drussell23/JARVIS-Prime", "reasoning/"),
    ("reactor", "drussell23/JARVIS-Reactor", "backend/training/"),
)


# Regex for parsing the sensor's own emission format so external callers
# (e.g. CandidateGenerator's exhaustion hook) can recover the sensor's
# internal dedup_key from an op's ``description``. Must stay in lockstep
# with the format string at the envelope construction site in scan_once:
#     f"GitHub Issue #{finding.issue_number} in {finding.repo_full}: {finding.title}"
_GITHUB_ISSUE_DESCRIPTION_RE = re.compile(r"^GitHub Issue #(\d+) in ([^:]+):")


def issue_key_from_description(description: str) -> Optional[str]:
    """Recover the sensor's dedup_key from a github_issue op description.

    Parses the canonical emission format produced by ``scan_once`` and
    maps the full repo slug (``drussell23/JARVIS``) back to the short
    repo name (``jarvis``) via ``_TRINITY_REPOS`` so the returned key is
    byte-identical to what ``scan_once`` computes on line::

        dedup_key = f"{finding.repo}:{finding.issue_number}"

    Required for the CandidateGenerator → ``register_issue_exhaustion``
    hook: the caller has the op's ``description`` (via ``OperationContext``)
    but NOT its ``evidence`` dict, and the sensor-side registry dedups
    on the short-repo key, not the full slug. Parsing from the description
    keeps evidence-threading out of OperationContext.

    Parameters
    ----------
    description:
        The op description, e.g.
        ``"GitHub Issue #16501 in drussell23/JARVIS: 🚨 Critical: ..."``

    Returns
    -------
    Optional[str]
        The dedup key ``"{short_repo}:{issue_number}"`` when the description
        matches the emission format AND the repo slug maps to a known
        Trinity repo. Returns ``None`` on any mismatch — callers treat that
        as "not a github_issue op" and skip the cooldown registration.
    """
    if not description:
        return None
    m = _GITHUB_ISSUE_DESCRIPTION_RE.match(description)
    if m is None:
        return None
    issue_number = m.group(1)
    repo_full = m.group(2).strip()
    for repo_name, trinity_repo_full, _default_path in _TRINITY_REPOS:
        if trinity_repo_full == repo_full:
            return f"{repo_name}:{issue_number}"
    return None

# Label -> urgency mapping (deterministic)
_LABEL_URGENCY: Dict[str, str] = {
    "critical": "critical",
    "bug": "high",
    "security": "critical",
    "regression": "high",
    "automated-test": "high",
    "dependency": "normal",
    "enhancement": "low",
    "documentation": "low",
}

# Labels that indicate Ouroboros CAN resolve this autonomously
_AUTO_RESOLVABLE_LABELS = frozenset({
    "bug", "automated-test", "dependency", "documentation",
    "test-failure", "regression", "security",
})

# Labels that require human judgment
_HUMAN_REQUIRED_LABELS = frozenset({
    "design", "architecture", "breaking-change", "discussion",
})


@dataclass
class IssueFinding:
    """One GitHub issue detected for potential auto-resolution."""
    repo: str
    repo_full: str
    issue_number: int
    title: str
    labels: Tuple[str, ...]
    urgency: str
    auto_resolvable: bool
    body_excerpt: str
    created_at: str
    url: str
    details: Dict[str, Any] = field(default_factory=dict)


class GitHubIssueSensor:
    """Proactive GitHub issue discovery for the Ouroboros intake layer.

    Polls open issues across all Trinity repositories using the gh CLI.
    Classifies each issue to determine urgency and whether Ouroboros can
    auto-resolve it. Issues flow through the full governance pipeline.

    The organism fixes its own bugs.

    Follows the implicit sensor protocol: start(), stop(), scan_once().
    """

    def __init__(
        self,
        repo: str,
        router: Any,
        poll_interval_s: float = _POLL_INTERVAL_S,
        repos: Optional[Tuple[Tuple[str, str, str], ...]] = None,
    ) -> None:
        self._repo = repo
        self._router = router
        self._poll_interval_s = poll_interval_s
        self._repos = repos or _TRINITY_REPOS
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._seen_issues: set[str] = set()
        # Per-repo circuit breaker state.
        self._breaker_failures: Dict[str, int] = {}
        self._breaker_open_until: Dict[str, float] = {}

    def _breaker_tripped(self, repo_full: str) -> bool:
        deadline = self._breaker_open_until.get(repo_full, 0.0)
        if deadline == 0.0:
            return False
        if time.monotonic() >= deadline:
            self._breaker_open_until.pop(repo_full, None)
            self._breaker_failures[repo_full] = 0
            logger.info(
                "[GitHubIssueSensor] Breaker reset for %s — resuming scans",
                repo_full,
            )
            return False
        return True

    def _breaker_trip(self, repo_full: str, reason: str) -> None:
        self._breaker_failures[repo_full] = (
            self._breaker_failures.get(repo_full, 0) + 1
        )
        if self._breaker_failures[repo_full] >= _BREAKER_THRESHOLD:
            self._breaker_open_until[repo_full] = (
                time.monotonic() + _BREAKER_COOLDOWN_S
            )
            logger.warning(
                "[GitHubIssueSensor] Breaker OPEN for %s (%.0fs cooldown) — "
                "%d consecutive failures, last=%s",
                repo_full,
                _BREAKER_COOLDOWN_S,
                self._breaker_failures[repo_full],
                reason[:120],
            )

    def _breaker_clear(self, repo_full: str) -> None:
        if self._breaker_failures.get(repo_full, 0) > 0:
            self._breaker_failures[repo_full] = 0

    @staticmethod
    def _is_breaker_trigger(text: str) -> bool:
        low = text.lower()
        return any(trig in low for trig in _BREAKER_TRIGGERS)

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(
            self._poll_loop(), name=f"github_issue_sensor_{self._repo}"
        )
        logger.info(
            "[GitHubIssueSensor] Started — monitoring %d repos, poll=%ds",
            len(self._repos), self._poll_interval_s,
        )

    def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()

    async def _poll_loop(self) -> None:
        await asyncio.sleep(120.0)
        while self._running:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("[GitHubIssueSensor] Poll error")
            try:
                await asyncio.sleep(self._poll_interval_s)
            except asyncio.CancelledError:
                break

    async def scan_once(self) -> List[IssueFinding]:
        """Scan all Trinity repos for open issues."""
        all_findings: List[IssueFinding] = []

        for repo_name, repo_full, default_path in self._repos:
            try:
                findings = await self._scan_repo(repo_name, repo_full, default_path)
                all_findings.extend(findings)
            except Exception:
                logger.debug(
                    "[GitHubIssueSensor] Failed to scan %s", repo_full,
                    exc_info=True,
                )

        # Deduplicate recurring issues (e.g., daily "Unlock Test Suite Failed")
        deduplicated = self._deduplicate_recurring(all_findings)

        # Emit envelopes
        emitted = 0
        cooldown_suppressed = 0
        for finding in deduplicated:
            dedup_key = f"{finding.repo}:{finding.issue_number}"
            if dedup_key in self._seen_issues:
                continue
            # Suppress issues currently inside their exhaustion cooldown
            # window. A chronic unresolvable issue would otherwise re-emit
            # on every scan, re-enter generation, re-exhaust, and single-
            # handedly drive ProviderExhaustionWatcher's global counter
            # toward hibernation even when the reflex path is healthy.
            if _issue_cooldown_active(dedup_key):
                cooldown_suppressed += 1
                logger.info(
                    "[GitHubIssueSensor] #%d (%s): suppressed — "
                    "exhaustion cooldown still active",
                    finding.issue_number, finding.repo,
                )
                continue
            self._seen_issues.add(dedup_key)

            try:
                envelope = make_envelope(
                    source="github_issue",
                    description=(
                        f"GitHub Issue #{finding.issue_number} in "
                        f"{finding.repo_full}: {finding.title}"
                    ),
                    target_files=self._infer_target_files(finding),
                    repo=finding.repo,
                    confidence=0.80,
                    urgency=finding.urgency,
                    evidence={
                        "category": "github_issue",
                        "issue_number": finding.issue_number,
                        "repo_full": finding.repo_full,
                        "labels": list(finding.labels),
                        "auto_resolvable": finding.auto_resolvable,
                        "url": finding.url,
                        "body_excerpt": finding.body_excerpt[:300],
                        "recurring": finding.details.get("recurring_count", 1),
                        "sensor": "GitHubIssueSensor",
                    },
                    requires_human_ack=not finding.auto_resolvable,
                )
                result = await self._router.ingest(envelope)
                if result == "enqueued":
                    emitted += 1
                    logger.info(
                        "[GitHubIssueSensor] #%d (%s): %s -> %s "
                        "(auto=%s, urgency=%s)",
                        finding.issue_number, finding.repo,
                        finding.title[:50], result,
                        finding.auto_resolvable, finding.urgency,
                    )
            except Exception:
                logger.debug(
                    "[GitHubIssueSensor] Emit failed for #%d",
                    finding.issue_number,
                )

        if all_findings:
            logger.info(
                "[GitHubIssueSensor] Scan: %d issues, %d deduplicated, "
                "%d cooldown-suppressed, %d emitted",
                len(all_findings),
                len(all_findings) - len(deduplicated),
                cooldown_suppressed,
                emitted,
            )
        return deduplicated

    # ------------------------------------------------------------------
    # Repo scanning (deterministic — gh CLI argv-based, no shell)
    # ------------------------------------------------------------------

    async def _scan_repo(
        self, repo_name: str, repo_full: str, default_path: str,
    ) -> List[IssueFinding]:
        """Scan one repo for open issues via gh CLI."""
        findings = []

        if self._breaker_tripped(repo_full):
            return []

        try:
            proc = await asyncio.create_subprocess_exec(
                "gh", "issue", "list",
                "--repo", repo_full,
                "--state", "open",
                "--limit", str(_MAX_ISSUES_PER_SCAN),
                "--json", "number,title,labels,body,createdAt,url",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=30.0,
            )

            if proc.returncode != 0:
                err_text = stderr.decode(errors="replace")
                logger.warning(
                    "[GitHubIssueSensor] gh error for %s: %s",
                    repo_full, err_text[:200],
                )
                if self._is_breaker_trigger(err_text):
                    self._breaker_trip(repo_full, err_text)
                return []

            issues = json.loads(stdout.decode())
            self._breaker_clear(repo_full)

        except asyncio.TimeoutError:
            logger.warning("[GitHubIssueSensor] gh timeout for %s", repo_full)
            self._breaker_trip(repo_full, "timeout")
            return []
        except json.JSONDecodeError:
            return []

        for issue in issues:
            number = issue.get("number", 0)
            title = issue.get("title", "")
            body = issue.get("body", "") or ""
            created_at = issue.get("createdAt", "")
            url = issue.get("url", "")

            labels_raw = issue.get("labels", [])
            labels = tuple(
                label.get("name", "").lower()
                for label in labels_raw
                if isinstance(label, dict)
            )

            urgency = self._classify_urgency(labels, title)
            auto_resolvable = self._is_auto_resolvable(labels, title, body)

            findings.append(IssueFinding(
                repo=repo_name,
                repo_full=repo_full,
                issue_number=number,
                title=title,
                labels=labels,
                urgency=urgency,
                auto_resolvable=auto_resolvable,
                body_excerpt=body[:500],
                created_at=created_at,
                url=url,
            ))

        return findings

    # ------------------------------------------------------------------
    # Classification (deterministic — label + keyword matching)
    # ------------------------------------------------------------------

    @staticmethod
    def _classify_urgency(labels: Tuple[str, ...], title: str) -> str:
        _URGENCY_RANK = {"critical": 0, "high": 1, "normal": 2, "low": 3}
        best: Optional[str] = None
        best_rank = 99
        for label in labels:
            for pattern, urgency in _LABEL_URGENCY.items():
                if pattern in label:
                    rank = _URGENCY_RANK.get(urgency, 9)
                    if rank < best_rank:
                        best = urgency
                        best_rank = rank

        if best is not None:
            return best

        title_lower = title.lower()
        if any(w in title_lower for w in ("critical", "crash", "security")):
            return "critical"
        if any(w in title_lower for w in ("bug", "error", "fail", "broken")):
            return "high"
        return "normal"

    @staticmethod
    def _is_auto_resolvable(
        labels: Tuple[str, ...], title: str, body: str,
    ) -> bool:
        if any(label in _HUMAN_REQUIRED_LABELS for label in labels):
            return False
        if any(label in _AUTO_RESOLVABLE_LABELS for label in labels):
            return True

        combined = f"{title} {body}".lower()
        if any(w in combined for w in (
            "test failed", "test suite failed", "importerror",
            "modulenotfounderror", "traceback", "assertion error",
            "dependency", "requirements.txt", "deprecat",
        )):
            return True
        if any(w in combined for w in (
            "design", "proposal", "rfc", "discuss", "breaking change",
        )):
            return False
        return False

    @staticmethod
    def _deduplicate_recurring(
        findings: List[IssueFinding],
    ) -> List[IssueFinding]:
        """Group recurring issues by normalized title, keep most recent."""
        groups: Dict[str, List[IssueFinding]] = {}
        for f in findings:
            normalized = re.sub(r'[^\w\s]', '', f.title.lower()).strip()
            normalized = re.sub(r'\d+', '', normalized).strip()
            groups.setdefault(normalized, []).append(f)

        deduplicated = []
        for group in groups.values():
            if len(group) == 1:
                deduplicated.append(group[0])
            else:
                most_recent = max(group, key=lambda f: f.created_at)
                deduplicated.append(IssueFinding(
                    repo=most_recent.repo,
                    repo_full=most_recent.repo_full,
                    issue_number=most_recent.issue_number,
                    title=most_recent.title,
                    labels=most_recent.labels,
                    urgency=most_recent.urgency,
                    auto_resolvable=most_recent.auto_resolvable,
                    body_excerpt=most_recent.body_excerpt,
                    created_at=most_recent.created_at,
                    url=most_recent.url,
                    details={
                        "recurring_count": len(group),
                        "all_issue_numbers": sorted(
                            g.issue_number for g in group
                        ),
                    },
                ))

        return deduplicated

    @staticmethod
    def _infer_target_files(finding: IssueFinding) -> Tuple[str, ...]:
        """Extract file paths from issue body. Deterministic regex."""
        paths = re.findall(
            r'(?:backend|frontend|tests|scripts|docs)/[\w/._-]+\.'
            r'(?:py|ts|js|md|yaml|json)',
            finding.body_excerpt,
        )
        if paths:
            return tuple(paths[:5])

        for repo_name, _, default_path in _TRINITY_REPOS:
            if finding.repo == repo_name:
                return (default_path,)
        return ("backend/",)

    def health(self) -> Dict[str, Any]:
        return {
            "sensor": "GitHubIssueSensor",
            "repo": self._repo,
            "running": self._running,
            "issues_seen": len(self._seen_issues),
            "repos_monitored": len(self._repos),
            "poll_interval_s": self._poll_interval_s,
        }
