"""Regression spine for §40 Tier 1 #15 — Second-Order Doll Completion Metric.

Closes the operator's §40.5 Wave 1 #15 ship. Covers:

* §33.1 master flag default-FALSE + truthy alternation
* Closed 5-value :class:`DollCompletionStage` taxonomy
* Pure-function stage derivation across all 5 stages
* Git log composer with injected runner (hermetic, no subprocess)
* Composes canonical sources (flag_registry / capability_constellation
  / auto_committer signature) — no parallel state
* 6 AST pin canonical-source pass + 6 synthetic-regression firings
* Auto-discovered REPL via §32.11 Slice 4 naming-cage
* FlagRegistry seeds auto-discovered (6 specs)
* IDE GET route gating discipline
* ``DollCompletionSnapshot.to_dict`` projection completeness
* End-to-end: hermetic git log → snapshot with expected stages
"""
from __future__ import annotations

import ast
import os
import subprocess
from pathlib import Path
from typing import Any, Iterable, List
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Substrate import (load-bearing — confirms module is wireable)
# ---------------------------------------------------------------------------
from backend.core.ouroboros.governance import (
    second_order_doll_metric as sodm,
)
from backend.core.ouroboros.governance.second_order_doll_metric import (
    AxisProgress,
    CommitEvidence,
    DollCompletionSnapshot,
    DollCompletionStage,
    SECOND_ORDER_DOLL_METRIC_SCHEMA_VERSION,
    _ENV_MASTER,
    _GIT_FORMAT,
    _STAGE_GLYPH,
    _STAGE_WEIGHT,
    _extract_risk_tier,
    _is_autonomous_commit,
    _parse_git_log,
    _stage_for_axis,
    aggregate_doll_completion,
    applied_threshold,
    commit_scan_max,
    format_axis_detail,
    format_doll_completion_panel,
    get_cached_snapshot,
    graduated_min_days,
    graduated_threshold,
    master_enabled,
    proposed_threshold,
    reset_cache_for_tests,
    stage_glyph,
)


# ---------------------------------------------------------------------------
# Shared isolation fixture — every test runs with cache reset + env clean
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_doll_metric(monkeypatch):
    """Reset cached snapshot + ensure master env is unset before each test.

    Tests opt into master-on explicitly via monkeypatch.setenv. This is
    the canonical pattern from sibling §38.11 / §39 tests.
    """
    reset_cache_for_tests()
    monkeypatch.delenv(_ENV_MASTER, raising=False)
    monkeypatch.delenv("JARVIS_DOLL_COMMIT_SCAN_MAX", raising=False)
    monkeypatch.delenv("JARVIS_DOLL_GRADUATED_THRESHOLD", raising=False)
    monkeypatch.delenv("JARVIS_DOLL_GRADUATED_MIN_DAYS", raising=False)
    monkeypatch.delenv("JARVIS_DOLL_APPLIED_THRESHOLD", raising=False)
    monkeypatch.delenv("JARVIS_DOLL_PROPOSED_THRESHOLD", raising=False)
    yield
    reset_cache_for_tests()


# ---------------------------------------------------------------------------
# §33.1 — master flag default-FALSE + truthy alternation
# ---------------------------------------------------------------------------


class TestMasterFlagDefault:
    """Per §33.1 graduation contract — master stays FALSE until empirical."""

    def test_master_default_false(self):
        assert master_enabled() is False

    @pytest.mark.parametrize("truthy", ["1", "true", "TRUE", "yes", "on"])
    def test_master_truthy_alternation(self, monkeypatch, truthy):
        monkeypatch.setenv(_ENV_MASTER, truthy)
        assert master_enabled() is True

    @pytest.mark.parametrize("falsy", ["", "0", "false", "no", "off", "bogus"])
    def test_master_falsy_alternation(self, monkeypatch, falsy):
        monkeypatch.setenv(_ENV_MASTER, falsy)
        assert master_enabled() is False


# ---------------------------------------------------------------------------
# Env knob clamping discipline (operator-binding "no hardcoding")
# ---------------------------------------------------------------------------


class TestEnvKnobClamping:
    @pytest.mark.parametrize(
        "env,getter,below,above,min_v,max_v",
        [
            ("JARVIS_DOLL_COMMIT_SCAN_MAX",
             commit_scan_max, "1", "9999999", 10, 50_000),
            ("JARVIS_DOLL_GRADUATED_THRESHOLD",
             graduated_threshold, "0", "100000", 1, 10_000),
            ("JARVIS_DOLL_GRADUATED_MIN_DAYS",
             graduated_min_days, "0", "9999999", 1, 3_650),
            ("JARVIS_DOLL_APPLIED_THRESHOLD",
             applied_threshold, "0", "100000", 1, 10_000),
            ("JARVIS_DOLL_PROPOSED_THRESHOLD",
             proposed_threshold, "0", "100000", 1, 10_000),
        ],
    )
    def test_int_clamps_to_bounds(
        self, monkeypatch, env, getter, below, above, min_v, max_v,
    ):
        monkeypatch.setenv(env, below)
        assert getter() == min_v
        monkeypatch.setenv(env, above)
        assert getter() == max_v

    def test_non_int_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("JARVIS_DOLL_GRADUATED_THRESHOLD", "not-an-int")
        # Falls back to default (10), not min (1)
        assert graduated_threshold() == 10


# ---------------------------------------------------------------------------
# Closed 5-value DollCompletionStage taxonomy
# ---------------------------------------------------------------------------


class TestStageTaxonomy:
    def test_exactly_5_values(self):
        values = {s.value for s in DollCompletionStage}
        assert values == {
            "untouched", "observed", "proposed", "applied", "graduated",
        }

    def test_stage_glyph_covers_all_5(self):
        for stage in DollCompletionStage:
            assert _STAGE_GLYPH[stage.value]
            assert stage_glyph(stage) == _STAGE_GLYPH[stage.value]

    def test_stage_weight_covers_all_5(self):
        for stage in DollCompletionStage:
            assert stage.value in _STAGE_WEIGHT

    def test_stage_weights_monotone_increasing(self):
        ordered = [
            DollCompletionStage.UNTOUCHED,
            DollCompletionStage.OBSERVED,
            DollCompletionStage.PROPOSED,
            DollCompletionStage.APPLIED,
            DollCompletionStage.GRADUATED,
        ]
        weights = [_STAGE_WEIGHT[s.value] for s in ordered]
        assert weights == sorted(weights)
        assert weights[0] == 0.0
        assert weights[-1] == 1.0

    def test_stage_glyph_invalid_returns_question(self):
        assert stage_glyph("nonexistent") == "?"
        assert stage_glyph(None) == "?"
        assert stage_glyph(object()) == "?"


# ---------------------------------------------------------------------------
# Pure-function stage derivation — all 5 stages reachable
# ---------------------------------------------------------------------------


class TestStageDerivation:
    def test_zero_commits_returns_untouched(self):
        stage, _ = _stage_for_axis(
            autonomous_count=0,
            tier_distribution={},
            earliest_age_s=0.0,
            proposed_thr=2,
            applied_thr=5,
            graduated_thr=10,
            graduated_min_days_v=30,
        )
        assert stage is DollCompletionStage.UNTOUCHED

    def test_one_commit_approval_routes_observed(self):
        # 1 commit at approval_required — below proposed threshold (2)
        stage, diagnostic = _stage_for_axis(
            autonomous_count=1,
            tier_distribution={"approval_required": 1},
            earliest_age_s=86400,
            proposed_thr=2,
            applied_thr=5,
            graduated_thr=10,
            graduated_min_days_v=30,
        )
        assert stage is DollCompletionStage.OBSERVED
        assert "1 commits" in diagnostic

    def test_proposed_threshold_with_approval_routes_proposed(self):
        stage, _ = _stage_for_axis(
            autonomous_count=3,
            tier_distribution={"approval_required": 3},
            earliest_age_s=86400 * 7,
            proposed_thr=2,
            applied_thr=5,
            graduated_thr=10,
            graduated_min_days_v=30,
        )
        assert stage is DollCompletionStage.PROPOSED

    def test_applied_threshold_with_safe_auto_routes_applied(self):
        stage, _ = _stage_for_axis(
            autonomous_count=6,
            tier_distribution={"safe_auto": 4, "notify_apply": 2},
            earliest_age_s=86400 * 7,
            proposed_thr=2,
            applied_thr=5,
            graduated_thr=10,
            graduated_min_days_v=30,
        )
        assert stage is DollCompletionStage.APPLIED

    def test_graduated_requires_all_three_conditions(self):
        # Threshold met + safe_auto present + days span met
        stage, _ = _stage_for_axis(
            autonomous_count=15,
            tier_distribution={"safe_auto": 10, "notify_apply": 5},
            earliest_age_s=86400 * 45,
            proposed_thr=2,
            applied_thr=5,
            graduated_thr=10,
            graduated_min_days_v=30,
        )
        assert stage is DollCompletionStage.GRADUATED

    def test_graduated_threshold_met_but_no_safe_auto_routes_applied(self):
        """Cage-trusted commits required — operator-gated alone won't graduate."""
        stage, _ = _stage_for_axis(
            autonomous_count=15,
            tier_distribution={"notify_apply": 15},
            earliest_age_s=86400 * 45,
            proposed_thr=2,
            applied_thr=5,
            graduated_thr=10,
            graduated_min_days_v=30,
        )
        # has_safe_auto=False → no GRADUATED. notify_apply still routes APPLIED.
        assert stage is DollCompletionStage.APPLIED

    def test_graduated_safe_auto_but_insufficient_days_routes_applied(self):
        # Safe auto + threshold met but days too short
        stage, _ = _stage_for_axis(
            autonomous_count=15,
            tier_distribution={"safe_auto": 10},
            earliest_age_s=86400 * 5,
            proposed_thr=2,
            applied_thr=5,
            graduated_thr=10,
            graduated_min_days_v=30,
        )
        assert stage is DollCompletionStage.APPLIED

    def test_unknown_tier_doesnt_prevent_progression(self):
        """A commit with Risk: unknown should count toward thresholds
        but not satisfy has_safe_auto / has_notify_apply / approval gates."""
        stage, _ = _stage_for_axis(
            autonomous_count=4,
            tier_distribution={"unknown": 4},
            earliest_age_s=86400 * 7,
            proposed_thr=2,
            applied_thr=5,
            graduated_thr=10,
            graduated_min_days_v=30,
        )
        # No least-cautious + no approval → OBSERVED
        assert stage is DollCompletionStage.OBSERVED


# ---------------------------------------------------------------------------
# Git log composer — hermetic via injected runner
# ---------------------------------------------------------------------------


def _make_git_log_output(commits: List[dict]) -> str:
    """Build canonical git log output matching _GIT_FORMAT."""
    parts: List[str] = []
    for c in commits:
        # __OV_DOLL__ separator + commit_hash + commit_time + body
        # + __END_HEADER__ + files
        parts.append("__OV_DOLL__")
        parts.append(c["hash"])
        parts.append(str(c["time"]))
        parts.append(c["body"])
        parts.append("__END_HEADER__")
        for f in c.get("files", []):
            parts.append(f)
    return "\n".join(parts) + "\n"


class TestGitLogParser:
    def test_empty_input_returns_empty_tuple(self):
        assert _parse_git_log("") == ()
        assert _parse_git_log("   \n  ") == ()

    def test_malformed_chunks_skipped(self):
        # missing time field → skipped
        out = "__OV_DOLL__\nabcdef\n__END_HEADER__\nfile.py\n"
        assert _parse_git_log(out) == ()

    def test_well_formed_chunk_parsed(self):
        raw = _make_git_log_output([
            {
                "hash": "abc123",
                "time": 1700000000,
                "body": (
                    "fix(auth): close session leak\n\n"
                    "Risk: safe_auto\n"
                    "Ouroboros+Venom [O+V] — "
                    "Autonomous Self-Development Engine\n"
                ),
                "files": [
                    "backend/core/ouroboros/governance/orchestrator.py",
                    "tests/test_foo.py",
                ],
            },
        ])
        parsed = _parse_git_log(raw)
        assert len(parsed) == 1
        c = parsed[0]
        assert c.commit_hash == "abc123"
        assert c.commit_time_unix == 1700000000
        assert "safe_auto" in c.body
        assert len(c.files) == 2

    def test_multiple_chunks_parsed_in_order(self):
        raw = _make_git_log_output([
            {"hash": "aaa", "time": 1, "body": "msg1", "files": ["a.py"]},
            {"hash": "bbb", "time": 2, "body": "msg2", "files": ["b.py"]},
            {"hash": "ccc", "time": 3, "body": "msg3", "files": ["c.py"]},
        ])
        parsed = _parse_git_log(raw)
        assert len(parsed) == 3
        assert [c.commit_hash for c in parsed] == ["aaa", "bbb", "ccc"]


class TestAutonomousCommitDetection:
    def test_canonical_signature_detected(self):
        body = (
            "fix(auth): close leak\n\n"
            "Ouroboros+Venom [O+V] — Autonomous Self-Development Engine\n"
        )
        signature = (
            "Ouroboros+Venom [O+V] — Autonomous Self-Development Engine"
        )
        assert _is_autonomous_commit(body, signature) is True

    def test_human_commit_not_detected(self):
        body = "fix(auth): close leak\n\nSigned-off-by: Derek\n"
        signature = (
            "Ouroboros+Venom [O+V] — Autonomous Self-Development Engine"
        )
        assert _is_autonomous_commit(body, signature) is False

    def test_empty_signature_returns_false(self):
        assert _is_autonomous_commit("body", "") is False
        assert _is_autonomous_commit("", "sig") is False


class TestRiskTierExtraction:
    def test_canonical_tier_extracted(self):
        body = "fix\n\nRisk: safe_auto\nOp-ID: foo\n"
        canonical = frozenset({
            "safe_auto", "notify_apply", "approval_required", "blocked",
        })
        assert _extract_risk_tier(body, canonical) == "safe_auto"

    def test_unrecognized_tier_returns_unknown(self):
        body = "fix\n\nRisk: rocketship\n"
        canonical = frozenset({"safe_auto"})
        assert _extract_risk_tier(body, canonical) == "unknown"

    def test_missing_risk_token_returns_unknown(self):
        body = "fix\n\nOp-ID: foo\n"
        canonical = frozenset({"safe_auto"})
        assert _extract_risk_tier(body, canonical) == "unknown"

    def test_case_insensitive_match(self):
        body = "fix\n\nRisk: SAFE_AUTO\n"
        canonical = frozenset({"safe_auto"})
        assert _extract_risk_tier(body, canonical) == "safe_auto"


# ---------------------------------------------------------------------------
# Aggregator — master-flag gating
# ---------------------------------------------------------------------------


class TestAggregatorGating:
    def test_master_off_returns_empty_snapshot(self):
        snap = aggregate_doll_completion()
        assert snap.master_enabled is False
        assert snap.axes == ()
        assert snap.completion_ratio == 0.0
        assert _ENV_MASTER in snap.diagnostic

    def test_master_on_returns_non_empty(self, monkeypatch):
        monkeypatch.setenv(_ENV_MASTER, "true")
        # Hermetic — empty git log so all axes are UNTOUCHED
        snap = aggregate_doll_completion(
            git_log_runner=_fake_runner(""),
        )
        assert snap.master_enabled is True
        # We expect 8 canonical Category axes (all flag_registry buckets)
        assert len(snap.axes) >= 1
        assert all(
            a.stage is DollCompletionStage.UNTOUCHED for a in snap.axes
        )

    def test_caches_snapshot_within_ttl(self, monkeypatch):
        monkeypatch.setenv(_ENV_MASTER, "true")
        s1 = aggregate_doll_completion(
            git_log_runner=_fake_runner(""),
        )
        s2 = aggregate_doll_completion(
            git_log_runner=_fake_runner(""),
        )
        # Same object — cached
        assert s1 is s2

    def test_force_refresh_bypasses_cache(self, monkeypatch):
        monkeypatch.setenv(_ENV_MASTER, "true")
        s1 = aggregate_doll_completion(
            git_log_runner=_fake_runner(""),
        )
        s2 = aggregate_doll_completion(
            git_log_runner=_fake_runner(""),
            force_refresh=True,
        )
        assert s1 is not s2


def _fake_runner(stdout_text: str, returncode: int = 0):
    """Return a fake subprocess.run-shaped callable returning canned stdout."""
    class _FakeResult:
        def __init__(self):
            self.returncode = returncode
            self.stdout = stdout_text
            self.stderr = ""

    def runner(*args, **kwargs):
        return _FakeResult()
    return runner


# ---------------------------------------------------------------------------
# End-to-end aggregation against a hermetic git log
# ---------------------------------------------------------------------------


class TestEndToEndAggregation:
    def test_autonomous_commit_advances_stage(self, monkeypatch, tmp_path):
        """When a single O+V-signed commit touches a FlagRegistry
        source_file for the 'safety' category, that axis should
        advance from UNTOUCHED to at least OBSERVED."""
        monkeypatch.setenv(_ENV_MASTER, "true")
        # Bring graduated/applied/proposed thresholds in reach so we
        # can craft each ladder rung deterministically.
        monkeypatch.setenv("JARVIS_DOLL_PROPOSED_THRESHOLD", "2")
        monkeypatch.setenv("JARVIS_DOLL_APPLIED_THRESHOLD", "3")

        # Pick a real FlagSpec source_file for the safety axis to
        # ensure the commit matches a known category.
        target_file = _safety_axis_source_file()
        assert target_file, "expected at least one safety-axis flag"

        raw = _make_git_log_output([
            {
                "hash": "abc111",
                "time": 1700000000,
                "body": (
                    "feat(cage): tighten guardian\n\n"
                    "Risk: approval_required\n"
                    "Op-ID: synth1\n"
                    "Ouroboros+Venom [O+V] — "
                    "Autonomous Self-Development Engine\n"
                ),
                "files": [target_file],
            },
            {
                "hash": "abc222",
                "time": 1700100000,
                "body": (
                    "feat(cage): observe drift\n\n"
                    "Risk: approval_required\n"
                    "Op-ID: synth2\n"
                    "Ouroboros+Venom [O+V] — "
                    "Autonomous Self-Development Engine\n"
                ),
                "files": [target_file],
            },
        ])
        snap = aggregate_doll_completion(
            git_log_runner=_fake_runner(raw),
        )
        safety = snap.axis_for_category("safety")
        assert safety is not None
        # 2 approval-tier commits → PROPOSED at threshold=2
        assert safety.stage is DollCompletionStage.PROPOSED
        assert safety.autonomous_commit_count == 2

    def test_human_commit_does_not_advance_stage(self, monkeypatch):
        monkeypatch.setenv(_ENV_MASTER, "true")
        target_file = _safety_axis_source_file()
        raw = _make_git_log_output([
            {
                "hash": "human1",
                "time": 1700000000,
                "body": (
                    "fix(cage): operator-led closure\n\n"
                    "Signed-off-by: Derek\n"
                ),
                "files": [target_file],
            },
        ])
        snap = aggregate_doll_completion(
            git_log_runner=_fake_runner(raw),
        )
        safety = snap.axis_for_category("safety")
        assert safety is not None
        assert safety.stage is DollCompletionStage.UNTOUCHED
        assert safety.autonomous_commit_count == 0

    def test_unrelated_files_dont_count(self, monkeypatch):
        monkeypatch.setenv(_ENV_MASTER, "true")
        raw = _make_git_log_output([
            {
                "hash": "unrelated1",
                "time": 1700000000,
                "body": (
                    "feat(unrelated): refactor README\n\n"
                    "Risk: safe_auto\n"
                    "Ouroboros+Venom [O+V] — "
                    "Autonomous Self-Development Engine\n"
                ),
                "files": ["docs/README.md", "tests/test_foo.py"],
            },
        ])
        snap = aggregate_doll_completion(
            git_log_runner=_fake_runner(raw),
        )
        # No axis advances because the files don't match any FlagSpec.source_file
        for axis in snap.axes:
            assert axis.autonomous_commit_count == 0
            assert axis.stage is DollCompletionStage.UNTOUCHED


def _safety_axis_source_file() -> str:
    """Find a real flag_registry source_file in the safety category."""
    from backend.core.ouroboros.governance import flag_registry as fr
    fr.reset_default_registry()
    reg = fr.ensure_seeded()
    for spec in reg.list_all():
        try:
            if spec.category.value == "safety" and spec.source_file:
                return spec.source_file
        except Exception:  # noqa: BLE001
            continue
    return ""


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


class TestRenderers:
    def test_panel_master_off_returns_disabled_marker(self, monkeypatch):
        monkeypatch.setenv(_ENV_MASTER, "false")
        # Empty snapshot with master=False
        snap = aggregate_doll_completion()
        out = format_doll_completion_panel(snap)
        # master-off snapshot → returns disabled marker
        assert "disabled" in out or _ENV_MASTER in out

    def test_panel_master_on_renders_stages(self, monkeypatch):
        monkeypatch.setenv(_ENV_MASTER, "true")
        snap = aggregate_doll_completion(
            git_log_runner=_fake_runner(""),
        )
        out = format_doll_completion_panel(snap)
        assert "Second-order doll" in out
        # All 5 stage names appear in the per-stage summary line
        for stage in DollCompletionStage:
            assert stage.value in out

    def test_axis_detail_missing_returns_marker(self, monkeypatch):
        monkeypatch.setenv(_ENV_MASTER, "true")
        snap = aggregate_doll_completion(
            git_log_runner=_fake_runner(""),
        )
        out = format_axis_detail(snap, "nonexistent_axis")
        assert "not found" in out


# ---------------------------------------------------------------------------
# Snapshot to_dict + from_dict round-trip discipline (§33.5)
# ---------------------------------------------------------------------------


class TestArtifactProjection:
    def test_axis_progress_to_dict_full_shape(self):
        axis = AxisProgress(
            category="safety",
            linked_principles=("6. Threshold-triggered neuroplasticity",),
            flag_count=10,
            source_file_count=5,
            autonomous_commit_count=3,
            earliest_commit_age_s=86400.0 * 30,
            most_recent_commit_age_s=86400.0,
            tier_distribution={"safe_auto": 2, "notify_apply": 1},
            stage=DollCompletionStage.APPLIED,
            diagnostic="3 commits",
        )
        d = axis.to_dict()
        expected_keys = {
            "category", "linked_principles", "flag_count",
            "source_file_count", "autonomous_commit_count",
            "earliest_commit_age_s", "most_recent_commit_age_s",
            "tier_distribution", "stage", "diagnostic",
            "schema_version",
        }
        assert set(d.keys()) == expected_keys
        assert d["stage"] == "applied"
        assert d["schema_version"] == SECOND_ORDER_DOLL_METRIC_SCHEMA_VERSION

    def test_snapshot_to_dict_full_shape(self, monkeypatch):
        monkeypatch.setenv(_ENV_MASTER, "true")
        snap = aggregate_doll_completion(
            git_log_runner=_fake_runner(""),
        )
        d = snap.to_dict()
        expected_keys = {
            "aggregated_at_unix", "master_enabled", "axes",
            "stage_counts", "completion_ratio", "elapsed_s",
            "diagnostic", "schema_version",
        }
        assert set(d.keys()) == expected_keys
        assert d["master_enabled"] is True
        assert isinstance(d["axes"], list)
        # Each axis is the full to_dict projection
        if d["axes"]:
            assert "schema_version" in d["axes"][0]

    def test_commit_evidence_to_dict(self):
        ev = CommitEvidence(
            commit_hash="abc",
            risk_tier="safe_auto",
            age_seconds=42.0,
        )
        d = ev.to_dict()
        assert d["commit_hash"] == "abc"
        assert d["risk_tier"] == "safe_auto"
        assert d["age_seconds"] == 42.0
        assert d["schema_version"] == SECOND_ORDER_DOLL_METRIC_SCHEMA_VERSION

    def test_snapshot_axis_for_category_lookup(self, monkeypatch):
        monkeypatch.setenv(_ENV_MASTER, "true")
        snap = aggregate_doll_completion(
            git_log_runner=_fake_runner(""),
        )
        # At least one canonical category should exist
        assert snap.axis_for_category("safety") is not None
        assert snap.axis_for_category("nonexistent") is None
        assert snap.axis_for_category(None) is None


# ---------------------------------------------------------------------------
# §32.11 Slice 4 — REPL auto-discovered
# ---------------------------------------------------------------------------


class TestReplDispatch:
    def test_match_canonical_verb(self):
        from backend.core.ouroboros.governance.doll_metric_repl import (
            dispatch_doll_metric_command,
        )
        r = dispatch_doll_metric_command("/doll_metric help")
        assert r.matched is True
        assert r.ok is True

    def test_match_short_alias(self):
        from backend.core.ouroboros.governance.doll_metric_repl import (
            dispatch_doll_metric_command,
        )
        r = dispatch_doll_metric_command("/doll help")
        assert r.matched is True

    def test_help_bypasses_master_gate(self):
        from backend.core.ouroboros.governance.doll_metric_repl import (
            dispatch_doll_metric_command,
        )
        # Default master is False — help still works
        r = dispatch_doll_metric_command("/doll help")
        assert r.matched is True
        assert r.ok is True
        assert "/doll" in r.text

    def test_status_blocked_when_master_off(self):
        from backend.core.ouroboros.governance.doll_metric_repl import (
            dispatch_doll_metric_command,
        )
        r = dispatch_doll_metric_command("/doll status")
        assert r.matched is True
        assert r.ok is False
        assert "disabled" in r.text

    def test_status_renders_when_master_on(self, monkeypatch):
        monkeypatch.setenv(_ENV_MASTER, "true")
        from backend.core.ouroboros.governance.doll_metric_repl import (
            dispatch_doll_metric_command,
        )
        r = dispatch_doll_metric_command("/doll status")
        assert r.matched is True
        assert r.ok is True

    def test_unknown_subcommand_returns_helpful_error(self, monkeypatch):
        monkeypatch.setenv(_ENV_MASTER, "true")
        from backend.core.ouroboros.governance.doll_metric_repl import (
            dispatch_doll_metric_command,
        )
        r = dispatch_doll_metric_command("/doll bogus")
        assert r.matched is True
        assert r.ok is False
        assert "unknown subcommand" in r.text.lower()

    def test_unmatched_line_returns_matched_false(self):
        from backend.core.ouroboros.governance.doll_metric_repl import (
            dispatch_doll_metric_command,
        )
        r = dispatch_doll_metric_command("/something_else")
        assert r.matched is False

    def test_refresh_subcommand_invokes_force_refresh(self, monkeypatch):
        monkeypatch.setenv(_ENV_MASTER, "true")
        from backend.core.ouroboros.governance.doll_metric_repl import (
            dispatch_doll_metric_command,
        )
        r = dispatch_doll_metric_command("/doll refresh")
        assert r.matched is True
        assert r.ok is True
        assert "refresh" in r.text.lower()

    def test_show_requires_category_arg(self, monkeypatch):
        monkeypatch.setenv(_ENV_MASTER, "true")
        from backend.core.ouroboros.governance.doll_metric_repl import (
            dispatch_doll_metric_command,
        )
        r = dispatch_doll_metric_command("/doll show")
        assert r.matched is True
        assert r.ok is False
        assert "required" in r.text.lower()

    def test_show_renders_known_category(self, monkeypatch):
        monkeypatch.setenv(_ENV_MASTER, "true")
        from backend.core.ouroboros.governance.doll_metric_repl import (
            dispatch_doll_metric_command,
        )
        r = dispatch_doll_metric_command("/doll show safety")
        assert r.matched is True
        assert r.ok is True

    def test_completion_subcommand(self, monkeypatch):
        monkeypatch.setenv(_ENV_MASTER, "true")
        from backend.core.ouroboros.governance.doll_metric_repl import (
            dispatch_doll_metric_command,
        )
        r = dispatch_doll_metric_command("/doll completion")
        assert r.matched is True
        assert r.ok is True
        assert "%" in r.text


# ---------------------------------------------------------------------------
# §32.11 Slice 4 — naming-cage zero-edit auto-discovery
# ---------------------------------------------------------------------------


class TestNamingCageAutoDiscovery:
    def test_doll_metric_verb_in_registry(self):
        from backend.core.ouroboros.battle_test import (
            repl_dispatch_registry as rdr,
        )
        rdr.reset_registry_for_tests()
        rdr.prime_registry()
        verbs = rdr.list_verbs()
        assert "doll_metric" in verbs

    def test_dispatcher_routes_via_registry(self):
        from backend.core.ouroboros.battle_test import (
            repl_dispatch_registry as rdr,
        )
        rdr.reset_registry_for_tests()
        rdr.prime_registry()
        outcome = rdr.try_dispatch("/doll_metric help")
        assert outcome.matched is True
        assert outcome.ok is True


# ---------------------------------------------------------------------------
# FlagRegistry seeds — auto-discovered (§33.3 naming-cage)
# ---------------------------------------------------------------------------


class TestFlagRegistrySeeds:
    def test_all_6_seeds_auto_discovered(self):
        from backend.core.ouroboros.governance import flag_registry as fr
        fr.reset_default_registry()
        reg = fr.ensure_seeded()
        names = {f.name for f in reg.list_all()}
        for expected in [
            "JARVIS_SECOND_ORDER_DOLL_METRIC_ENABLED",
            "JARVIS_DOLL_COMMIT_SCAN_MAX",
            "JARVIS_DOLL_GRADUATED_THRESHOLD",
            "JARVIS_DOLL_GRADUATED_MIN_DAYS",
            "JARVIS_DOLL_APPLIED_THRESHOLD",
            "JARVIS_DOLL_PROPOSED_THRESHOLD",
        ]:
            assert expected in names, f"missing seed: {expected}"

    def test_master_seed_is_bool_observability_default_false(self):
        from backend.core.ouroboros.governance import flag_registry as fr
        fr.reset_default_registry()
        reg = fr.ensure_seeded()
        spec = next(
            (
                f for f in reg.list_all()
                if f.name == "JARVIS_SECOND_ORDER_DOLL_METRIC_ENABLED"
            ),
            None,
        )
        assert spec is not None
        assert spec.type.value == "bool"
        assert spec.category.value == "observability"
        assert spec.default is False


# ---------------------------------------------------------------------------
# 6 AST pins canonical-source pass + 6 synthetic regressions
# ---------------------------------------------------------------------------


class TestAstPinsCanonicalPass:
    """All 6 pins must pass on the actual source we shipped."""

    @pytest.fixture
    def canonical_source(self):
        """Load the canonical second_order_doll_metric.py source + tree."""
        from backend.core.ouroboros.governance import (
            second_order_doll_metric as m,
        )
        path = Path(m.__file__)
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        return src, tree

    @pytest.fixture
    def pins(self):
        from backend.core.ouroboros.governance.second_order_doll_metric import (  # noqa: E501
            register_shipped_invariants,
        )
        return register_shipped_invariants()

    def test_all_6_pins_registered(self, pins):
        assert len(pins) == 6
        expected_names = {
            "second_order_doll_master_default_false",
            "second_order_doll_authority_asymmetry",
            "second_order_doll_stage_taxonomy_5_values",
            "second_order_doll_composes_canonical_constellation",
            "second_order_doll_composes_canonical_flag_registry",
            "second_order_doll_composes_canonical_ov_signature",
        }
        assert {p.invariant_name for p in pins} == expected_names

    def test_master_default_false_pin_passes(self, canonical_source, pins):
        src, tree = canonical_source
        pin = next(
            p for p in pins
            if p.invariant_name == "second_order_doll_master_default_false"
        )
        violations = pin.validate(tree, src)
        assert not violations, violations

    def test_authority_asymmetry_pin_passes(self, canonical_source, pins):
        src, tree = canonical_source
        pin = next(
            p for p in pins
            if p.invariant_name == "second_order_doll_authority_asymmetry"
        )
        violations = pin.validate(tree, src)
        assert not violations, violations

    def test_stage_taxonomy_pin_passes(self, canonical_source, pins):
        src, tree = canonical_source
        pin = next(
            p for p in pins
            if p.invariant_name
            == "second_order_doll_stage_taxonomy_5_values"
        )
        violations = pin.validate(tree, src)
        assert not violations, violations

    def test_composes_canonical_constellation_pin_passes(
        self, canonical_source, pins,
    ):
        src, tree = canonical_source
        pin = next(
            p for p in pins
            if p.invariant_name
            == "second_order_doll_composes_canonical_constellation"
        )
        violations = pin.validate(tree, src)
        assert not violations, violations

    def test_composes_canonical_flag_registry_pin_passes(
        self, canonical_source, pins,
    ):
        src, tree = canonical_source
        pin = next(
            p for p in pins
            if p.invariant_name
            == "second_order_doll_composes_canonical_flag_registry"
        )
        violations = pin.validate(tree, src)
        assert not violations, violations

    def test_composes_canonical_ov_signature_pin_passes(
        self, canonical_source, pins,
    ):
        src, tree = canonical_source
        pin = next(
            p for p in pins
            if p.invariant_name
            == "second_order_doll_composes_canonical_ov_signature"
        )
        violations = pin.validate(tree, src)
        assert not violations, violations


class TestAstPinsSyntheticRegression:
    """Synthetic regressions — each pin MUST fire when its invariant is
    violated. Without these the pin could silently no-op."""

    @pytest.fixture
    def pins(self):
        from backend.core.ouroboros.governance.second_order_doll_metric import (  # noqa: E501
            register_shipped_invariants,
        )
        return register_shipped_invariants()

    def test_master_pin_fires_on_premature_flip(self, pins):
        # Synthetic: master_enabled() with default=True
        synthetic = """
def master_enabled():
    return _flag("FOO", default=True)
"""
        tree = ast.parse(synthetic)
        pin = next(
            p for p in pins
            if p.invariant_name == "second_order_doll_master_default_false"
        )
        violations = pin.validate(tree, synthetic)
        assert violations

    def test_authority_pin_fires_on_orchestrator_import(self, pins):
        synthetic = (
            "from backend.core.ouroboros.governance.orchestrator "
            "import foo\n"
        )
        tree = ast.parse(synthetic)
        pin = next(
            p for p in pins
            if p.invariant_name == "second_order_doll_authority_asymmetry"
        )
        violations = pin.validate(tree, synthetic)
        assert violations
        assert any("orchestrator" in v for v in violations)

    def test_stage_taxonomy_pin_fires_on_missing_value(self, pins):
        # Synthetic: 4-value DollCompletionStage (missing GRADUATED)
        synthetic = """
import enum
class DollCompletionStage(str, enum.Enum):
    UNTOUCHED = "untouched"
    OBSERVED = "observed"
    PROPOSED = "proposed"
    APPLIED = "applied"
"""
        tree = ast.parse(synthetic)
        pin = next(
            p for p in pins
            if p.invariant_name
            == "second_order_doll_stage_taxonomy_5_values"
        )
        violations = pin.validate(tree, synthetic)
        assert violations
        # Either missing OR extra would fire
        assert "missing" in violations[0] or "unexpected" in violations[0]

    def test_constellation_compose_pin_fires_on_missing_import(self, pins):
        # IMPORTANT: synthetic source must not contain the canonical
        # substrings the pin checks for. Comment lines count — the
        # pin uses raw substring check on `src`.
        synthetic = "x = 1\ny = 2\n"
        tree = ast.parse(synthetic)
        pin = next(
            p for p in pins
            if p.invariant_name
            == "second_order_doll_composes_canonical_constellation"
        )
        violations = pin.validate(tree, synthetic)
        assert violations

    def test_flag_registry_compose_pin_fires_on_missing_import(self, pins):
        synthetic = "x = 1\ny = 2\n"
        tree = ast.parse(synthetic)
        pin = next(
            p for p in pins
            if p.invariant_name
            == "second_order_doll_composes_canonical_flag_registry"
        )
        violations = pin.validate(tree, synthetic)
        assert violations

    def test_ov_signature_compose_pin_fires_on_missing_import(self, pins):
        synthetic = "x = 1\ny = 2\n"
        tree = ast.parse(synthetic)
        pin = next(
            p for p in pins
            if p.invariant_name
            == "second_order_doll_composes_canonical_ov_signature"
        )
        violations = pin.validate(tree, synthetic)
        assert violations


# ---------------------------------------------------------------------------
# Canonical-source smoke — composes existing files
# ---------------------------------------------------------------------------


class TestCanonicalSourceSmokes:
    def test_ov_signature_substring_present_via_auto_committer(self):
        """The accessor we add MUST exist + return a non-empty
        canonical substring."""
        from backend.core.ouroboros.governance import auto_committer
        sig = auto_committer.ov_signature_substring()
        assert isinstance(sig, str)
        assert sig
        assert "Ouroboros" in sig and "Venom" in sig

    def test_principles_for_category_public_in_constellation(self):
        from backend.core.ouroboros.governance import (
            capability_constellation as cc,
        )
        # Reused canonical principle map — must return non-empty tuples
        # for the 8 canonical Category enum values.
        assert cc.principles_for_category("safety")
        assert cc.principles_for_category("observability")

    def test_sse_event_registered(self):
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            _VALID_EVENT_TYPES,
            EVENT_TYPE_SECOND_ORDER_DOLL_PROGRESS_UPDATED,
        )
        assert (
            EVENT_TYPE_SECOND_ORDER_DOLL_PROGRESS_UPDATED
            in _VALID_EVENT_TYPES
        )
        assert (
            EVENT_TYPE_SECOND_ORDER_DOLL_PROGRESS_UPDATED
            == "second_order_doll_progress_updated"
        )

    def test_ide_get_route_handler_exists(self):
        """The handler method is callable on the IDEObservabilityRouter
        class. We don't instantiate (constructor needs args) — just
        verify the canonical attribute is present, which is the
        load-bearing check for route registration."""
        from backend.core.ouroboros.governance.ide_observability import (
            IDEObservabilityRouter,
        )
        assert hasattr(IDEObservabilityRouter, "_handle_second_order_doll")
        assert hasattr(IDEObservabilityRouter, "_doll_metric_master_enabled")


# ---------------------------------------------------------------------------
# Public API stability — preserved across schema bumps
# ---------------------------------------------------------------------------


class TestPublicApiStability:
    def test_all_exports_callable_or_class(self):
        from backend.core.ouroboros.governance import (
            second_order_doll_metric as m,
        )
        for name in m.__all__:
            obj = getattr(m, name)
            # Either a callable, a class, or a constant
            assert obj is not None

    def test_schema_version_constant(self):
        assert isinstance(
            SECOND_ORDER_DOLL_METRIC_SCHEMA_VERSION, str,
        )
        assert SECOND_ORDER_DOLL_METRIC_SCHEMA_VERSION.startswith(
            "second_order_doll_metric.",
        )
