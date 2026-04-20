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
from pathlib import Path
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

# --- Webhook-driven mode (gap #4 migration, Slice 1) ----------------------
#
# When ``JARVIS_GITHUB_WEBHOOK_ENABLED=true``, GitHub's native webhook
# delivery becomes the primary event source for issue signals, and the
# poll loop demotes to a fallback cadence (``JARVIS_GITHUB_ISSUE_FALLBACK_INTERVAL_S``,
# default 900s = 15min) whose job is to catch dropped webhooks — not to
# be the dominant path. When the flag is off (default), nothing changes —
# the sensor keeps polling at ``_POLL_INTERVAL_S``.
#
# Manifesto §3 (Asynchronous tendrils, no polling on the hot path). The
# shadow pattern applies: flag defaults off; operators opt in; we prove
# the behavioral match between webhook-delivered and poll-delivered
# envelopes before flipping the default. See ``ingest_webhook`` below.
_GITHUB_FALLBACK_INTERVAL_S = float(
    os.environ.get("JARVIS_GITHUB_ISSUE_FALLBACK_INTERVAL_S", "900")
)


def webhook_enabled() -> bool:
    """Re-read ``JARVIS_GITHUB_WEBHOOK_ENABLED`` at call-time.

    Intentionally not cached — tests monkeypatch the env and the
    ``EventChannelServer._handle_github`` short-circuit path re-checks
    per-request. The check is a string compare + lower(); negligible.
    """
    return os.environ.get(
        "JARVIS_GITHUB_WEBHOOK_ENABLED", "false",
    ).lower() in ("true", "1", "yes")
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
# Per-issue exhaustion cooldown registry (bt-2026-04-15 findings, disk-backed)
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
# Persistence requirement: the sensor already dedups within a single session
# via ``_seen_issues`` (in-memory set cleared on sensor restart). To prevent
# the SAME issue from being re-emitted and re-exhausted in a fresh session
# (the cross-restart chronic pattern observed in the battle test log), the
# cooldown registry must survive process restart. We persist to an atomic
# JSON file under ``.jarvis/github_issue_cooldowns.json`` keyed by
# ``"{repo}:{issue_number}"`` → ``expires_at_unix`` (wall-clock float).
#
# Clock choice: wall-clock (``time.time()``) — NOT ``time.monotonic()``.
# Monotonic timers reset across process restart, so persisted monotonic
# values are garbage after reboot. Wall-clock is not monotonic inside a
# process (NTP can skew it), but for a 15-minute cooldown that risk is
# acceptable. The cost of a skew false-positive is "one extra suppression"
# and the cost of a false-negative is "one extra emission" — both bounded
# and non-destructive.
#
# Env gates (all reversible):
#   ``JARVIS_GITHUB_ISSUE_EXHAUSTION_COOLDOWN_S`` — default 900s. Set to 0
#     or negative to disable the registry entirely. ``register_issue_exhaustion``
#     becomes a no-op and the scan gate always returns False.
#   ``JARVIS_GITHUB_ISSUE_COOLDOWN_PATH`` — override the on-disk registry
#     path for tests. Default: ``{JARVIS_REPO_PATH or '.'}/.jarvis/github_issue_cooldowns.json``.
#
# State is module-level so any caller — CandidateGenerator on exhaustion,
# orchestrator POSTMORTEM handler, tests — can register a cooldown without
# needing a handle to the sensor instance. Disk I/O is lazy: loaded on
# first registry access, written on every ``register_issue_exhaustion``.

_ISSUE_EXHAUSTION_COOLDOWN_S: float = float(
    os.environ.get("JARVIS_GITHUB_ISSUE_EXHAUSTION_COOLDOWN_S", "900")
)

# issue_key -> ``expires_at_unix`` (wall-clock epoch float). Persisted
# to disk between process lifetimes.
_issue_exhaustion_cooldowns: Dict[str, float] = {}
_cooldown_registry_loaded: bool = False
_cooldown_load_warned: bool = False


def _cooldown_registry_path() -> Path:
    """Resolve the on-disk path for the cooldown registry.

    Env overrides:
      * ``JARVIS_GITHUB_ISSUE_COOLDOWN_PATH`` — full path override (tests).
      * ``JARVIS_REPO_PATH`` — repo root, same convention as the rest
        of the governance stack. Default: ``.``.
    """
    explicit = os.environ.get("JARVIS_GITHUB_ISSUE_COOLDOWN_PATH", "").strip()
    if explicit:
        return Path(explicit)
    repo_root = Path(os.environ.get("JARVIS_REPO_PATH", "."))
    return repo_root / ".jarvis" / "github_issue_cooldowns.json"


def _load_cooldown_registry() -> None:
    """Load the persisted cooldown registry from disk into the module dict.

    Idempotent — uses ``_cooldown_registry_loaded`` as a one-shot guard so
    repeated calls are O(1). Prunes entries whose ``expires_at_unix`` has
    already passed at load time. Missing file is treated as an empty
    registry. Corrupt file is logged once and treated as empty.
    """
    global _cooldown_registry_loaded, _cooldown_load_warned
    if _cooldown_registry_loaded:
        return
    _cooldown_registry_loaded = True  # set before I/O so errors don't loop

    path = _cooldown_registry_path()
    if not path.exists():
        return  # fresh start, empty registry

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        if not _cooldown_load_warned:
            logger.warning(
                "[GitHubIssueSensor] Cooldown registry at %s is unreadable "
                "(%s); treating as empty", path, exc,
            )
            _cooldown_load_warned = True
        return

    if not isinstance(raw, dict):
        if not _cooldown_load_warned:
            logger.warning(
                "[GitHubIssueSensor] Cooldown registry at %s is not a dict "
                "(got %s); treating as empty", path, type(raw).__name__,
            )
            _cooldown_load_warned = True
        return

    now = time.time()
    loaded = 0
    pruned = 0
    for key, value in raw.items():
        if not isinstance(key, str):
            continue
        try:
            expires_at = float(value)
        except (TypeError, ValueError):
            continue
        if expires_at <= now:
            pruned += 1
            continue
        _issue_exhaustion_cooldowns[key] = expires_at
        loaded += 1

    if loaded or pruned:
        logger.info(
            "[GitHubIssueSensor] Cooldown registry loaded from %s: "
            "%d active, %d pruned", path, loaded, pruned,
        )


def _save_cooldown_registry() -> None:
    """Atomically persist the in-memory registry to disk.

    Uses tempfile → fsync → rename. Failure to write is logged at WARNING
    and does NOT raise — the cooldown is still honored in-memory for the
    current process lifetime, we only lose cross-restart persistence.
    """
    path = _cooldown_registry_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        import tempfile
        with tempfile.NamedTemporaryFile(
            "w",
            dir=str(path.parent),
            delete=False,
            suffix=".tmp",
            prefix=".github_issue_cooldowns.",
        ) as f:
            json.dump(_issue_exhaustion_cooldowns, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
            tmp_path = f.name
        os.replace(tmp_path, str(path))
    except OSError as exc:
        logger.warning(
            "[GitHubIssueSensor] Failed to persist cooldown registry to %s: %s",
            path, exc,
        )


def register_issue_exhaustion(issue_key: str, reason: str = "") -> None:
    """Record that an op sourced from ``issue_key`` exhausted its providers.

    Writes the cooldown both to the in-memory dict AND to the on-disk
    registry so the next process (battle test restart, hot reload, …)
    inherits the state. No-op when
    ``JARVIS_GITHUB_ISSUE_EXHAUSTION_COOLDOWN_S`` is 0 or negative.

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
    _load_cooldown_registry()  # lazy one-shot init
    expires_at = time.time() + _ISSUE_EXHAUSTION_COOLDOWN_S
    _issue_exhaustion_cooldowns[issue_key] = expires_at
    logger.info(
        "[GitHubIssueSensor] Cooldown set for %s (%.0fs, expires_at=%.0f): %s",
        issue_key,
        _ISSUE_EXHAUSTION_COOLDOWN_S,
        expires_at,
        reason[:120],
    )
    _save_cooldown_registry()


def _issue_cooldown_active(issue_key: str) -> bool:
    """Return True when ``issue_key`` is currently within its cooldown window.

    Transparently expires stale entries — if the wall-clock deadline has
    passed, the entry is removed (both from memory and from the on-disk
    registry via a deferred save) and the function returns False.
    Loads the disk registry lazily on first access. Runs in O(1) amortized.
    """
    _load_cooldown_registry()
    expires_at = _issue_exhaustion_cooldowns.get(issue_key)
    if expires_at is None:
        return False
    if time.time() >= expires_at:
        _issue_exhaustion_cooldowns.pop(issue_key, None)
        _save_cooldown_registry()  # persist the prune
        return False
    return True


def clear_issue_cooldowns() -> None:
    """Clear the entire cooldown registry (in-memory AND on disk).

    Intended for tests; also safe to call from operational tooling if a
    human operator wants to manually re-arm the sensor for a specific
    chronic issue. Resets the lazy-load guard so the next access will
    re-read from disk (which will find the emptied file).
    """
    global _cooldown_registry_loaded
    _issue_exhaustion_cooldowns.clear()
    _cooldown_registry_loaded = False
    path = _cooldown_registry_path()
    try:
        if path.exists():
            path.unlink()
    except OSError as exc:
        logger.debug(
            "[GitHubIssueSensor] clear_issue_cooldowns: unlink failed: %s", exc,
        )


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
        # Gap #4 mitigation: when webhook-primary mode is on, the poll loop
        # is a fallback for dropped deliveries, not the dominant path. The
        # caller-supplied ``poll_interval_s`` is honored only when webhooks
        # are off; otherwise ``_GITHUB_FALLBACK_INTERVAL_S`` (default 900s)
        # wins. The flag is re-read at __init__ time — sensors are rebuilt
        # on hot-reload so this matches operator expectations.
        if webhook_enabled():
            self._poll_interval_s = _GITHUB_FALLBACK_INTERVAL_S
            self._webhook_mode = True
        else:
            self._poll_interval_s = poll_interval_s
            self._webhook_mode = False
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
        mode = (
            "webhook-primary (poll=fallback)"
            if self._webhook_mode
            else "poll-primary"
        )
        logger.info(
            "[GitHubIssueSensor] Started — monitoring %d repos, poll=%ds, mode=%s",
            len(self._repos), self._poll_interval_s, mode,
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

    async def ingest_webhook(self, payload: Dict[str, Any]) -> bool:
        """Handle one GitHub ``issues`` webhook delivery.

        Alternative entry point to the poll loop — transforms a webhook
        payload into an ``IssueFinding`` and runs through the **same**
        dedup / cooldown / envelope gates as ``scan_once``. The emitted
        envelope is shape-identical to the poll path, so downstream
        consumers (UnifiedIntakeRouter filters, orchestrator postmortems,
        ``ExhaustionWatcher`` counters) cannot tell the two apart —
        that's the point.

        Only work-relevant actions are honored: ``opened``, ``reopened``,
        ``labeled``, ``edited``. Close / delete / assign actions return
        ``False`` without emission — they don't represent new work for
        the organism.

        Never raises. Returns ``True`` only when an envelope was enqueued;
        ``False`` for ignored actions, malformed payloads, dedup hits,
        and cooldown suppressions. Callers (``EventChannelServer._handle_github``)
        should log the return for observability but must not retry —
        the next webhook delivery or poll scan will cover missed work.

        Manifesto §3: this is the replacement for ``asyncio.sleep`` on
        the hot path for this sensor. When
        ``JARVIS_GITHUB_WEBHOOK_ENABLED=true`` and the server is wired,
        GitHub pushes events to us in real time — the poll loop remains
        only as the dropped-webhook safety net.
        """
        try:
            action = str(payload.get("action", ""))
            if action not in {"opened", "reopened", "labeled", "edited"}:
                logger.debug(
                    "[GitHubIssueSensor] webhook action=%s ignored "
                    "(not work-relevant)", action,
                )
                return False

            issue = payload.get("issue")
            if not isinstance(issue, dict):
                logger.debug(
                    "[GitHubIssueSensor] webhook missing 'issue' dict: "
                    "payload_keys=%s", list(payload.keys())[:8],
                )
                return False

            try:
                issue_number = int(issue.get("number", 0) or 0)
            except (TypeError, ValueError):
                issue_number = 0
            if issue_number <= 0:
                logger.debug("[GitHubIssueSensor] webhook missing issue number")
                return False

            title = str(issue.get("title", "") or "")
            body = str(issue.get("body") or "")

            repository = payload.get("repository")
            if not isinstance(repository, dict):
                logger.debug("[GitHubIssueSensor] webhook missing repository dict")
                return False
            repo_full = str(repository.get("full_name", "") or "")
            if not repo_full:
                logger.debug("[GitHubIssueSensor] webhook missing repo full_name")
                return False

            # Map webhook repo full_name -> our short repo key. If it
            # doesn't match any Trinity repo we still emit, just with a
            # default "jarvis" short key — better than dropping.
            repo_short = "jarvis"
            for name, full, _default_path in self._repos:
                if full == repo_full:
                    repo_short = name
                    break

            labels_raw = issue.get("labels") or []
            labels_list: List[str] = []
            if isinstance(labels_raw, list):
                for lbl in labels_raw:
                    if isinstance(lbl, dict):
                        labels_list.append(str(lbl.get("name", "") or ""))
                    else:
                        labels_list.append(str(lbl))
            labels: Tuple[str, ...] = tuple(lbl for lbl in labels_list if lbl)

            urgency = self._classify_urgency(labels, title)
            auto_resolvable = self._is_auto_resolvable(labels, title, body)

            finding = IssueFinding(
                repo=repo_short,
                repo_full=repo_full,
                issue_number=issue_number,
                title=title,
                labels=labels,
                urgency=urgency,
                auto_resolvable=auto_resolvable,
                body_excerpt=body[:500],
                created_at=str(issue.get("created_at", "") or ""),
                url=str(issue.get("html_url", "") or ""),
                details={"via": "webhook", "action": action},
            )

            dedup_key = f"{finding.repo}:{finding.issue_number}"
            if _issue_cooldown_active(dedup_key):
                logger.info(
                    "[GitHubIssueSensor] webhook #%d (%s): suppressed — "
                    "exhaustion cooldown still active",
                    finding.issue_number, finding.repo,
                )
                return False
            if dedup_key in self._seen_issues:
                logger.debug(
                    "[GitHubIssueSensor] webhook #%d (%s): already seen "
                    "this session (dedup hit)",
                    finding.issue_number, finding.repo,
                )
                return False
            self._seen_issues.add(dedup_key)

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
                    "recurring": 1,
                    "sensor": "GitHubIssueSensor",
                    "via": "webhook",
                    "webhook_action": action,
                },
                requires_human_ack=not finding.auto_resolvable,
            )
            result = await self._router.ingest(envelope)
            if result == "enqueued":
                logger.info(
                    "[GitHubIssueSensor] webhook #%d (%s): %s -> enqueued "
                    "(action=%s, auto=%s, urgency=%s)",
                    finding.issue_number, finding.repo,
                    finding.title[:50], action,
                    finding.auto_resolvable, finding.urgency,
                )
                return True
            logger.debug(
                "[GitHubIssueSensor] webhook #%d (%s): router returned %r",
                finding.issue_number, finding.repo, result,
            )
            return False
        except Exception:
            # Observer contract for event-driven intake: webhook handlers
            # MUST NOT raise, or one bad payload takes down the entire
            # EventChannelServer request.
            logger.debug(
                "[GitHubIssueSensor] webhook ingest failed", exc_info=True,
            )
            return False

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
            # Check the disk-backed cooldown registry FIRST, before the
            # in-memory session dedup. ``_seen_issues`` is cleared on
            # every sensor instance reset (process restart, hot reload,
            # __init__ re-run), but the persisted cooldown survives all
            # three — so the cooldown becomes the authoritative gate for
            # "this issue was recently attempted and failed" regardless
            # of whether this session has also seen it. Without this
            # ordering, a fresh session with a cleared ``_seen_issues``
            # would fall through to the emission path and ingest a chronic
            # issue again, counting toward ExhaustionWatcher's global
            # counter exactly like the bt-2026-04-15-012736 → 013455
            # repeat pattern that motivated this fix.
            if _issue_cooldown_active(dedup_key):
                cooldown_suppressed += 1
                logger.info(
                    "[GitHubIssueSensor] #%d (%s): suppressed — "
                    "exhaustion cooldown still active",
                    finding.issue_number, finding.repo,
                )
                continue
            if dedup_key in self._seen_issues:
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
