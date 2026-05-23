"""Slice 12Y — Dynamic Budget Reservation & WAL-second Telemetry Seal.

bt-2026-05-23-211212 (Slice 12X validation soak) achieved the historic
``operations[]=19`` populated milestone via Slice 12V WAL-first and
proved clean shutdown via Slices 12U+12V+12W+12X — but the fixture op
itself was refused at preflight:

  session_budget_preflight_refused:
    claude_est=$0.5000 > session_remaining=$0.4863

Concurrent BACKGROUND-tier sensor ops (todo_scanner, doc_staleness,
opportunity_miner, etc.) had collectively consumed $0.514 of the
$1.00 session cap, leaving the foreground fixture in a starvation
window between sensor budget consumption and its own $0.50 per-call
Claude estimate.

Slice 12Y closes this via two composable parts:

# Part 1 — Dynamic Background Spend Ceiling

New env knob ``JARVIS_BACKGROUND_SPEND_LIMIT_PCT`` (default 0.5)
defines the fraction of total session cap that BACKGROUND-tier ops
may consume cumulatively. The complement (1 - limit_pct) is the
RESERVED RUNWAY for foreground / complex / fixture ops.

``check_preflight`` gains an optional ``signal_source`` kwarg. When
the source matches the mirrored urgency_router taxonomy
(_BACKGROUND_SOURCES + _SPECULATIVE_SOURCES) the gate applies an
additional check BEFORE the legacy ``est > remaining`` refusal:

  foreground_reserve = total_cap * (1 - limit_pct)
  bg_remaining = max(0, remaining - foreground_reserve)
  refuse if est > bg_remaining

With default 0.5 + cap=$1.00:
  * sensors collectively burn up to $0.50 → at that point their
    per-call preflight refuses with the structured reason
    ``session_budget_preflight_refused:background_spend_ceiling``
  * foreground ops always see the full ``remaining`` so the
    original $0.50 reserve stays available for at least one
    complex Claude call

When ``signal_source`` is None (default — all pre-Slice-12Y
callers), behavior is byte-identical to the legacy gate.

# Part 2 — WAL-second Telemetry Seal

bt-2026-05-23-211212 also showed that Slice 12W WAL-second's
success log line never appeared in debug.log even though the
WAL-second code was reachable. Root cause: when the cleanup chain
hangs upstream (``intake_service.stop()`` wedged at step 2 in that
soak), execution reaches WAL-second through a degraded path where
logger handlers are in questionable state.

Slice 12Y makes the telemetry airtight via THREE emissions:

  (1) entry-marker logged BEFORE any potentially failing operation
      — proves WAL-second was REACHED even if the write throws
  (2) stderr direct-write (bypasses logger handler chain entirely
      — survives wedged handlers, lands in tee/pipe redirects)
  (3) success-marker via standard logger after write completes

All three are individually try-wrapped so a single failure can't
mask the others.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import Optional

import pytest

from backend.core.ouroboros.governance import session_budget_authority
from backend.core.ouroboros.governance.session_budget_authority import (
    BACKGROUND_SPEND_LIMIT_PCT_ENV_VAR,
    SessionBudgetPreflightRefused,
    _BACKGROUND_TIER_SIGNAL_SOURCES,
    check_preflight,
    get_background_spend_limit_pct,
    get_session_total_cap_usd,
    is_background_tier_source,
    reset_for_tests,
    set_session_budget_provider,
)


# ──────────────────────────────────────────────────────────────────────
# Shared fixtures
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    for var in (
        BACKGROUND_SPEND_LIMIT_PCT_ENV_VAR,
        "JARVIS_S2_SESSION_BUDGET_USD",
        "OUROBOROS_BATTLE_COST_CAP",
    ):
        monkeypatch.delenv(var, raising=False)
    reset_for_tests()
    yield
    reset_for_tests()


class _FakeProvider:
    """Duck-typed SBA provider with mutable total_spent + remaining."""

    def __init__(self, total_spent: float, remaining: float):
        self.total_spent = total_spent
        self.remaining = remaining


# ──────────────────────────────────────────────────────────────────────
# Part 1 — Env knob + helpers
# ──────────────────────────────────────────────────────────────────────


class TestPart1EnvKnob:
    def test_default_is_half(self, monkeypatch):
        monkeypatch.delenv(
            BACKGROUND_SPEND_LIMIT_PCT_ENV_VAR, raising=False,
        )
        assert get_background_spend_limit_pct() == 0.5

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("0.0", 0.0), ("0.25", 0.25), ("0.5", 0.5),
            ("0.75", 0.75), ("1.0", 1.0),
            # Out-of-range clamps to [0, 1].
            ("-0.5", 0.0), ("2.0", 1.0),
            # Garbage falls back to default.
            ("garbage", 0.5), ("", 0.5),
        ],
    )
    def test_value_parsing_and_clamping(
        self, monkeypatch, raw, expected,
    ):
        monkeypatch.setenv(
            BACKGROUND_SPEND_LIMIT_PCT_ENV_VAR, raw,
        )
        assert get_background_spend_limit_pct() == expected


class TestPart1IsBackgroundTier:
    @pytest.mark.parametrize(
        "src",
        list(_BACKGROUND_TIER_SIGNAL_SOURCES),
    )
    def test_all_canonical_sources_match(self, src):
        assert is_background_tier_source(src) is True

    @pytest.mark.parametrize(
        "src",
        [
            "swe_bench_pro", "voice_human", "runtime_health",
            "github_issue", None, "", "unknown",
        ],
    )
    def test_non_background_sources_do_not_match(self, src):
        assert is_background_tier_source(src) is False

    def test_case_insensitive(self):
        assert is_background_tier_source("TODO_SCANNER") is True
        assert is_background_tier_source("Todo_Scanner") is True

    def test_taxonomy_matches_urgency_router(self):
        """Defensive duplication pin — _BACKGROUND_TIER_SIGNAL_SOURCES
        MUST stay in sync with urgency_router._BACKGROUND_SOURCES +
        _SPECULATIVE_SOURCES. The SBA can't import urgency_router
        (layering loop), so the two tables must be manually kept
        in sync; this test fails loudly if they drift."""
        from backend.core.ouroboros.governance.urgency_router import (
            _BACKGROUND_SOURCES,
            _SPECULATIVE_SOURCES,
        )
        canonical = set(_BACKGROUND_SOURCES) | set(
            _SPECULATIVE_SOURCES
        )
        assert _BACKGROUND_TIER_SIGNAL_SOURCES == frozenset(
            canonical,
        ), (
            f"SBA tier set {_BACKGROUND_TIER_SIGNAL_SOURCES} "
            f"drifted from urgency_router {canonical} — update "
            "session_budget_authority._BACKGROUND_TIER_SIGNAL_SOURCES"
        )


class TestPart1TotalCap:
    def test_returns_none_when_no_provider(self):
        reset_for_tests()
        assert get_session_total_cap_usd() is None

    def test_returns_total_spent_plus_remaining(self):
        set_session_budget_provider(
            _FakeProvider(total_spent=0.50, remaining=0.50),
        )
        assert get_session_total_cap_usd() == 1.0

    def test_handles_provider_exceptions(self):
        class _Broken:
            @property
            def total_spent(self):
                raise RuntimeError("boom")
            @property
            def remaining(self):
                raise RuntimeError("also broken")

        set_session_budget_provider(_Broken())
        # Both reads fail → 0 + 0 = 0 (NEVER raises).
        assert get_session_total_cap_usd() == 0.0


# ──────────────────────────────────────────────────────────────────────
# Part 1 — check_preflight background-spend ceiling
# ──────────────────────────────────────────────────────────────────────


class TestPart1Ceiling:
    """The load-bearing claim: background ops are refused when
    they would push cumulative spend above
    ``total_cap * limit_pct``, while foreground ops still see the
    full ``remaining``."""

    def test_background_op_refused_when_reserve_breached(
        self, monkeypatch,
    ):
        """Cap=$1.00, spent=$0.50, remaining=$0.50, limit_pct=0.5
        → foreground_reserve=$0.50 → bg_remaining=$0.00
        → background $0.10 op MUST refuse with structured reason."""
        monkeypatch.setenv(
            BACKGROUND_SPEND_LIMIT_PCT_ENV_VAR, "0.5",
        )
        set_session_budget_provider(
            _FakeProvider(total_spent=0.50, remaining=0.50),
        )
        with pytest.raises(SessionBudgetPreflightRefused) as excinfo:
            check_preflight(
                provider_name="dw",
                estimated_cost_usd=0.10,
                signal_source="todo_scanner",
            )
        # Structured reason distinct from generic preflight.
        assert excinfo.value.reason == (
            "session_budget_preflight_refused:"
            "background_spend_ceiling"
        )

    def test_foreground_op_admitted_at_same_state(self, monkeypatch):
        """SAME budget state as above — foreground op (no signal
        in background tier) MUST be admitted up to the full
        $0.50 remaining."""
        monkeypatch.setenv(
            BACKGROUND_SPEND_LIMIT_PCT_ENV_VAR, "0.5",
        )
        set_session_budget_provider(
            _FakeProvider(total_spent=0.50, remaining=0.50),
        )
        # Foreground op estimated $0.50 — exactly within remaining,
        # MUST NOT raise.
        check_preflight(
            provider_name="claude",
            estimated_cost_usd=0.50,
            signal_source="swe_bench_pro",
        )

    def test_legacy_path_unchanged_when_signal_source_none(
        self, monkeypatch,
    ):
        """When signal_source is None (default — all pre-Slice-12Y
        callers), behavior MUST be byte-identical to the legacy
        gate."""
        monkeypatch.setenv(
            BACKGROUND_SPEND_LIMIT_PCT_ENV_VAR, "0.5",
        )
        set_session_budget_provider(
            _FakeProvider(total_spent=0.50, remaining=0.50),
        )
        # No signal_source → no background ceiling → admits $0.50.
        check_preflight(
            provider_name="claude",
            estimated_cost_usd=0.50,
        )

    def test_background_op_admitted_when_room_remains(
        self, monkeypatch,
    ):
        """Cap=$1.00, spent=$0.10, remaining=$0.90, limit_pct=0.5
        → foreground_reserve=$0.50 → bg_remaining=$0.40
        → background $0.20 op MUST be admitted (room left)."""
        monkeypatch.setenv(
            BACKGROUND_SPEND_LIMIT_PCT_ENV_VAR, "0.5",
        )
        set_session_budget_provider(
            _FakeProvider(total_spent=0.10, remaining=0.90),
        )
        check_preflight(
            provider_name="dw",
            estimated_cost_usd=0.20,
            signal_source="opportunity_miner",
        )

    def test_limit_pct_one_disables_reservation(self, monkeypatch):
        """limit_pct=1.0 → foreground_reserve=0 → background ops
        see the full remaining (pre-Slice-12Y rollback path)."""
        monkeypatch.setenv(
            BACKGROUND_SPEND_LIMIT_PCT_ENV_VAR, "1.0",
        )
        set_session_budget_provider(
            _FakeProvider(total_spent=0.50, remaining=0.50),
        )
        # Background op CAN use full $0.50 remaining when ceiling
        # is disabled.
        check_preflight(
            provider_name="dw",
            estimated_cost_usd=0.50,
            signal_source="todo_scanner",
        )

    def test_limit_pct_zero_blocks_all_background(self, monkeypatch):
        """limit_pct=0.0 → foreground_reserve=total_cap →
        bg_remaining always 0 → every background op refused."""
        monkeypatch.setenv(
            BACKGROUND_SPEND_LIMIT_PCT_ENV_VAR, "0.0",
        )
        set_session_budget_provider(
            _FakeProvider(total_spent=0.0, remaining=1.0),
        )
        with pytest.raises(SessionBudgetPreflightRefused):
            check_preflight(
                provider_name="dw",
                estimated_cost_usd=0.01,
                signal_source="todo_scanner",
            )

    def test_no_provider_no_op(self, monkeypatch):
        """When no SBA provider registered (env-only configs,
        tests, headless rigs), preflight fails OPEN regardless of
        signal_source — preserves pre-Slice-12Y behavior."""
        monkeypatch.setenv(
            BACKGROUND_SPEND_LIMIT_PCT_ENV_VAR, "0.5",
        )
        reset_for_tests()
        # Must NOT raise.
        check_preflight(
            provider_name="dw",
            estimated_cost_usd=10.0,
            signal_source="todo_scanner",
        )


class TestPart1ASTPin:
    def _read(self, path: str) -> str:
        return Path(path).read_text()

    def test_providers_pass_signal_source_to_preflight(self):
        """The provider call sites MUST forward signal_source from
        the ProviderContext so the SBA can apply the ceiling.
        Pinned because Slice 12W's similar oversight let posthog
        spawn unattributed."""
        src = self._read(
            "backend/core/ouroboros/governance/providers.py"
        )
        # Find the check_preflight call site.
        idx = src.find("_sba_check_preflight(")
        assert idx > 0, "check_preflight call missing from providers.py"
        block = src[idx:idx + 800]
        assert "signal_source" in block, (
            "providers.py check_preflight call doesn't pass "
            "signal_source — background spend ceiling can't fire"
        )

    def test_doubleword_provider_passes_signal_source(self):
        src = self._read(
            "backend/core/ouroboros/governance/doubleword_provider.py"
        )
        idx = src.find("_sba_check_preflight(")
        assert idx > 0
        block = src[idx:idx + 800]
        assert "signal_source" in block, (
            "doubleword_provider.py check_preflight call doesn't "
            "pass signal_source"
        )


# ──────────────────────────────────────────────────────────────────────
# Part 2 — WAL-second telemetry seal
# ──────────────────────────────────────────────────────────────────────


class TestPart2WALSecondTelemetry:
    """The seal must guarantee 3 emissions: stderr entry-marker,
    logger entry-marker, success-or-failure stderr+logger pair."""

    def _read_harness(self) -> str:
        return Path(
            "backend/core/ouroboros/battle_test/harness.py"
        ).read_text()

    def test_wal_second_has_entry_stderr_marker(self):
        """A direct stderr write MUST precede the WAL-second call
        so we know it was reached even if the logger is wedged."""
        src = self._read_harness()
        idx = src.find("Slice 12W Phase 3")
        assert idx > 0
        block = src[idx:idx + 5000]
        assert "Slice12Y.WALSecond.REACHED" in block, (
            "Slice 12Y entry-stderr marker missing — wedged "
            "logger handlers will silently swallow attribution"
        )

    def test_wal_second_has_entry_logger_marker(self):
        """A logger.info ENTRY marker fires BEFORE the write so
        we see WAL-second was reached even if the write throws."""
        src = self._read_harness()
        idx = src.find("Slice 12W Phase 3")
        block = src[idx:idx + 5000]
        assert "ENTRY" in block, (
            "Slice 12Y logger entry-marker missing — only the "
            "success path emits; failures invisible"
        )

    def test_wal_second_has_result_stderr_marker(self):
        """A stderr RESULT marker fires AFTER the write with
        succeeded=True/False so operators see the outcome.

        Match against the full harness file (the WAL-second
        block is large enough that the marker can land outside
        any fixed-size search window after the Phase 3 header)."""
        src = self._read_harness()
        assert "Slice12Y.WALSecond.RESULT" in src, (
            "Slice 12Y stderr result-marker missing — operators "
            "can't see whether the WAL-second write succeeded "
            "when logger is wedged"
        )

    def test_wal_second_tracks_succeeded_flag(self):
        """The success path must set ``_wal_second_succeeded =
        True`` ONLY after the write returns cleanly. Failure
        path falls through with the flag still False."""
        src = self._read_harness()
        idx = src.find("Slice 12W Phase 3")
        block = src[idx:idx + 5000]
        assert "_wal_second_succeeded = False" in block
        assert "_wal_second_succeeded = True" in block


class TestPart2HarnessPosition:
    """Position pin: WAL-second still lives between step 4
    (GovernedLoopService.stop) and step 5 (GovernanceStack.stop).
    Slice 12W's positioning is preserved; Slice 12Y only adds
    telemetry emissions."""

    def test_wal_second_still_between_step_4_and_step_5(self):
        src = Path(
            "backend/core/ouroboros/battle_test/harness.py"
        ).read_text()
        idx_step4 = src.find("# 4. Governed loop service")
        idx_wal_second = src.find("Slice 12W Phase 3")
        idx_step5 = src.find("# 5. Governance stack")
        assert idx_step4 > 0
        assert idx_wal_second > 0
        assert idx_step5 > 0
        assert idx_step4 < idx_wal_second < idx_step5


# ──────────────────────────────────────────────────────────────────────
# Public surface
# ──────────────────────────────────────────────────────────────────────


class TestPart1PublicSurface:
    def test_exports(self):
        for name in (
            "BACKGROUND_SPEND_LIMIT_PCT_ENV_VAR",
            "_BACKGROUND_TIER_SIGNAL_SOURCES",
            "check_preflight",
            "get_background_spend_limit_pct",
            "get_session_total_cap_usd",
            "is_background_tier_source",
        ):
            assert hasattr(session_budget_authority, name), (
                f"session_budget_authority.{name} missing"
            )
            assert name in session_budget_authority.__all__, (
                f"{name} missing from __all__"
            )
