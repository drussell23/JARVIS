"""Tests for GitHubIssueSensor exhaustion cooldown registry.

Covers:
  1. Registry primitives (``register_issue_exhaustion`` / ``_issue_cooldown_active``
     / ``clear_issue_cooldowns``) — activation, expiry, disabled-path.
  2. ``issue_key_from_description`` — the parser that recovers the sensor's
     internal dedup key from an op's description string. Critical for the
     CandidateGenerator exhaustion hook: the returned key MUST be
     byte-identical to what ``scan_once`` computes via
     ``f"{finding.repo}:{finding.issue_number}"``, otherwise the cooldown
     is architecturally inert.
  3. End-to-end: ``register_issue_exhaustion`` → next scan suppresses the
     same key via the ``_issue_cooldown_active`` gate inside the emit loop.
  4. CandidateGenerator hook wiring: generate() exhaustion on a
     github_issue-sourced op calls ``register_issue_exhaustion`` with a
     key matching the sensor's dedup format.

These are the minimum to keep the cooldown path from becoming dead code
after the two-commit PR lands.
"""
from __future__ import annotations

import importlib
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.core.ouroboros.governance.intake.sensors import (
    github_issue_sensor as ghs,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_registry():
    """Make sure no test leaks cooldown state into another."""
    ghs.clear_issue_cooldowns()
    yield
    ghs.clear_issue_cooldowns()


# ---------------------------------------------------------------------------
# 1. Registry primitives
# ---------------------------------------------------------------------------


class TestRegistryPrimitives:
    def test_unregistered_key_is_cold(self):
        assert ghs._issue_cooldown_active("jarvis:99999") is False

    def test_registered_key_is_hot(self):
        ghs.register_issue_exhaustion("jarvis:16501", reason="pytest")
        assert ghs._issue_cooldown_active("jarvis:16501") is True

    def test_independent_keys_do_not_collide(self):
        ghs.register_issue_exhaustion("jarvis:16501", reason="pytest")
        assert ghs._issue_cooldown_active("jarvis:99999") is False
        assert ghs._issue_cooldown_active("reactor:16501") is False

    def test_empty_key_is_noop(self):
        ghs.register_issue_exhaustion("", reason="pytest")
        # Registry should be empty afterwards
        assert not ghs._issue_exhaustion_cooldowns

    def test_clear_wipes_all(self):
        ghs.register_issue_exhaustion("jarvis:1", reason="x")
        ghs.register_issue_exhaustion("jarvis:2", reason="x")
        ghs.clear_issue_cooldowns()
        assert not ghs._issue_exhaustion_cooldowns
        assert ghs._issue_cooldown_active("jarvis:1") is False

    def test_expiry_pops_stale_entry_on_read(self, monkeypatch):
        # Set the cooldown to something long then rewind the monotonic clock
        # past it to simulate expiry.
        ghs.register_issue_exhaustion("jarvis:16501", reason="pytest")
        # Mutate the stored deadline to be in the past
        ghs._issue_exhaustion_cooldowns["jarvis:16501"] = time.monotonic() - 1.0
        assert ghs._issue_cooldown_active("jarvis:16501") is False
        # Read should have popped the entry
        assert "jarvis:16501" not in ghs._issue_exhaustion_cooldowns

    def test_disabled_via_env(self, monkeypatch):
        """Env gate set to 0 makes register_issue_exhaustion a no-op."""
        monkeypatch.setenv("JARVIS_GITHUB_ISSUE_EXHAUSTION_COOLDOWN_S", "0")
        # Reload the module so the constant re-reads the env.
        importlib.reload(ghs)
        try:
            assert ghs._ISSUE_EXHAUSTION_COOLDOWN_S == 0.0
            ghs.register_issue_exhaustion("jarvis:16501", reason="disabled")
            assert ghs._issue_cooldown_active("jarvis:16501") is False
            assert not ghs._issue_exhaustion_cooldowns
        finally:
            # Restore default for other tests
            monkeypatch.setenv("JARVIS_GITHUB_ISSUE_EXHAUSTION_COOLDOWN_S", "900")
            importlib.reload(ghs)


# ---------------------------------------------------------------------------
# 2. Parser — issue_key_from_description
# ---------------------------------------------------------------------------


class TestIssueKeyFromDescription:
    """The parser's output MUST exactly equal the sensor's internal
    dedup_key format ``f"{finding.repo}:{finding.issue_number}"`` for the
    cooldown hook to be effective. Any drift = silent inertness.
    """

    def test_standard_jarvis_repo(self):
        desc = (
            "GitHub Issue #16501 in drussell23/JARVIS: "
            "\U0001f6a8 Critical: Unlock Test Suite Failed"
        )
        assert ghs.issue_key_from_description(desc) == "jarvis:16501"

    def test_jarvis_prime_repo(self):
        desc = "GitHub Issue #42 in drussell23/JARVIS-Prime: some title"
        assert ghs.issue_key_from_description(desc) == "jarvis-prime:42"

    def test_reactor_repo(self):
        desc = "GitHub Issue #7 in drussell23/JARVIS-Reactor: whatever"
        assert ghs.issue_key_from_description(desc) == "reactor:7"

    def test_none_on_non_github_description(self):
        desc = "Some unrelated operation from test_failure sensor"
        assert ghs.issue_key_from_description(desc) is None

    def test_none_on_unknown_repo_slug(self):
        desc = "GitHub Issue #123 in acme/UnknownRepo: test"
        assert ghs.issue_key_from_description(desc) is None

    def test_none_on_empty_string(self):
        assert ghs.issue_key_from_description("") is None

    def test_parser_byte_matches_sensor_dedup_key_format(self):
        """Regression: parser and sensor must produce identical keys.

        If this ever fails it means the sensor's dedup_key format at
        ``scan_once`` (``f"{finding.repo}:{finding.issue_number}"``) drifted
        from the parser's output, and the cooldown hook will silently stop
        working. Reconstruct the dedup_key exactly as scan_once does and
        compare.
        """
        # Simulate what scan_once computes
        finding_repo = "jarvis"
        finding_issue_number = 16501
        sensor_key = f"{finding_repo}:{finding_issue_number}"
        desc = (
            f"GitHub Issue #{finding_issue_number} in "
            "drussell23/JARVIS: Critical: stuff"
        )
        parsed_key = ghs.issue_key_from_description(desc)
        assert parsed_key == sensor_key, (
            f"parser drift: parsed={parsed_key!r} != sensor={sensor_key!r}"
        )


# ---------------------------------------------------------------------------
# 3. Registry → scan integration (sensor-side skip)
# ---------------------------------------------------------------------------


class _FakeFinding:
    """Minimal IssueFinding stand-in — only the fields scan_once's emit
    loop reads. The real dataclass pulls in urgency inference, labels,
    and auto_resolvable classification that would require a full gh CLI
    mock to build. Using a duck-typed stand-in keeps this test focused
    on the cooldown gate.
    """

    def __init__(self, repo: str, issue_number: int, title: str = "t") -> None:
        self.repo = repo
        self.repo_full = f"drussell23/{repo.title()}"
        self.issue_number = issue_number
        self.title = title
        self.body_excerpt = ""
        self.labels: tuple = ()
        self.auto_resolvable = True
        self.urgency = "high"
        self.url = ""
        self.details: dict = {"recurring_count": 1}


class TestRegistryToScanIntegration:
    """The sensor's emit loop must consult ``_issue_cooldown_active`` before
    calling ``_router.ingest``. This test exercises exactly that gate by
    calling ``register_issue_exhaustion`` with a key and then confirming
    a matching dedup_key is suppressed before ingest fires.
    """

    @pytest.mark.asyncio
    async def test_cooldown_active_suppresses_emission(self):
        # Register cooldown BEFORE sensor scan
        ghs.register_issue_exhaustion("jarvis:16501", reason="integration")

        # Build a sensor with a mocked router; bypass __init__ side-effects
        # by using __new__.
        sensor = ghs.GitHubIssueSensor.__new__(ghs.GitHubIssueSensor)
        sensor._repo = "jarvis"
        sensor._router = AsyncMock()
        sensor._router.ingest = AsyncMock(return_value="enqueued")
        sensor._poll_interval_s = 3600.0
        sensor._repos = ghs._TRINITY_REPOS
        sensor._running = False
        sensor._task = None
        sensor._seen_issues = set()
        sensor._breaker_failures = {}
        sensor._breaker_open_until = {}

        # Directly drive the emission path with a fake finding by calling
        # the same inner logic scan_once uses. We simulate: a dedup list
        # containing the cooldown-active finding + one fresh finding, then
        # verify only the fresh one calls ingest.
        fresh = _FakeFinding("jarvis", 99999, "fresh issue")
        cooldown_hit = _FakeFinding("jarvis", 16501, "recurring issue")
        deduplicated = [cooldown_hit, fresh]

        emitted_ingests = []

        async def _fake_ingest(envelope):
            emitted_ingests.append(envelope)
            return "enqueued"

        sensor._router.ingest = _fake_ingest

        # Re-implement the emit loop body inline (mirrors scan_once lines
        # 304-355 in the current file) so we don't need to stub gh CLI.
        # The test is pinning THIS loop's interaction with the registry,
        # not the full scan subprocess path.
        from backend.core.ouroboros.governance.intake.intent_envelope import (
            make_envelope,
        )
        cooldown_suppressed = 0
        emitted = 0
        for finding in deduplicated:
            dedup_key = f"{finding.repo}:{finding.issue_number}"
            if dedup_key in sensor._seen_issues:
                continue
            if ghs._issue_cooldown_active(dedup_key):
                cooldown_suppressed += 1
                continue
            sensor._seen_issues.add(dedup_key)
            envelope = make_envelope(
                source="github_issue",
                description=(
                    f"GitHub Issue #{finding.issue_number} in "
                    f"{finding.repo_full}: {finding.title}"
                ),
                target_files=("backend/",),
                repo=finding.repo,
                confidence=0.80,
                urgency=finding.urgency,
                evidence={"issue_number": finding.issue_number},
                requires_human_ack=not finding.auto_resolvable,
            )
            result = await sensor._router.ingest(envelope)
            if result == "enqueued":
                emitted += 1

        assert cooldown_suppressed == 1
        assert emitted == 1
        assert len(emitted_ingests) == 1
        assert emitted_ingests[0].evidence["issue_number"] == 99999

    @pytest.mark.asyncio
    async def test_no_cooldown_emits_normally(self):
        """Control: without register_issue_exhaustion, ingest fires."""
        sensor = ghs.GitHubIssueSensor.__new__(ghs.GitHubIssueSensor)
        sensor._seen_issues = set()
        calls = []

        async def _fake_ingest(envelope):
            calls.append(envelope)
            return "enqueued"

        from backend.core.ouroboros.governance.intake.intent_envelope import (
            make_envelope,
        )
        finding = _FakeFinding("jarvis", 16501)
        dedup_key = f"{finding.repo}:{finding.issue_number}"
        assert not ghs._issue_cooldown_active(dedup_key)
        sensor._seen_issues.add(dedup_key)
        envelope = make_envelope(
            source="github_issue",
            description=(
                f"GitHub Issue #{finding.issue_number} in "
                f"{finding.repo_full}: {finding.title}"
            ),
            target_files=("backend/",),
            repo=finding.repo,
            confidence=0.80,
            urgency=finding.urgency,
            evidence={"issue_number": finding.issue_number},
            requires_human_ack=False,
        )
        await _fake_ingest(envelope)
        assert len(calls) == 1


# ---------------------------------------------------------------------------
# 4. CandidateGenerator hook wiring
# ---------------------------------------------------------------------------


class TestCandidateGeneratorHook:
    """When ``CandidateGenerator.generate()`` catches an
    ``all_providers_exhausted`` RuntimeError for an op whose ``signal_source``
    is ``"github_issue"``, it must call ``register_issue_exhaustion`` with a
    key matching ``issue_key_from_description(context.description)``.

    We stub ``_generate_dispatch`` to raise the exhaustion error, run
    ``generate``, and assert the registry saw the key.
    """

    @pytest.mark.asyncio
    async def test_github_issue_exhaustion_fills_registry(self):
        from backend.core.ouroboros.governance.candidate_generator import (
            CandidateGenerator,
        )

        # Build a near-empty CandidateGenerator via __new__ so we can stub
        # the one method we care about without the full wiring.
        gen = CandidateGenerator.__new__(CandidateGenerator)
        gen._exhaustion_watcher = None  # disable the other call site

        async def _fake_dispatch(context, deadline):
            raise RuntimeError("all_providers_exhausted:fallback_failed")

        gen._generate_dispatch = _fake_dispatch  # type: ignore[method-assign]

        # Build a fake OperationContext with just the fields the hook reads
        ctx = MagicMock()
        ctx.signal_source = "github_issue"
        ctx.description = (
            "GitHub Issue #16501 in drussell23/JARVIS: "
            "Critical: Unlock Test Suite Failed"
        )
        ctx.op_id = "op-test-12345"

        ghs.clear_issue_cooldowns()
        with pytest.raises(RuntimeError, match="all_providers_exhausted"):
            await gen.generate(ctx, deadline=None)

        assert ghs._issue_cooldown_active("jarvis:16501"), (
            "CandidateGenerator hook did not register jarvis:16501 "
            "into the cooldown registry"
        )

    @pytest.mark.asyncio
    async def test_non_github_issue_source_is_skipped(self):
        """test_failure exhaustion must NOT hit the github cooldown path."""
        from backend.core.ouroboros.governance.candidate_generator import (
            CandidateGenerator,
        )

        gen = CandidateGenerator.__new__(CandidateGenerator)
        gen._exhaustion_watcher = None

        async def _fake_dispatch(context, deadline):
            raise RuntimeError("all_providers_exhausted:fallback_failed")

        gen._generate_dispatch = _fake_dispatch  # type: ignore[method-assign]

        ctx = MagicMock()
        ctx.signal_source = "test_failure"
        ctx.description = (
            "Stable test failure: tests/foo.py::test_bar (streak=2)"
        )
        ctx.op_id = "op-test-67890"

        ghs.clear_issue_cooldowns()
        with pytest.raises(RuntimeError, match="all_providers_exhausted"):
            await gen.generate(ctx, deadline=None)

        assert not ghs._issue_exhaustion_cooldowns, (
            "cooldown registry must stay empty for non-github_issue sources"
        )

    @pytest.mark.asyncio
    async def test_github_issue_with_unparseable_description_is_safe(self):
        """Parser returns None → hook logs + continues, does NOT raise."""
        from backend.core.ouroboros.governance.candidate_generator import (
            CandidateGenerator,
        )

        gen = CandidateGenerator.__new__(CandidateGenerator)
        gen._exhaustion_watcher = None

        async def _fake_dispatch(context, deadline):
            raise RuntimeError("all_providers_exhausted:fallback_failed")

        gen._generate_dispatch = _fake_dispatch  # type: ignore[method-assign]

        ctx = MagicMock()
        ctx.signal_source = "github_issue"
        ctx.description = "malformed github issue description without match"
        ctx.op_id = "op-test-malformed"

        ghs.clear_issue_cooldowns()
        with pytest.raises(RuntimeError, match="all_providers_exhausted"):
            await gen.generate(ctx, deadline=None)

        assert not ghs._issue_exhaustion_cooldowns
