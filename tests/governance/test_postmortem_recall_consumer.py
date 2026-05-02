"""Priority #2 Slice 4 — Recurrence consumer regression tests.

Coverage:

  * **Sub-gate flag** — asymmetric env semantics, default false.
  * **Env knob clamps** — TTL hours + max boost count enforce
    floor + ceiling + garbage fallback.
  * **5-value RecurrenceBoostStatus closed taxonomy pin**.
  * **Failure-class regex extraction** — valid Python repr +
    malformed + empty.
  * **compute_recurrence_boosts** — empty input, single advisory,
    TTL decay (old advisory excluded), wrong action/kind filter,
    max_count clamp, multi-class grouping.
  * **MonotonicTighteningVerdict.PASSED stamping** — every
    emitted boost carries canonical Phase C string.
  * **compute_effective_top_k** — no boost / matched class /
    None-target-takes-max / ceiling clamp / expired-boost-
    ignored / defensive fallback.
  * **get_active_recurrence_boosts** — disabled / master-off /
    enabled+e2e + read-failure-defensive.
  * **Frozen RecurrenceBoost** — to_dict round-trip + is_active
    helper.
  * **Authority invariants** — AST-pinned: governance allowlist
    + MUST reference MonotonicTighteningVerdict +
    read_coherence_advisories + INJECT_POSTMORTEM_RECALL_HINT
    + no orchestrator + no eval-family + no async.
"""
from __future__ import annotations

import ast
import os
import tempfile
import time
from pathlib import Path
from unittest import mock

import pytest

from backend.core.ouroboros.governance.adaptation.ledger import (
    MonotonicTighteningVerdict,
)
from backend.core.ouroboros.governance.verification.coherence_action_bridge import (
    CoherenceAdvisory,
    CoherenceAdvisoryAction,
    TighteningProposalStatus,
    record_coherence_advisory,
)
from backend.core.ouroboros.governance.verification.coherence_auditor import (
    BehavioralDriftKind,
    DriftSeverity,
)
from backend.core.ouroboros.governance.verification.postmortem_recall_consumer import (
    POSTMORTEM_RECALL_CONSUMER_SCHEMA_VERSION,
    RecurrenceBoost,
    RecurrenceBoostStatus,
    boost_max_count,
    boost_ttl_hours,
    compute_effective_top_k,
    compute_recurrence_boosts,
    get_active_recurrence_boosts,
    postmortem_recurrence_boost_enabled,
)
from backend.core.ouroboros.governance.verification.postmortem_recall_consumer import (  # noqa: E501
    _FAILURE_CLASS_RE,
    _extract_failure_class,
)


_FORBIDDEN_CALL_TOKENS = ("e" + "val(", "e" + "xec(")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_advisory_path():
    d = Path(tempfile.mkdtemp(prefix="pmcons_test_")).resolve()
    yield d / "coherence_advisory.jsonl"
    import shutil
    shutil.rmtree(d, ignore_errors=True)


def _make_recurrence_advisory(
    *,
    advisory_id: str = "adv-1",
    failure_class: str = "test_failure",
    count: int = 5,
    recorded_at_ts: float = 0.0,
) -> CoherenceAdvisory:
    if recorded_at_ts == 0.0:
        recorded_at_ts = time.time()
    detail = (
        f"failure_class {failure_class!r} appeared {count} "
        f"times > budget 3"
    )
    return CoherenceAdvisory(
        advisory_id=advisory_id,
        drift_signature=f"sig-{advisory_id}",
        drift_kind=BehavioralDriftKind.RECURRENCE_DRIFT,
        action=(
            CoherenceAdvisoryAction.INJECT_POSTMORTEM_RECALL_HINT
        ),
        severity=DriftSeverity.HIGH,
        detail=detail,
        recorded_at_ts=recorded_at_ts,
        tightening_status=(
            TighteningProposalStatus.NEUTRAL_NOTIFICATION
        ),
    )


# ---------------------------------------------------------------------------
# 1. Sub-gate flag
# ---------------------------------------------------------------------------


class TestSubGateFlag:
    def test_default_is_true_post_graduation(self):
        os.environ.pop(
            "JARVIS_POSTMORTEM_RECURRENCE_BOOST_ENABLED", None,
        )
        assert postmortem_recurrence_boost_enabled() is True

    @pytest.mark.parametrize(
        "v", ["1", "true", "yes", "on"],
    )
    def test_truthy(self, v):
        with mock.patch.dict(
            os.environ,
            {"JARVIS_POSTMORTEM_RECURRENCE_BOOST_ENABLED": v},
        ):
            assert postmortem_recurrence_boost_enabled() is True

    @pytest.mark.parametrize("v", ["0", "false", "no"])
    def test_falsy(self, v):
        with mock.patch.dict(
            os.environ,
            {"JARVIS_POSTMORTEM_RECURRENCE_BOOST_ENABLED": v},
        ):
            assert postmortem_recurrence_boost_enabled() is False


# ---------------------------------------------------------------------------
# 2. Env knob clamps
# ---------------------------------------------------------------------------


class TestEnvKnobs:
    def test_ttl_hours_default(self):
        os.environ.pop(
            "JARVIS_POSTMORTEM_RECALL_BOOST_TTL_HOURS", None,
        )
        assert boost_ttl_hours() == 6.0

    def test_ttl_hours_floor(self):
        with mock.patch.dict(
            os.environ,
            {
                "JARVIS_POSTMORTEM_RECALL_BOOST_TTL_HOURS":
                    "0.1",
            },
        ):
            assert boost_ttl_hours() == 1.0

    def test_ttl_hours_ceiling(self):
        with mock.patch.dict(
            os.environ,
            {
                "JARVIS_POSTMORTEM_RECALL_BOOST_TTL_HOURS":
                    "9999",
            },
        ):
            assert boost_ttl_hours() == 168.0

    def test_max_count_default(self):
        os.environ.pop(
            "JARVIS_POSTMORTEM_RECALL_BOOST_MAX_COUNT", None,
        )
        assert boost_max_count() == 5

    def test_max_count_floor(self):
        with mock.patch.dict(
            os.environ,
            {"JARVIS_POSTMORTEM_RECALL_BOOST_MAX_COUNT": "0"},
        ):
            assert boost_max_count() == 1

    def test_max_count_ceiling(self):
        with mock.patch.dict(
            os.environ,
            {
                "JARVIS_POSTMORTEM_RECALL_BOOST_MAX_COUNT":
                    "9999",
            },
        ):
            assert boost_max_count() == 20

    def test_garbage_falls_back(self):
        with mock.patch.dict(
            os.environ,
            {
                "JARVIS_POSTMORTEM_RECALL_BOOST_TTL_HOURS":
                    "garbage",
            },
        ):
            assert boost_ttl_hours() == 6.0


# ---------------------------------------------------------------------------
# 3. Closed taxonomy pin
# ---------------------------------------------------------------------------


class TestClosedTaxonomy:
    def test_5_values(self):
        assert len(list(RecurrenceBoostStatus)) == 5

    def test_values(self):
        expected = {
            "active", "expired", "disabled",
            "no_advisory", "failed",
        }
        assert {
            s.value for s in RecurrenceBoostStatus
        } == expected


# ---------------------------------------------------------------------------
# 4. Failure-class regex extraction
# ---------------------------------------------------------------------------


class TestFailureClassExtraction:
    def test_python_repr_format(self):
        detail = (
            "failure_class 'timeout_failure' appeared 5 times "
            "> budget 3"
        )
        assert _extract_failure_class(detail) == "timeout_failure"

    def test_no_match_returns_empty(self):
        assert _extract_failure_class("garbage") == ""

    def test_empty_returns_empty(self):
        assert _extract_failure_class("") == ""

    def test_underscore_in_failure_class(self):
        detail = "failure_class 'a_b_c_d' appeared 1 time"
        assert _extract_failure_class(detail) == "a_b_c_d"

    def test_regex_compiled(self):
        # Pin: regex is module-level and compiled (not built per-call)
        assert _FAILURE_CLASS_RE.search(
            "failure_class 'x' appeared",
        ) is not None


# ---------------------------------------------------------------------------
# 5. compute_recurrence_boosts
# ---------------------------------------------------------------------------


class TestComputeRecurrenceBoosts:
    def test_empty_input_returns_empty(self):
        assert dict(compute_recurrence_boosts([])) == {}

    def test_none_input_returns_empty(self):
        assert dict(
            compute_recurrence_boosts(None),  # type: ignore[arg-type]
        ) == {}

    def test_single_advisory_emits_boost(self):
        ts = time.time()
        adv = _make_recurrence_advisory(
            failure_class="test", recorded_at_ts=ts,
        )
        boosts = compute_recurrence_boosts([adv], now_ts=ts)
        assert "test" in boosts
        b = boosts["test"]
        assert b.failure_class == "test"
        assert b.boost_count == 1
        assert b.expires_at > ts

    def test_monotonic_verdict_stamped_passed(self):
        ts = time.time()
        adv = _make_recurrence_advisory(recorded_at_ts=ts)
        boosts = compute_recurrence_boosts([adv], now_ts=ts)
        b = list(boosts.values())[0]
        assert b.monotonic_tightening_verdict == (
            MonotonicTighteningVerdict.PASSED.value
        )

    def test_ttl_filter_excludes_old(self):
        ts = time.time()
        old_adv = _make_recurrence_advisory(
            recorded_at_ts=ts - 86400 * 2,  # 2 days old
        )
        boosts = compute_recurrence_boosts(
            [old_adv], ttl_hours=6.0, now_ts=ts,
        )
        assert dict(boosts) == {}

    def test_wrong_action_filtered(self):
        ts = time.time()
        wrong = CoherenceAdvisory(
            advisory_id="x", drift_signature="s",
            drift_kind=BehavioralDriftKind.RECURRENCE_DRIFT,
            # Wrong action — TIGHTEN_RISK_BUDGET
            action=(
                CoherenceAdvisoryAction.TIGHTEN_RISK_BUDGET
            ),
            severity=DriftSeverity.HIGH,
            detail="failure_class 'x' appeared 5",
            recorded_at_ts=ts,
            tightening_status=TighteningProposalStatus.PASSED,
        )
        boosts = compute_recurrence_boosts([wrong], now_ts=ts)
        assert dict(boosts) == {}

    def test_wrong_kind_filtered(self):
        ts = time.time()
        wrong = CoherenceAdvisory(
            advisory_id="x", drift_signature="s",
            # Wrong kind — POSTURE_LOCKED
            drift_kind=BehavioralDriftKind.POSTURE_LOCKED,
            action=(
                CoherenceAdvisoryAction
                .INJECT_POSTMORTEM_RECALL_HINT
            ),
            severity=DriftSeverity.HIGH,
            detail="failure_class 'x' appeared 5",
            recorded_at_ts=ts,
            tightening_status=(
                TighteningProposalStatus.NEUTRAL_NOTIFICATION
            ),
        )
        boosts = compute_recurrence_boosts([wrong], now_ts=ts)
        assert dict(boosts) == {}

    def test_max_count_clamp(self, monkeypatch):
        # Synthetic failure_class — extend the known-classes registry
        # so the Vector 3 plausibility gate accepts it.
        monkeypatch.setenv("JARVIS_KNOWN_FAILURE_CLASSES", "x")
        ts = time.time()
        # 20 advisories for same failure_class
        advs = [
            _make_recurrence_advisory(
                advisory_id=f"adv-{i}",
                failure_class="x",
                recorded_at_ts=ts - i * 60,
            )
            for i in range(20)
        ]
        boosts = compute_recurrence_boosts(
            advs, max_count=5, now_ts=ts,
        )
        assert boosts["x"].boost_count == 5

    def test_multi_class_grouping(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_KNOWN_FAILURE_CLASSES", "class_a,class_b",
        )
        ts = time.time()
        advs = [
            _make_recurrence_advisory(
                advisory_id=f"a-{i}",
                failure_class="class_a",
                recorded_at_ts=ts - i * 60,
            )
            for i in range(3)
        ] + [
            _make_recurrence_advisory(
                advisory_id=f"b-{i}",
                failure_class="class_b",
                recorded_at_ts=ts - i * 60,
            )
            for i in range(2)
        ]
        boosts = compute_recurrence_boosts(advs, now_ts=ts)
        assert set(boosts.keys()) == {"class_a", "class_b"}
        assert boosts["class_a"].boost_count == 3
        assert boosts["class_b"].boost_count == 2

    def test_advisory_with_unparseable_detail_skipped(self):
        ts = time.time()
        bad = CoherenceAdvisory(
            advisory_id="bad", drift_signature="s",
            drift_kind=BehavioralDriftKind.RECURRENCE_DRIFT,
            action=(
                CoherenceAdvisoryAction
                .INJECT_POSTMORTEM_RECALL_HINT
            ),
            severity=DriftSeverity.HIGH,
            detail="garbage detail no failure_class here",
            recorded_at_ts=ts,
            tightening_status=(
                TighteningProposalStatus.NEUTRAL_NOTIFICATION
            ),
        )
        boosts = compute_recurrence_boosts([bad], now_ts=ts)
        assert dict(boosts) == {}

    def test_garbage_advisory_skipped(self):
        ts = time.time()
        boosts = compute_recurrence_boosts(
            ["not an advisory", 42, None],  # type: ignore[list-item]
            now_ts=ts,
        )
        assert dict(boosts) == {}

    def test_expires_at_uses_newest_advisory(self):
        ts = time.time()
        old_adv = _make_recurrence_advisory(
            advisory_id="old", recorded_at_ts=ts - 1800,  # 30m ago
        )
        new_adv = _make_recurrence_advisory(
            advisory_id="new", recorded_at_ts=ts - 60,  # 1m ago
        )
        boosts = compute_recurrence_boosts(
            [old_adv, new_adv], ttl_hours=6.0, now_ts=ts,
        )
        b = boosts["test_failure"]
        # expires_at = newest (ts-60) + 6h
        expected = (ts - 60) + 6 * 3600
        assert abs(b.expires_at - expected) < 1.0


# ---------------------------------------------------------------------------
# 6. compute_effective_top_k
# ---------------------------------------------------------------------------


class TestComputeEffectiveTopK:
    def test_no_boost_returns_base(self):
        eff = compute_effective_top_k({}, base_top_k=3)
        assert eff == 3

    def test_matched_failure_class_extends_top_k(self):
        ts = time.time()
        boosts = {
            "test": RecurrenceBoost(
                failure_class="test", boost_count=4,
                expires_at=ts + 3600, source_advisory_id="x",
            ),
        }
        eff = compute_effective_top_k(
            boosts, base_top_k=3,
            target_failure_class="test", now_ts=ts,
        )
        assert eff == 7

    def test_unmatched_failure_class_returns_base(self):
        ts = time.time()
        boosts = {
            "test": RecurrenceBoost(
                failure_class="test", boost_count=4,
                expires_at=ts + 3600, source_advisory_id="x",
            ),
        }
        eff = compute_effective_top_k(
            boosts, base_top_k=3,
            target_failure_class="other", now_ts=ts,
        )
        assert eff == 3

    def test_none_target_takes_max_active_boost(self):
        ts = time.time()
        boosts = {
            "a": RecurrenceBoost(
                failure_class="a", boost_count=2,
                expires_at=ts + 3600, source_advisory_id="x",
            ),
            "b": RecurrenceBoost(
                failure_class="b", boost_count=5,
                expires_at=ts + 3600, source_advisory_id="y",
            ),
        }
        eff = compute_effective_top_k(
            boosts, base_top_k=3,
            target_failure_class=None, now_ts=ts,
        )
        assert eff == 8

    def test_ceiling_clamp(self):
        ts = time.time()
        boosts = {
            "x": RecurrenceBoost(
                failure_class="x", boost_count=999,
                expires_at=ts + 3600, source_advisory_id="y",
            ),
        }
        eff = compute_effective_top_k(
            boosts, base_top_k=3,
            target_failure_class="x", now_ts=ts,
        )
        # Default ceiling = 10
        assert eff == 10

    def test_expired_boost_ignored(self):
        ts = time.time()
        boosts = {
            "x": RecurrenceBoost(
                failure_class="x", boost_count=5,
                expires_at=ts - 3600,  # already expired
                source_advisory_id="y",
            ),
        }
        eff = compute_effective_top_k(
            boosts, base_top_k=3,
            target_failure_class="x", now_ts=ts,
        )
        assert eff == 3

    def test_zero_base_clamped_to_one(self):
        eff = compute_effective_top_k(
            {}, base_top_k=0,
        )
        assert eff >= 1

    def test_all_expired_with_none_target(self):
        ts = time.time()
        boosts = {
            "x": RecurrenceBoost(
                failure_class="x", boost_count=5,
                expires_at=ts - 3600, source_advisory_id="y",
            ),
        }
        eff = compute_effective_top_k(
            boosts, base_top_k=3,
            target_failure_class=None, now_ts=ts,
        )
        assert eff == 3


# ---------------------------------------------------------------------------
# 7. get_active_recurrence_boosts (high-level entry)
# ---------------------------------------------------------------------------


class TestGetActiveRecurrenceBoosts:
    def test_disabled_returns_empty(self):
        os.environ.pop(
            "JARVIS_POSTMORTEM_RECURRENCE_BOOST_ENABLED", None,
        )
        assert (
            dict(get_active_recurrence_boosts()) == {}
        )

    def test_master_off_returns_empty(self, tmp_advisory_path):
        with mock.patch.dict(
            os.environ,
            {
                "JARVIS_POSTMORTEM_RECURRENCE_BOOST_ENABLED":
                    "true",
                # master flag NOT set → defaults false
            },
        ):
            os.environ.pop(
                "JARVIS_POSTMORTEM_RECALL_ENABLED", None,
            )
            assert (
                dict(get_active_recurrence_boosts(
                    advisory_path=tmp_advisory_path,
                )) == {}
            )

    def test_e2e_read_and_compute(self, tmp_advisory_path):
        ts = time.time()
        adv = _make_recurrence_advisory(
            failure_class="real_class", recorded_at_ts=ts,
        )
        # Persist via Priority #1 Slice 4's writer
        with mock.patch.dict(
            os.environ,
            {
                "JARVIS_COHERENCE_ACTION_BRIDGE_ENABLED":
                    "true",
            },
        ):
            record_coherence_advisory(
                adv, path=tmp_advisory_path,
            )
        # Read via consumer — extend the known-classes registry so
        # the synthetic "real_class" passes the Vector 3 plausibility
        # gate.
        with mock.patch.dict(
            os.environ,
            {
                "JARVIS_POSTMORTEM_RECALL_ENABLED": "true",
                "JARVIS_POSTMORTEM_RECURRENCE_BOOST_ENABLED":
                    "true",
                "JARVIS_KNOWN_FAILURE_CLASSES": "real_class",
            },
        ):
            boosts = get_active_recurrence_boosts(
                advisory_path=tmp_advisory_path, now_ts=ts,
            )
        assert "real_class" in boosts

    def test_missing_advisory_path_returns_empty(self):
        with mock.patch.dict(
            os.environ,
            {
                "JARVIS_POSTMORTEM_RECALL_ENABLED": "true",
                "JARVIS_POSTMORTEM_RECURRENCE_BOOST_ENABLED":
                    "true",
            },
        ):
            assert (
                dict(get_active_recurrence_boosts(
                    advisory_path=Path("/nonexistent/path.jsonl"),
                )) == {}
            )

    def test_read_failure_returns_empty(self, tmp_advisory_path):
        with mock.patch.dict(
            os.environ,
            {
                "JARVIS_POSTMORTEM_RECALL_ENABLED": "true",
                "JARVIS_POSTMORTEM_RECURRENCE_BOOST_ENABLED":
                    "true",
            },
        ):
            with mock.patch(
                "backend.core.ouroboros.governance.verification."
                "postmortem_recall_consumer.read_coherence_advisories",
                side_effect=RuntimeError("disk error"),
            ):
                assert (
                    dict(get_active_recurrence_boosts(
                        advisory_path=tmp_advisory_path,
                    )) == {}
                )


# ---------------------------------------------------------------------------
# 8. RecurrenceBoost dataclass
# ---------------------------------------------------------------------------


class TestRecurrenceBoost:
    def test_frozen(self):
        b = RecurrenceBoost(
            failure_class="x", boost_count=3,
            expires_at=time.time() + 3600,
            source_advisory_id="y",
        )
        with pytest.raises((AttributeError, Exception)):
            b.boost_count = 999  # type: ignore[misc]

    def test_to_dict_round_trip(self):
        ts = time.time()
        b = RecurrenceBoost(
            failure_class="x", boost_count=3,
            expires_at=ts + 3600, source_advisory_id="adv",
        )
        d = b.to_dict()
        assert d["failure_class"] == "x"
        assert d["boost_count"] == 3
        assert d["expires_at"] == ts + 3600
        assert d["source_advisory_id"] == "adv"
        assert d["monotonic_tightening_verdict"] == (
            MonotonicTighteningVerdict.PASSED.value
        )

    def test_is_active_in_window(self):
        ts = time.time()
        b = RecurrenceBoost(
            failure_class="x", boost_count=3,
            expires_at=ts + 3600, source_advisory_id="y",
        )
        assert b.is_active(now_ts=ts) is True

    def test_is_active_after_expiry(self):
        ts = time.time()
        b = RecurrenceBoost(
            failure_class="x", boost_count=3,
            expires_at=ts - 3600, source_advisory_id="y",
        )
        assert b.is_active(now_ts=ts) is False


# ---------------------------------------------------------------------------
# 9. Schema integrity
# ---------------------------------------------------------------------------


class TestSchemaIntegrity:
    def test_schema_version_stable(self):
        assert (
            POSTMORTEM_RECALL_CONSUMER_SCHEMA_VERSION
            == "postmortem_recall_consumer.1"
        )


# ---------------------------------------------------------------------------
# 10. Authority invariants — AST-pinned
# ---------------------------------------------------------------------------


def _module_source() -> str:
    path = (
        Path(__file__).resolve().parents[2]
        / "backend" / "core" / "ouroboros" / "governance"
        / "verification" / "postmortem_recall_consumer.py"
    )
    return path.read_text(encoding="utf-8")


class TestAuthorityInvariants:
    @pytest.fixture
    def source(self):
        return _module_source()

    def test_no_orchestrator_imports(self, source):
        forbidden = [
            "orchestrator", "iron_gate", "policy",
            "change_engine", "candidate_generator", "providers",
            "doubleword_provider", "urgency_router",
            "auto_action_router", "subagent_scheduler",
            "tool_executor", "phase_runners",
            "semantic_guardian", "semantic_firewall",
            "risk_engine", "episodic_memory",
            "ast_canonical", "semantic_index",
        ]
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                m = (
                    node.module if isinstance(node, ast.ImportFrom)
                    else (
                        node.names[0].name if node.names else ""
                    )
                )
                m = m or ""
                for f in forbidden:
                    assert f not in m, f"forbidden import: {m}"

    def test_governance_imports_in_allowlist(self, source):
        """Slice 4 hot path may import:
          * Slice 1 (postmortem_recall)
          * Priority #1 Slice 1 (coherence_auditor)
          * Priority #1 Slice 4 (coherence_action_bridge)
          * adaptation.ledger (MonotonicTighteningVerdict)
        Module-owned registration functions (``register_flags`` /
        ``register_shipped_invariants``) are STRUCTURALLY exempt —
        their imports only fire from the boot-time discovery loops,
        never on the hot path."""
        tree = ast.parse(source)
        allowed = {
            "backend.core.ouroboros.governance.adaptation.ledger",
            "backend.core.ouroboros.governance.verification.coherence_action_bridge",
            "backend.core.ouroboros.governance.verification.coherence_auditor",
            "backend.core.ouroboros.governance.verification.postmortem_recall",
        }
        registration_funcs = {"register_flags", "register_shipped_invariants"}
        exempt_ranges = []
        for fnode in ast.walk(tree):
            if isinstance(fnode, ast.FunctionDef):
                if fnode.name in registration_funcs:
                    start = getattr(fnode, "lineno", 0)
                    end = getattr(fnode, "end_lineno", start) or start
                    exempt_ranges.append((start, end))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module and "governance" in node.module:
                    lineno = getattr(node, "lineno", 0)
                    if any(s <= lineno <= e for s, e in exempt_ranges):
                        continue
                    assert node.module in allowed, (
                        f"governance import outside allowlist: "
                        f"{node.module}"
                    )

    def test_must_reference_monotonic_tightening_verdict(
        self, source,
    ):
        """STRUCTURAL Phase C universal-cage-rule integration
        pin."""
        assert "MonotonicTighteningVerdict" in source

    def test_must_reference_read_coherence_advisories(self, source):
        """Canonical reader reuse from Priority #1 Slice 4."""
        assert "read_coherence_advisories" in source

    def test_must_reference_inject_postmortem_recall_hint(
        self, source,
    ):
        """Filter target — catches refactor that drops the
        action filter."""
        assert "INJECT_POSTMORTEM_RECALL_HINT" in source

    def test_must_import_monotonic_via_importfrom(self, source):
        tree = ast.parse(source)
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if (
                    node.module
                    == "backend.core.ouroboros.governance"
                    ".adaptation.ledger"
                ):
                    for alias in node.names:
                        if alias.name == "MonotonicTighteningVerdict":
                            found = True
        assert found, (
            "must import MonotonicTighteningVerdict via "
            "importfrom from adaptation.ledger"
        )

    def test_no_mutation_tools(self, source):
        forbidden = [
            "edit_file", "write_file", "delete_file",
            "subprocess." + "run", "subprocess." + "Popen",
            "os." + "system", "shutil.rmtree",
        ]
        for f in forbidden:
            assert f not in source

    def test_no_eval_family_calls(self, source):
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    assert node.func.id not in (
                        "exec", "eval", "compile",
                    )
        for token in _FORBIDDEN_CALL_TOKENS:
            assert token not in source

    def test_no_async_functions(self, source):
        tree = ast.parse(source)
        for node in ast.walk(tree):
            assert not isinstance(node, ast.AsyncFunctionDef)

    def test_public_api_exported(self, source):
        for name in (
            "RecurrenceBoost", "RecurrenceBoostStatus",
            "compute_recurrence_boosts",
            "compute_effective_top_k",
            "get_active_recurrence_boosts",
            "postmortem_recurrence_boost_enabled",
            "boost_ttl_hours", "boost_max_count",
            "POSTMORTEM_RECALL_CONSUMER_SCHEMA_VERSION",
        ):
            assert f'"{name}"' in source
