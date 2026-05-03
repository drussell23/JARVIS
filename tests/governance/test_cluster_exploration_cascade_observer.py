"""ClusterExplorationCascadeObserver Slice 4 -- regression spine.

Pins:
  * Master flag + sub-flag asymmetric env semantics
  * Env knob clamping
  * _parse_cluster_coverage_tag defensive parse (5+ corruption modes)
  * observe_cluster_coverage_completion: every short-circuit path +
    happy path persistence + filtering of project-root sentinel +
    role inference stub gating
  * render_prior_context_block: empty / partial / full
  * Backward-compat: OperationContext.intake_evidence_json default
    empty + threading via OperationContext.create
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple

import pytest

from backend.core.ouroboros.governance.cluster_exploration_cascade_observer import (  # noqa: E501
    CLUSTER_CASCADE_SCHEMA_VERSION,
    _parse_cluster_coverage_tag,
    auto_role_enabled,
    cascade_observer_enabled,
    observe_cluster_coverage_completion,
    render_prior_context_block,
)
from backend.core.ouroboros.governance.domain_map_memory import (
    DomainMapStore,
    reset_default_store,
)
from backend.core.ouroboros.governance.op_context import (
    OperationContext,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    for var in (
        "JARVIS_CLUSTER_CASCADE_OBSERVER_ENABLED",
        "JARVIS_DOMAIN_MAP_AUTO_ROLE_ENABLED",
        "JARVIS_CLUSTER_CASCADE_PRIOR_CONTEXT_MAX_FILES",
        "JARVIS_DOMAIN_MAP_ENABLED",
    ):
        monkeypatch.delenv(var, raising=False)
    yield
    reset_default_store()


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DOMAIN_MAP_ENABLED", "true")
    return DomainMapStore(project_root=tmp_path)


def _enable_cascade(monkeypatch):
    monkeypatch.setenv("JARVIS_CLUSTER_CASCADE_OBSERVER_ENABLED", "true")
    monkeypatch.setenv("JARVIS_DOMAIN_MAP_ENABLED", "true")


def _cluster_coverage_evidence_json(
    *,
    centroid_hash8: str = "abc12345",
    cluster_id: int = 3,
    theme_label: str = "voice biometric",
    cluster_size: int = 5,
) -> str:
    return json.dumps({
        "category": "cluster_coverage",
        "cluster_id": cluster_id,
        "centroid_hash8": centroid_hash8,
        "kind": "goal",
        "theme_label": theme_label,
        "cluster_size": cluster_size,
        "sensor": "ProactiveExplorationSensor",
        "target_files_source": "representative_paths",
        "representative_paths_count": 2,
    }, sort_keys=True)


# ---------------------------------------------------------------------------
# Constants + flags
# ---------------------------------------------------------------------------


class TestConstants:
    def test_schema_version(self):
        assert CLUSTER_CASCADE_SCHEMA_VERSION == "cluster_cascade.v1"


class TestCascadeFlag:
    def test_default_true_post_graduation(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_CLUSTER_CASCADE_OBSERVER_ENABLED", raising=False,
        )
        assert cascade_observer_enabled() is True

    @pytest.mark.parametrize("raw", ["1", "true", "On", "YES"])
    def test_truthy(self, monkeypatch, raw):
        monkeypatch.setenv(
            "JARVIS_CLUSTER_CASCADE_OBSERVER_ENABLED", raw,
        )
        assert cascade_observer_enabled() is True

    @pytest.mark.parametrize("raw", ["0", "false", "garbage"])
    def test_falsy(self, monkeypatch, raw):
        monkeypatch.setenv(
            "JARVIS_CLUSTER_CASCADE_OBSERVER_ENABLED", raw,
        )
        assert cascade_observer_enabled() is False


class TestAutoRoleFlag:
    def test_default_false(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_DOMAIN_MAP_AUTO_ROLE_ENABLED", raising=False,
        )
        assert auto_role_enabled() is False

    def test_truthy(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_DOMAIN_MAP_AUTO_ROLE_ENABLED", "true",
        )
        assert auto_role_enabled() is True


# ---------------------------------------------------------------------------
# OperationContext additive field
# ---------------------------------------------------------------------------


class TestOperationContextAdditive:
    def test_intake_evidence_json_defaults_empty(self):
        ctx = OperationContext.create(
            target_files=("a.py",), description="x",
        )
        assert ctx.intake_evidence_json == ""

    def test_intake_evidence_json_threaded(self):
        evidence = {"category": "cluster_coverage", "x": 1}
        evidence_json = json.dumps(evidence, sort_keys=True)
        ctx = OperationContext.create(
            target_files=("a.py",), description="x",
            intake_evidence_json=evidence_json,
        )
        assert ctx.intake_evidence_json == evidence_json
        # Round-trip parse works
        parsed = json.loads(ctx.intake_evidence_json)
        assert parsed["category"] == "cluster_coverage"


# ---------------------------------------------------------------------------
# _parse_cluster_coverage_tag
# ---------------------------------------------------------------------------


class TestParseTag:
    def test_happy_path(self):
        evidence = _cluster_coverage_evidence_json()
        tag = _parse_cluster_coverage_tag(evidence)
        assert tag is not None
        assert tag["centroid_hash8"] == "abc12345"
        assert tag["cluster_id"] == 3

    def test_empty_string_returns_none(self):
        assert _parse_cluster_coverage_tag("") is None

    def test_none_returns_none(self):
        assert _parse_cluster_coverage_tag(None) is None  # type: ignore[arg-type]

    def test_non_string_returns_none(self):
        assert _parse_cluster_coverage_tag(42) is None  # type: ignore[arg-type]

    def test_invalid_json_returns_none(self):
        assert _parse_cluster_coverage_tag("{not json") is None

    def test_non_dict_top_level_returns_none(self):
        assert _parse_cluster_coverage_tag("[1,2,3]") is None
        assert _parse_cluster_coverage_tag('"just a string"') is None

    def test_wrong_category_returns_none(self):
        evidence = json.dumps({
            "category": "test_failure",
            "centroid_hash8": "abc12345",
        })
        assert _parse_cluster_coverage_tag(evidence) is None

    def test_missing_category_returns_none(self):
        evidence = json.dumps({"centroid_hash8": "abc12345"})
        assert _parse_cluster_coverage_tag(evidence) is None

    def test_missing_centroid_hash8_returns_none(self):
        evidence = json.dumps({"category": "cluster_coverage"})
        assert _parse_cluster_coverage_tag(evidence) is None

    def test_empty_centroid_hash8_returns_none(self):
        evidence = json.dumps({
            "category": "cluster_coverage",
            "centroid_hash8": "",
        })
        assert _parse_cluster_coverage_tag(evidence) is None

    def test_non_string_centroid_hash8_returns_none(self):
        evidence = json.dumps({
            "category": "cluster_coverage",
            "centroid_hash8": 42,
        })
        assert _parse_cluster_coverage_tag(evidence) is None


# ---------------------------------------------------------------------------
# observe_cluster_coverage_completion
# ---------------------------------------------------------------------------


class TestObserveCompletion:
    @pytest.mark.asyncio
    async def test_master_off_returns_none(self, store, monkeypatch):
        # Post-graduation default is true; explicit "false" is the
        # operator escape hatch for the cascade observer.
        monkeypatch.setenv(
            "JARVIS_CLUSTER_CASCADE_OBSERVER_ENABLED", "false",
        )
        monkeypatch.setenv("JARVIS_DOMAIN_MAP_ENABLED", "true")
        out = await observe_cluster_coverage_completion(
            op_id="op-1",
            intake_evidence_json=_cluster_coverage_evidence_json(),
            touched_files=("a.py",),
            verify_passed=True,
            store=store,
        )
        assert out is None
        # No persistence happened.
        assert store.lookup_by_centroid_hash8("abc12345") is None

    @pytest.mark.asyncio
    async def test_domain_map_off_returns_none(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_CLUSTER_CASCADE_OBSERVER_ENABLED", "true",
        )
        # Post-graduation default is true; explicit "false" is the
        # operator escape hatch.
        monkeypatch.setenv("JARVIS_DOMAIN_MAP_ENABLED", "false")
        store = DomainMapStore(project_root=tmp_path)
        out = await observe_cluster_coverage_completion(
            op_id="op-1",
            intake_evidence_json=_cluster_coverage_evidence_json(),
            touched_files=("a.py",),
            verify_passed=True,
            store=store,
        )
        assert out is None

    @pytest.mark.asyncio
    async def test_verify_failed_returns_none(self, store, monkeypatch):
        _enable_cascade(monkeypatch)
        out = await observe_cluster_coverage_completion(
            op_id="op-1",
            intake_evidence_json=_cluster_coverage_evidence_json(),
            touched_files=("a.py",),
            verify_passed=False,  # failed
            store=store,
        )
        assert out is None
        assert store.lookup_by_centroid_hash8("abc12345") is None

    @pytest.mark.asyncio
    async def test_non_cluster_coverage_evidence_returns_none(
        self, store, monkeypatch,
    ):
        _enable_cascade(monkeypatch)
        evidence = json.dumps({"category": "test_failure"})
        out = await observe_cluster_coverage_completion(
            op_id="op-1",
            intake_evidence_json=evidence,
            touched_files=("a.py",),
            verify_passed=True,
            store=store,
        )
        assert out is None

    @pytest.mark.asyncio
    async def test_empty_evidence_returns_none(self, store, monkeypatch):
        _enable_cascade(monkeypatch)
        out = await observe_cluster_coverage_completion(
            op_id="op-1",
            intake_evidence_json="",
            touched_files=("a.py",),
            verify_passed=True,
            store=store,
        )
        assert out is None

    @pytest.mark.asyncio
    async def test_happy_path_persists(self, store, monkeypatch):
        _enable_cascade(monkeypatch)
        out = await observe_cluster_coverage_completion(
            op_id="op-42",
            intake_evidence_json=_cluster_coverage_evidence_json(),
            touched_files=("voice/auth.py", "voice/util.py"),
            verify_passed=True,
            store=store,
        )
        assert out is not None
        assert out.centroid_hash8 == "abc12345"
        assert out.cluster_id == 3
        assert out.theme_label == "voice biometric"
        assert "voice/auth.py" in out.discovered_files
        assert "voice/util.py" in out.discovered_files
        assert out.populated_by_op_id == "op-42"
        assert out.confidence == 1.0  # verify passed
        assert out.exploration_count == 1

        # And the store actually has it
        recovered = store.lookup_by_centroid_hash8("abc12345")
        assert recovered is not None
        assert recovered.centroid_hash8 == "abc12345"

    @pytest.mark.asyncio
    async def test_project_root_sentinel_filtered_out(
        self, store, monkeypatch,
    ):
        _enable_cascade(monkeypatch)
        out = await observe_cluster_coverage_completion(
            op_id="op-1",
            intake_evidence_json=_cluster_coverage_evidence_json(),
            # Includes the "." sentinel and an empty string
            touched_files=(".", "", "real.py"),
            verify_passed=True,
            store=store,
        )
        assert out is not None
        assert out.discovered_files == ("real.py",)

    @pytest.mark.asyncio
    async def test_empty_touched_files_still_records(
        self, store, monkeypatch,
    ):
        """Even with no files touched, a successful cluster_coverage
        completion is meaningful telemetry -- exploration_count + theme
        get recorded."""
        _enable_cascade(monkeypatch)
        out = await observe_cluster_coverage_completion(
            op_id="op-1",
            intake_evidence_json=_cluster_coverage_evidence_json(),
            touched_files=(),
            verify_passed=True,
            store=store,
        )
        assert out is not None
        assert out.discovered_files == ()
        assert out.exploration_count == 1

    @pytest.mark.asyncio
    async def test_role_stub_when_auto_role_off(
        self, store, monkeypatch,
    ):
        _enable_cascade(monkeypatch)
        # auto_role_enabled is OFF
        out = await observe_cluster_coverage_completion(
            op_id="op-1",
            intake_evidence_json=_cluster_coverage_evidence_json(),
            touched_files=("a.py",),
            verify_passed=True,
            store=store,
        )
        assert out is not None
        # No role recorded (caller didn't pass one; auto-role off)
        assert out.architectural_role == ""

    @pytest.mark.asyncio
    async def test_role_stub_when_auto_role_on(
        self, store, monkeypatch,
    ):
        _enable_cascade(monkeypatch)
        monkeypatch.setenv(
            "JARVIS_DOMAIN_MAP_AUTO_ROLE_ENABLED", "true",
        )
        out = await observe_cluster_coverage_completion(
            op_id="op-1",
            intake_evidence_json=_cluster_coverage_evidence_json(),
            touched_files=("a.py",),
            verify_passed=True,
            store=store,
        )
        assert out is not None
        # auto-role is stubbed -- placeholder marker recorded
        assert out.architectural_role == "role_inference_pending"

    @pytest.mark.asyncio
    async def test_no_store_no_root_returns_none(self, monkeypatch):
        _enable_cascade(monkeypatch)
        # Reset default singleton to ensure no fallback wiring
        reset_default_store()
        out = await observe_cluster_coverage_completion(
            op_id="op-1",
            intake_evidence_json=_cluster_coverage_evidence_json(),
            touched_files=("a.py",),
            verify_passed=True,
            store=None,
            project_root=None,
        )
        assert out is None

    @pytest.mark.asyncio
    async def test_idempotent_merge_increments_count(
        self, store, monkeypatch,
    ):
        _enable_cascade(monkeypatch)
        for _ in range(3):
            await observe_cluster_coverage_completion(
                op_id="op-x",
                intake_evidence_json=_cluster_coverage_evidence_json(),
                touched_files=("a.py",),
                verify_passed=True,
                store=store,
            )
        final = store.lookup_by_centroid_hash8("abc12345")
        assert final.exploration_count == 3


# ---------------------------------------------------------------------------
# render_prior_context_block
# ---------------------------------------------------------------------------


class TestRenderPriorContext:
    def test_master_off_returns_empty(self, store, monkeypatch):
        # Post-graduation default is true; explicit "false" is the
        # operator escape hatch.
        monkeypatch.setenv(
            "JARVIS_CLUSTER_CASCADE_OBSERVER_ENABLED", "false",
        )
        # Even with an entry on disk, master-off returns empty.
        store.record_exploration("abc12345", theme_label="x")
        out = render_prior_context_block(
            "abc12345", store=store,
        )
        assert out == ""

    def test_domain_map_off_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CLUSTER_CASCADE_OBSERVER_ENABLED", "true",
        )
        # Post-graduation default is true; explicit "false" is the
        # operator escape hatch.
        monkeypatch.setenv("JARVIS_DOMAIN_MAP_ENABLED", "false")
        store = DomainMapStore(project_root=tmp_path)
        out = render_prior_context_block(
            "abc12345", store=store,
        )
        assert out == ""

    def test_no_entry_returns_empty(self, store, monkeypatch):
        _enable_cascade(monkeypatch)
        out = render_prior_context_block(
            "nonexistent12345", store=store,
        )
        assert out == ""

    def test_invalid_hash_returns_empty(self, store, monkeypatch):
        _enable_cascade(monkeypatch)
        assert render_prior_context_block("", store=store) == ""
        assert render_prior_context_block("   ", store=store) == ""

    def test_entry_with_files_only(self, store, monkeypatch):
        _enable_cascade(monkeypatch)
        store.record_exploration(
            "abc12345",
            discovered_files=("voice/auth.py", "voice/util.py"),
        )
        out = render_prior_context_block("abc12345", store=store)
        assert "Previously explored" in out
        assert "count=1" in out
        assert "voice/auth.py" in out
        assert "voice/util.py" in out
        # No role line since none recorded
        assert "Architectural role" not in out

    def test_entry_with_role(self, store, monkeypatch):
        _enable_cascade(monkeypatch)
        store.record_exploration(
            "abc12345",
            discovered_files=("voice/auth.py",),
            architectural_role="voice biometric primitive",
        )
        out = render_prior_context_block("abc12345", store=store)
        assert "voice/auth.py" in out
        assert "Architectural role: voice biometric primitive" in out

    def test_role_inference_pending_marker_hidden(
        self, store, monkeypatch,
    ):
        """The Slice 4 stub marker should NOT appear in operator-
        facing output -- it's an internal flag for future arc
        wiring, not for the model to read."""
        _enable_cascade(monkeypatch)
        store.record_exploration(
            "abc12345",
            discovered_files=("a.py",),
            architectural_role="role_inference_pending",
        )
        out = render_prior_context_block("abc12345", store=store)
        assert "role_inference_pending" not in out
        assert "Architectural role" not in out

    def test_files_capped_by_env(self, store, monkeypatch):
        _enable_cascade(monkeypatch)
        monkeypatch.setenv(
            "JARVIS_CLUSTER_CASCADE_PRIOR_CONTEXT_MAX_FILES", "2",
        )
        store.record_exploration(
            "abc12345",
            discovered_files=tuple(f"f{i}.py" for i in range(20)),
        )
        out = render_prior_context_block("abc12345", store=store)
        # Only first 2 files surfaced
        assert "f0.py" in out and "f1.py" in out
        assert "f2.py" not in out

    def test_count_reflects_repeated_explorations(
        self, store, monkeypatch,
    ):
        _enable_cascade(monkeypatch)
        for _ in range(5):
            store.record_exploration(
                "abc12345",
                discovered_files=("a.py",),
            )
        out = render_prior_context_block("abc12345", store=store)
        assert "count=5" in out

    def test_empty_files_renders_placeholder(
        self, store, monkeypatch,
    ):
        _enable_cascade(monkeypatch)
        # Entry exists but with no discovered_files.
        store.record_exploration("abc12345", theme_label="x")
        out = render_prior_context_block("abc12345", store=store)
        assert "no files recorded" in out
