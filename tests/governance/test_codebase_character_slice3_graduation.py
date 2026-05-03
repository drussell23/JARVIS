"""CodebaseCharacterDigest Slice 3 — graduation regression suite.

Pins the four graduation deliverables:

  * Master flag flip: default-True post-graduation
  * 4 ``shipped_code_invariants`` AST pins (vocabulary + bug-fix
    regression pin on ProactiveExploration._emit_cluster_coverage_signals
    invocation + total-function pin + no-caller-imports pin)
  * 5 FlagRegistry seeds (master + 4 env-knob accessors)
  * ``EVENT_TYPE_CODEBASE_CHARACTER_INJECTED`` SSE event registered
  * 1 GET route ``/observability/codebase-character``
  * ProactiveExplorationSensor cluster-coverage emit path wires the
    bias and dedupes on centroid_hash8
"""
from __future__ import annotations

import ast
import enum
import inspect
import json
import time
from dataclasses import dataclass
from typing import Any, List, Tuple
from unittest import mock

import pytest

from backend.core.ouroboros.governance.codebase_character import (
    CODEBASE_CHARACTER_SCHEMA_VERSION,
    DigestOutcome,
    codebase_character_enabled,
    register_flags,
    register_shipped_invariants,
)
from backend.core.ouroboros.governance.flag_registry import (
    Category, FlagSpec, FlagType,
)
from backend.core.ouroboros.governance.ide_observability_stream import (
    EVENT_TYPE_CODEBASE_CHARACTER_INJECTED,
    _VALID_EVENT_TYPES,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _FakeStats:
    built_at: float
    corpus_n: int
    cluster_mode: str = "kmeans"


@dataclass
class _FakeCluster:
    cluster_id: int
    kind: str
    size: int
    nearest_item_text: str
    nearest_item_source: str
    source_composition: Tuple[Tuple[str, int], ...]
    centroid_hash8: str


def _mk_cluster(
    cid: int = 1, kind: str = "goal", size: int = 5,
    text: str = "Voice biometric authentication primitive",
    hash8: str = "deadbeef",
) -> _FakeCluster:
    return _FakeCluster(
        cluster_id=cid,
        kind=kind,
        size=size,
        nearest_item_text=text,
        nearest_item_source="git_commit",
        source_composition=(("git_commit", size),),
        centroid_hash8=hash8,
    )


class _FakeIndex:
    def __init__(self, clusters=(), built_at=None, corpus_n=10):
        self.clusters = clusters
        self._stats = _FakeStats(
            built_at=built_at if built_at is not None else time.time(),
            corpus_n=corpus_n,
        )

    def stats(self):
        return self._stats


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    for var in (
        "JARVIS_CODEBASE_CHARACTER_DIGEST_ENABLED",
        "JARVIS_CODEBASE_CHARACTER_MIN_CLUSTERS",
        "JARVIS_CODEBASE_CHARACTER_STALE_AFTER_S",
        "JARVIS_CODEBASE_CHARACTER_MAX_CLUSTERS_IN_DIGEST",
        "JARVIS_CODEBASE_CHARACTER_EXCERPT_MAX_CHARS",
        "JARVIS_EXPLORATION_CLUSTER_EMIT_PER_SCAN",
        "JARVIS_IDE_OBSERVABILITY_ENABLED",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


# ---------------------------------------------------------------------------
# §A — Master flag graduation
# ---------------------------------------------------------------------------


class TestMasterFlagGraduation:
    def test_default_true_post_graduation(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_CODEBASE_CHARACTER_DIGEST_ENABLED", raising=False,
        )
        assert codebase_character_enabled() is True

    @pytest.mark.parametrize("v", ["1", "true", "yes", "on", "TRUE"])
    def test_explicit_truthy(self, monkeypatch, v):
        monkeypatch.setenv(
            "JARVIS_CODEBASE_CHARACTER_DIGEST_ENABLED", v,
        )
        assert codebase_character_enabled() is True

    @pytest.mark.parametrize(
        "v", ["0", "false", "no", "off", "garbage"],
    )
    def test_explicit_falsy_rolls_back(self, monkeypatch, v):
        monkeypatch.setenv(
            "JARVIS_CODEBASE_CHARACTER_DIGEST_ENABLED", v,
        )
        assert codebase_character_enabled() is False

    @pytest.mark.parametrize("v", ["", "   ", "\t"])
    def test_empty_treats_as_unset(self, monkeypatch, v):
        monkeypatch.setenv(
            "JARVIS_CODEBASE_CHARACTER_DIGEST_ENABLED", v,
        )
        assert codebase_character_enabled() is True


# ---------------------------------------------------------------------------
# §B — register_shipped_invariants
# ---------------------------------------------------------------------------


class TestShippedInvariants:
    def test_returns_four(self):
        invs = register_shipped_invariants()
        assert len(invs) == 4

    def test_invariant_names(self):
        invs = register_shipped_invariants()
        names = {i.invariant_name for i in invs}
        expected = {
            "digest_outcome_vocabulary",
            "proactive_exploration_cluster_bias_present",
            "compute_codebase_character_total",
            "codebase_character_no_caller_imports",
        }
        assert names == expected

    def test_each_invariant_has_validator_and_target(self):
        invs = register_shipped_invariants()
        for inv in invs:
            assert callable(inv.validate)
            assert inv.target_file.startswith("backend/")
            assert inv.description.strip() != ""

    def test_outcome_vocabulary_pin_passes_clean_source(self):
        from backend.core.ouroboros.governance import (
            codebase_character,
        )
        src = inspect.getsource(codebase_character)
        tree = ast.parse(src)
        invs = register_shipped_invariants()
        vocab_inv = next(
            i for i in invs
            if i.invariant_name == "digest_outcome_vocabulary"
        )
        assert vocab_inv.validate(tree, src) == ()

    def test_outcome_vocabulary_pin_fires_on_added_value(self):
        bad_src = (
            "import enum\n"
            "class DigestOutcome(str, enum.Enum):\n"
            "    READY = 'r'\n"
            "    INSUFFICIENT_CLUSTERS = 'i'\n"
            "    STALE_INDEX = 's'\n"
            "    DISABLED = 'd'\n"
            "    FAILED = 'f'\n"
            "    NEW_ROGUE = 'x'\n"
        )
        tree = ast.parse(bad_src)
        invs = register_shipped_invariants()
        vocab_inv = next(
            i for i in invs
            if i.invariant_name == "digest_outcome_vocabulary"
        )
        violations = vocab_inv.validate(tree, bad_src)
        assert any("NEW_ROGUE" in v for v in violations)

    def test_outcome_vocabulary_pin_fires_on_missing_value(self):
        bad_src = (
            "import enum\n"
            "class DigestOutcome(str, enum.Enum):\n"
            "    READY = 'r'\n"
            "    DISABLED = 'd'\n"
            "    FAILED = 'f'\n"
        )
        tree = ast.parse(bad_src)
        invs = register_shipped_invariants()
        vocab_inv = next(
            i for i in invs
            if i.invariant_name == "digest_outcome_vocabulary"
        )
        violations = vocab_inv.validate(tree, bad_src)
        assert any(
            "INSUFFICIENT_CLUSTERS" in v or "STALE_INDEX" in v
            for v in violations
        )

    def test_proactive_exploration_bias_pin_passes_clean(self):
        # THE BUG-FIX REGRESSION PIN. Validates that
        # ProactiveExplorationSensor.scan_once contains the cluster-
        # coverage emit invocation wired in Slice 3.
        from backend.core.ouroboros.governance.intake.sensors import (
            proactive_exploration_sensor,
        )
        src = inspect.getsource(proactive_exploration_sensor)
        tree = ast.parse(src)
        invs = register_shipped_invariants()
        bias_inv = next(
            i for i in invs
            if i.invariant_name
            == "proactive_exploration_cluster_bias_present"
        )
        violations = bias_inv.validate(tree, src)
        assert violations == (), (
            "BUG-FIX regression pin violated: "
            f"{violations} — Slice 3's cluster-coverage emit was "
            "removed; the doc_staleness:exploration 10:1 ratio "
            "from soak v3 baseline has regressed"
        )

    def test_proactive_exploration_bias_pin_fires_on_removal(self):
        bad_src = (
            "class FakeSensor:\n"
            "    async def scan_once(self):\n"
            "        # Slice 3 wire-up was deleted\n"
            "        return []\n"
        )
        tree = ast.parse(bad_src)
        invs = register_shipped_invariants()
        bias_inv = next(
            i for i in invs
            if i.invariant_name
            == "proactive_exploration_cluster_bias_present"
        )
        violations = bias_inv.validate(tree, bad_src)
        assert any(
            "_emit_cluster_coverage_signals" in v
            for v in violations
        )

    def test_total_function_pin_passes_clean(self):
        from backend.core.ouroboros.governance import (
            codebase_character,
        )
        src = inspect.getsource(codebase_character)
        tree = ast.parse(src)
        invs = register_shipped_invariants()
        total_inv = next(
            i for i in invs
            if i.invariant_name
            == "compute_codebase_character_total"
        )
        assert total_inv.validate(tree, src) == ()

    def test_total_function_pin_fires_on_synthetic_raise(self):
        bad_src = (
            "def compute_codebase_character(*, enabled):\n"
            "    if not enabled:\n"
            "        raise RuntimeError('boom')\n"
            "    return None\n"
        )
        tree = ast.parse(bad_src)
        invs = register_shipped_invariants()
        total_inv = next(
            i for i in invs
            if i.invariant_name
            == "compute_codebase_character_total"
        )
        violations = total_inv.validate(tree, bad_src)
        assert len(violations) >= 1
        assert "raise" in violations[0]

    def test_no_caller_imports_pin_passes_clean(self):
        from backend.core.ouroboros.governance import (
            codebase_character,
        )
        src = inspect.getsource(codebase_character)
        tree = ast.parse(src)
        invs = register_shipped_invariants()
        no_caller_inv = next(
            i for i in invs
            if i.invariant_name
            == "codebase_character_no_caller_imports"
        )
        assert no_caller_inv.validate(tree, src) == ()

    def test_no_caller_imports_pin_fires_on_synthetic_import(self):
        bad_src = (
            "from backend.core.ouroboros.governance.strategic_direction import X\n"  # noqa: E501
        )
        tree = ast.parse(bad_src)
        invs = register_shipped_invariants()
        no_caller_inv = next(
            i for i in invs
            if i.invariant_name
            == "codebase_character_no_caller_imports"
        )
        violations = no_caller_inv.validate(tree, bad_src)
        assert any(
            "strategic_direction" in v for v in violations
        )


# ---------------------------------------------------------------------------
# §C — register_flags
# ---------------------------------------------------------------------------


class _StubRegistry:
    def __init__(self) -> None:
        self.specs: List[FlagSpec] = []

    def bulk_register(self, specs, *, override=False) -> int:
        self.specs.extend(specs)
        return len(specs)


class TestFlagRegistry:
    def test_register_returns_five(self):
        reg = _StubRegistry()
        n = register_flags(reg)
        assert n == 5

    def test_master_flag_default_true(self):
        reg = _StubRegistry()
        register_flags(reg)
        master = next(
            s for s in reg.specs
            if s.name == "JARVIS_CODEBASE_CHARACTER_DIGEST_ENABLED"
        )
        assert master.default is True
        assert master.type is FlagType.BOOL
        assert master.category is Category.SAFETY

    def test_all_five_flag_names(self):
        reg = _StubRegistry()
        register_flags(reg)
        names = {s.name for s in reg.specs}
        expected = {
            "JARVIS_CODEBASE_CHARACTER_DIGEST_ENABLED",
            "JARVIS_CODEBASE_CHARACTER_MIN_CLUSTERS",
            "JARVIS_CODEBASE_CHARACTER_STALE_AFTER_S",
            "JARVIS_CODEBASE_CHARACTER_MAX_CLUSTERS_IN_DIGEST",
            "JARVIS_EXPLORATION_CLUSTER_EMIT_PER_SCAN",
        }
        assert names == expected

    def test_all_specs_documented(self):
        reg = _StubRegistry()
        register_flags(reg)
        for spec in reg.specs:
            assert isinstance(spec.category, Category)
            assert spec.description.strip() != ""
            assert spec.source_file.endswith(".py")
            assert spec.since.startswith(
                "CodebaseCharacterDigest Slice 3"
            )

    def test_no_duplicate_names(self):
        reg = _StubRegistry()
        register_flags(reg)
        names = [s.name for s in reg.specs]
        assert len(names) == len(set(names))


# ---------------------------------------------------------------------------
# §D — SSE event registration
# ---------------------------------------------------------------------------


class TestSSEEvent:
    def test_event_type_constant(self):
        assert (
            EVENT_TYPE_CODEBASE_CHARACTER_INJECTED
            == "codebase_character_injected"
        )

    def test_event_type_in_valid_set(self):
        assert (
            EVENT_TYPE_CODEBASE_CHARACTER_INJECTED
            in _VALID_EVENT_TYPES
        )


# ---------------------------------------------------------------------------
# §E — ProactiveExploration cluster-coverage emit path
# ---------------------------------------------------------------------------


class _FakeRouter:
    def __init__(self) -> None:
        self.envelopes: List[Any] = []

    async def ingest(self, envelope) -> None:
        self.envelopes.append(envelope)


class TestProactiveExplorationCoverageEmit:
    @pytest.mark.asyncio
    async def test_emits_one_envelope_per_scan_default(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_CODEBASE_CHARACTER_DIGEST_ENABLED", "true",
        )
        from backend.core.ouroboros.governance.intake.sensors.proactive_exploration_sensor import (  # noqa: E501
            ProactiveExplorationSensor,
        )
        router = _FakeRouter()
        sensor = ProactiveExplorationSensor(
            repo="test-repo", router=router,
        )
        fake_idx = _FakeIndex(
            clusters=(
                _mk_cluster(1, hash8="aaaa1111"),
                _mk_cluster(2, hash8="bbbb2222"),
                _mk_cluster(3, hash8="cccc3333"),
            ),
        )
        with mock.patch(
            "backend.core.ouroboros.governance.semantic_index.get_default_index",  # noqa: E501
            return_value=fake_idx,
        ):
            emitted = await sensor._emit_cluster_coverage_signals()
        # Default cap = 1 emission per scan.
        assert len(emitted) == 1
        assert len(router.envelopes) == 1
        # Dedup recorded.
        assert "aaaa1111" in sensor._explored_clusters

    @pytest.mark.asyncio
    async def test_dedup_across_scans(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CODEBASE_CHARACTER_DIGEST_ENABLED", "true",
        )
        monkeypatch.setenv(
            "JARVIS_EXPLORATION_CLUSTER_EMIT_PER_SCAN", "8",
        )
        from backend.core.ouroboros.governance.intake.sensors.proactive_exploration_sensor import (  # noqa: E501
            ProactiveExplorationSensor,
        )
        router = _FakeRouter()
        sensor = ProactiveExplorationSensor(
            repo="r", router=router,
        )
        fake_idx = _FakeIndex(
            clusters=(
                _mk_cluster(1, hash8="aaaa1111"),
                _mk_cluster(2, hash8="bbbb2222"),
            ),
        )
        with mock.patch(
            "backend.core.ouroboros.governance.semantic_index.get_default_index",  # noqa: E501
            return_value=fake_idx,
        ):
            await sensor._emit_cluster_coverage_signals()
            # Second scan with same clusters — no re-emit.
            second = await sensor._emit_cluster_coverage_signals()
        assert second == []
        assert len(router.envelopes) == 2

    @pytest.mark.asyncio
    async def test_master_off_returns_empty(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CODEBASE_CHARACTER_DIGEST_ENABLED", "false",
        )
        from backend.core.ouroboros.governance.intake.sensors.proactive_exploration_sensor import (  # noqa: E501
            ProactiveExplorationSensor,
        )
        router = _FakeRouter()
        sensor = ProactiveExplorationSensor(
            repo="r", router=router,
        )
        fake_idx = _FakeIndex(
            clusters=(_mk_cluster(1), _mk_cluster(2)),
        )
        with mock.patch(
            "backend.core.ouroboros.governance.semantic_index.get_default_index",  # noqa: E501
            return_value=fake_idx,
        ):
            emitted = await sensor._emit_cluster_coverage_signals()
        assert emitted == []
        assert router.envelopes == []

    @pytest.mark.asyncio
    async def test_router_failure_swallowed(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CODEBASE_CHARACTER_DIGEST_ENABLED", "true",
        )
        from backend.core.ouroboros.governance.intake.sensors.proactive_exploration_sensor import (  # noqa: E501
            ProactiveExplorationSensor,
        )

        class _BombRouter:
            async def ingest(self, envelope):
                raise RuntimeError("router boom")

        sensor = ProactiveExplorationSensor(
            repo="r", router=_BombRouter(),
        )
        fake_idx = _FakeIndex(
            clusters=(_mk_cluster(1), _mk_cluster(2)),
        )
        with mock.patch(
            "backend.core.ouroboros.governance.semantic_index.get_default_index",  # noqa: E501
            return_value=fake_idx,
        ):
            # Must not propagate. Exception is logged at debug.
            emitted = await sensor._emit_cluster_coverage_signals()
        assert emitted == []

    @pytest.mark.asyncio
    async def test_envelope_has_cluster_coverage_evidence(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_CODEBASE_CHARACTER_DIGEST_ENABLED", "true",
        )
        from backend.core.ouroboros.governance.intake.sensors.proactive_exploration_sensor import (  # noqa: E501
            ProactiveExplorationSensor,
        )
        router = _FakeRouter()
        sensor = ProactiveExplorationSensor(
            repo="r", router=router,
        )
        fake_idx = _FakeIndex(
            clusters=(
                _mk_cluster(
                    7, "goal", 12,
                    "Voice biometric authentication WebAuthn",
                    hash8="vbabba00",
                ),
                _mk_cluster(8, hash8="bbbb2222"),
            ),
        )
        with mock.patch(
            "backend.core.ouroboros.governance.semantic_index.get_default_index",  # noqa: E501
            return_value=fake_idx,
        ):
            await sensor._emit_cluster_coverage_signals()
        assert len(router.envelopes) == 1
        env = router.envelopes[0]
        # Evidence dict carries the signature payload.
        evidence = (
            env.evidence
            if hasattr(env, "evidence")
            else env.get("evidence", {})
        )
        assert evidence.get("category") == "cluster_coverage"
        assert evidence.get("centroid_hash8") == "vbabba00"
        assert evidence.get("kind") == "goal"
        assert "voice biometric" in (
            evidence.get("theme_label") or ""
        ).lower()


# ---------------------------------------------------------------------------
# §F — GET route
# ---------------------------------------------------------------------------


def _aiohttp_available() -> bool:
    try:
        from aiohttp.test_utils import make_mocked_request  # noqa
        return True
    except ImportError:
        return False


def _make_request(path: str):
    from aiohttp.test_utils import make_mocked_request
    req = make_mocked_request("GET", path)
    req._transport_peername = ("127.0.0.1", 0)  # type: ignore[attr-defined]
    return req


@pytest.mark.skipif(
    not _aiohttp_available(),
    reason="aiohttp not available",
)
class TestGETRoute:
    @pytest.fixture(autouse=True)
    def _enable(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_IDE_OBSERVABILITY_ENABLED", "true",
        )
        monkeypatch.setenv(
            "JARVIS_CODEBASE_CHARACTER_DIGEST_ENABLED", "true",
        )

    @pytest.fixture
    def router(self):
        from backend.core.ouroboros.governance.ide_observability import (  # noqa: E501
            IDEObservabilityRouter,
        )
        return IDEObservabilityRouter()

    def test_route_registers(self, router):
        from aiohttp import web
        app = web.Application()
        router.register_routes(app)
        paths = [
            getattr(r, "resource", None)
            and r.resource.canonical
            for r in app.router.routes()
        ]
        assert "/observability/codebase-character" in paths

    @pytest.mark.asyncio
    async def test_get_200_with_master_on_and_index(self, router):
        fake_idx = _FakeIndex(
            clusters=(_mk_cluster(1), _mk_cluster(2)),
            corpus_n=10,
        )
        with mock.patch(
            "backend.core.ouroboros.governance.semantic_index.get_default_index",  # noqa: E501
            return_value=fake_idx,
        ):
            resp = await router._handle_codebase_character(
                _make_request(
                    "/observability/codebase-character",
                ),
            )
        assert resp.status == 200
        body = json.loads(resp.body.decode("utf-8"))
        assert body["enabled"] is True
        assert "config" in body
        for k in (
            "min_clusters", "stale_after_s",
            "max_clusters_in_digest", "excerpt_max_chars",
        ):
            assert k in body["config"]
        assert "snapshot" in body
        assert (
            body["snapshot"]["schema_version"]
            == CODEBASE_CHARACTER_SCHEMA_VERSION
        )
        assert body["snapshot"]["outcome"] in (
            "ready", "stale_index", "insufficient_clusters",
            "disabled", "failed",
        )

    @pytest.mark.asyncio
    async def test_get_403_when_master_off(
        self, router, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_CODEBASE_CHARACTER_DIGEST_ENABLED", "false",
        )
        resp = await router._handle_codebase_character(
            _make_request("/observability/codebase-character"),
        )
        assert resp.status == 403
        body = json.loads(resp.body.decode("utf-8"))
        assert (
            body["reason_code"]
            == "ide_observability.codebase_character_disabled"
        )

    @pytest.mark.asyncio
    async def test_get_403_when_umbrella_off(
        self, router, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_IDE_OBSERVABILITY_ENABLED", "false",
        )
        resp = await router._handle_codebase_character(
            _make_request("/observability/codebase-character"),
        )
        assert resp.status == 403
        body = json.loads(resp.body.decode("utf-8"))
        assert body["reason_code"] == "ide_observability.disabled"


# ---------------------------------------------------------------------------
# §G — _cluster_emit_per_scan env knob
# ---------------------------------------------------------------------------


class TestEmitPerScanKnob:
    def test_default_one(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_EXPLORATION_CLUSTER_EMIT_PER_SCAN",
            raising=False,
        )
        from backend.core.ouroboros.governance.intake.sensors.proactive_exploration_sensor import (  # noqa: E501
            _cluster_emit_per_scan,
        )
        assert _cluster_emit_per_scan() == 1

    def test_floor_clamp(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_EXPLORATION_CLUSTER_EMIT_PER_SCAN", "0",
        )
        from backend.core.ouroboros.governance.intake.sensors.proactive_exploration_sensor import (  # noqa: E501
            _cluster_emit_per_scan,
        )
        assert _cluster_emit_per_scan() == 1

    def test_ceiling_clamp(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_EXPLORATION_CLUSTER_EMIT_PER_SCAN", "999",
        )
        from backend.core.ouroboros.governance.intake.sensors.proactive_exploration_sensor import (  # noqa: E501
            _cluster_emit_per_scan,
        )
        assert _cluster_emit_per_scan() == 8

    def test_garbage_returns_default(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_EXPLORATION_CLUSTER_EMIT_PER_SCAN", "abc",
        )
        from backend.core.ouroboros.governance.intake.sensors.proactive_exploration_sensor import (  # noqa: E501
            _cluster_emit_per_scan,
        )
        assert _cluster_emit_per_scan() == 1
