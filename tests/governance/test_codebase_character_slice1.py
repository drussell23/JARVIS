"""CodebaseCharacterDigest Slice 1 — pure-stdlib substrate.

Pins:
  * Closed 5-value DigestOutcome enum
  * Total ``compute_codebase_character`` (NEVER raises)
  * Frozen output records
  * No caller imports (no orchestrator, no semantic_index, no
    strategic_direction, no proactive_exploration)
  * No exec/eval/compile / no asyncio / no file I/O / no network
  * Deterministic prompt-section rendering with byte-stable theme
    labels, char-budget truncation that never splits a cluster body
"""
from __future__ import annotations

import ast
import enum
import inspect
import time
from dataclasses import FrozenInstanceError, dataclass
from typing import Any, Dict, Sequence, Tuple
from unittest import mock

import pytest

from backend.core.ouroboros.governance import codebase_character as cc_mod
from backend.core.ouroboros.governance.codebase_character import (
    CODEBASE_CHARACTER_SCHEMA_VERSION,
    ClusterCharacter,
    CodebaseCharacterSnapshot,
    DigestOutcome,
    codebase_character_enabled,
    compute_codebase_character,
    excerpt_max_chars,
    max_clusters_in_digest,
    min_clusters,
    stale_after_s,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _FakeCluster:
    """Synthetic cluster matching the ``_ClusterLike`` Protocol shape."""

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
    source: str = "git_commit",
    comp: Tuple[Tuple[str, int], ...] = (("git_commit", 4), ("goal", 1)),
    hash8: str = "deadbeef",
) -> _FakeCluster:
    return _FakeCluster(
        cluster_id=cid,
        kind=kind,
        size=size,
        nearest_item_text=text,
        nearest_item_source=source,
        source_composition=comp,
        centroid_hash8=hash8,
    )


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    for var in (
        "JARVIS_CODEBASE_CHARACTER_DIGEST_ENABLED",
        "JARVIS_CODEBASE_CHARACTER_MIN_CLUSTERS",
        "JARVIS_CODEBASE_CHARACTER_STALE_AFTER_S",
        "JARVIS_CODEBASE_CHARACTER_MAX_CLUSTERS_IN_DIGEST",
        "JARVIS_CODEBASE_CHARACTER_EXCERPT_MAX_CHARS",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


# ---------------------------------------------------------------------------
# §A — Closed vocabulary
# ---------------------------------------------------------------------------


class TestClosedVocabulary:
    def test_digest_outcome_is_str_enum(self):
        assert issubclass(DigestOutcome, enum.Enum)
        assert issubclass(DigestOutcome, str)

    def test_digest_outcome_has_exactly_five_values(self):
        assert len(list(DigestOutcome)) == 5

    def test_digest_outcome_value_names(self):
        names = {m.name for m in DigestOutcome}
        assert names == {
            "READY",
            "INSUFFICIENT_CLUSTERS",
            "STALE_INDEX",
            "DISABLED",
            "FAILED",
        }

    def test_digest_outcome_value_strings(self):
        assert DigestOutcome.READY.value == "ready"
        assert (
            DigestOutcome.INSUFFICIENT_CLUSTERS.value
            == "insufficient_clusters"
        )
        assert DigestOutcome.STALE_INDEX.value == "stale_index"
        assert DigestOutcome.DISABLED.value == "disabled"
        assert DigestOutcome.FAILED.value == "failed"

    def test_only_ready_is_injectable(self):
        # is_ready() must be the SOLE branch criterion downstream.
        snap_ready = CodebaseCharacterSnapshot(
            outcome=DigestOutcome.READY, clusters=(),
            generated_at_ts=1.0, total_corpus_items=0,
            cluster_mode="kmeans", built_at_ts=1.0,
        )
        assert snap_ready.is_ready() is True
        for outcome in (
            DigestOutcome.INSUFFICIENT_CLUSTERS,
            DigestOutcome.STALE_INDEX,
            DigestOutcome.DISABLED,
            DigestOutcome.FAILED,
        ):
            snap = CodebaseCharacterSnapshot(
                outcome=outcome, clusters=(),
                generated_at_ts=1.0, total_corpus_items=0,
                cluster_mode="kmeans", built_at_ts=1.0,
            )
            assert snap.is_ready() is False

    def test_schema_version_pin(self):
        assert (
            CODEBASE_CHARACTER_SCHEMA_VERSION
            == "codebase_character.v1"
        )


# ---------------------------------------------------------------------------
# §B — Frozen records
# ---------------------------------------------------------------------------


class TestFrozenRecords:
    def test_cluster_character_frozen(self):
        cc = ClusterCharacter(
            cluster_id=1, kind="goal", size=3,
            theme_label="voice biometric",
            nearest_item_excerpt="...",
            nearest_item_source="git_commit",
            source_composition=(("git_commit", 3),),
            centroid_hash8="abc12345",
        )
        with pytest.raises(FrozenInstanceError):
            cc.size = 99  # type: ignore[misc]

    def test_snapshot_frozen(self):
        snap = CodebaseCharacterSnapshot(
            outcome=DigestOutcome.DISABLED,
            clusters=(),
            generated_at_ts=1.0,
            total_corpus_items=0,
            cluster_mode="kmeans",
            built_at_ts=1.0,
        )
        with pytest.raises(FrozenInstanceError):
            snap.outcome = DigestOutcome.READY  # type: ignore[misc]

    def test_snapshot_hashable(self):
        snap = CodebaseCharacterSnapshot(
            outcome=DigestOutcome.DISABLED,
            clusters=(),
            generated_at_ts=1.0,
            total_corpus_items=0,
            cluster_mode="kmeans",
            built_at_ts=1.0,
        )
        # frozen dataclass with no Lists → hashable
        {snap}  # noqa: B015


# ---------------------------------------------------------------------------
# §C — Env knobs (clamps + asymmetric defaults)
# ---------------------------------------------------------------------------


class TestMasterFlag:
    def test_default_true_post_graduation(self, monkeypatch):
        # Graduated 2026-05-02 (Slice 3): default-True.
        monkeypatch.delenv(
            "JARVIS_CODEBASE_CHARACTER_DIGEST_ENABLED", raising=False,
        )
        assert codebase_character_enabled() is True

    @pytest.mark.parametrize("v", ["1", "true", "yes", "on", "TRUE"])
    def test_truthy(self, monkeypatch, v):
        monkeypatch.setenv(
            "JARVIS_CODEBASE_CHARACTER_DIGEST_ENABLED", v,
        )
        assert codebase_character_enabled() is True

    @pytest.mark.parametrize(
        "v", ["0", "false", "no", "off", "garbage"],
    )
    def test_falsy(self, monkeypatch, v):
        monkeypatch.setenv(
            "JARVIS_CODEBASE_CHARACTER_DIGEST_ENABLED", v,
        )
        assert codebase_character_enabled() is False

    @pytest.mark.parametrize("v", ["", "   ", "\t"])
    def test_empty_unset_post_graduation(self, monkeypatch, v):
        # Empty / whitespace = unset → graduated default-True.
        monkeypatch.setenv(
            "JARVIS_CODEBASE_CHARACTER_DIGEST_ENABLED", v,
        )
        assert codebase_character_enabled() is True


class TestMinClusters:
    def test_default(self):
        assert min_clusters() == 2

    def test_floor(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CODEBASE_CHARACTER_MIN_CLUSTERS", "0",
        )
        assert min_clusters() == 1

    def test_ceiling(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CODEBASE_CHARACTER_MIN_CLUSTERS", "999",
        )
        assert min_clusters() == 16

    def test_garbage(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CODEBASE_CHARACTER_MIN_CLUSTERS", "abc",
        )
        assert min_clusters() == 2


class TestStaleAfterS:
    def test_default(self):
        assert stale_after_s() == 86400.0

    def test_floor(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CODEBASE_CHARACTER_STALE_AFTER_S", "0",
        )
        assert stale_after_s() == 60.0

    def test_ceiling(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CODEBASE_CHARACTER_STALE_AFTER_S", "9999999",
        )
        assert stale_after_s() == 604800.0

    def test_garbage(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CODEBASE_CHARACTER_STALE_AFTER_S", "xyz",
        )
        assert stale_after_s() == 86400.0


class TestMaxClustersInDigest:
    def test_default(self):
        assert max_clusters_in_digest() == 8

    def test_floor(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CODEBASE_CHARACTER_MAX_CLUSTERS_IN_DIGEST",
            "0",
        )
        assert max_clusters_in_digest() == 1

    def test_ceiling(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CODEBASE_CHARACTER_MAX_CLUSTERS_IN_DIGEST",
            "999",
        )
        assert max_clusters_in_digest() == 32


class TestExcerptMaxChars:
    def test_default(self):
        assert excerpt_max_chars() == 140

    def test_floor(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CODEBASE_CHARACTER_EXCERPT_MAX_CHARS", "0",
        )
        assert excerpt_max_chars() == 40

    def test_ceiling(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CODEBASE_CHARACTER_EXCERPT_MAX_CHARS", "9999",
        )
        assert excerpt_max_chars() == 400


# ---------------------------------------------------------------------------
# §D — Decision tree (compute_codebase_character)
# ---------------------------------------------------------------------------


class TestDecisionTreeDisabled:
    def test_master_off_returns_disabled(self):
        snap = compute_codebase_character(
            enabled=False, clusters=[_mk_cluster()],
            cluster_mode="kmeans", total_corpus_items=10,
            built_at_ts=time.time(), generated_at_ts=time.time(),
        )
        assert snap.outcome is DigestOutcome.DISABLED
        assert snap.clusters == ()
        assert snap.is_ready() is False


class TestDecisionTreeStale:
    def test_built_at_zero_returns_stale(self):
        snap = compute_codebase_character(
            enabled=True, clusters=[_mk_cluster(), _mk_cluster(2)],
            cluster_mode="kmeans", total_corpus_items=10,
            built_at_ts=0.0, generated_at_ts=time.time(),
        )
        assert snap.outcome is DigestOutcome.STALE_INDEX

    def test_aged_build_returns_stale(self):
        now = time.time()
        snap = compute_codebase_character(
            enabled=True, clusters=[_mk_cluster(), _mk_cluster(2)],
            cluster_mode="kmeans", total_corpus_items=10,
            built_at_ts=now - 90000.0,  # > 24h
            generated_at_ts=now,
        )
        assert snap.outcome is DigestOutcome.STALE_INDEX

    def test_fresh_build_passes_stale_check(self):
        now = time.time()
        snap = compute_codebase_character(
            enabled=True, clusters=[_mk_cluster(), _mk_cluster(2)],
            cluster_mode="kmeans", total_corpus_items=10,
            built_at_ts=now - 60.0, generated_at_ts=now,
        )
        assert snap.outcome is not DigestOutcome.STALE_INDEX

    def test_stale_override_via_kwarg(self):
        now = time.time()
        # 30s old, but override sets stale_after to 10s
        snap = compute_codebase_character(
            enabled=True, clusters=[_mk_cluster(), _mk_cluster(2)],
            cluster_mode="kmeans", total_corpus_items=10,
            built_at_ts=now - 30.0, generated_at_ts=now,
            stale_after_s_override=10.0,
        )
        assert snap.outcome is DigestOutcome.STALE_INDEX


class TestDecisionTreeInsufficient:
    def test_no_clusters_returns_insufficient(self):
        now = time.time()
        snap = compute_codebase_character(
            enabled=True, clusters=[], cluster_mode="kmeans",
            total_corpus_items=10, built_at_ts=now,
            generated_at_ts=now,
        )
        assert snap.outcome is DigestOutcome.INSUFFICIENT_CLUSTERS

    def test_below_floor_returns_insufficient(self):
        now = time.time()
        snap = compute_codebase_character(
            enabled=True, clusters=[_mk_cluster()],
            cluster_mode="kmeans", total_corpus_items=10,
            built_at_ts=now, generated_at_ts=now,
            min_cluster_floor=2,
        )
        assert snap.outcome is DigestOutcome.INSUFFICIENT_CLUSTERS

    def test_at_floor_returns_ready(self):
        now = time.time()
        snap = compute_codebase_character(
            enabled=True,
            clusters=[_mk_cluster(1), _mk_cluster(2)],
            cluster_mode="kmeans", total_corpus_items=10,
            built_at_ts=now, generated_at_ts=now,
            min_cluster_floor=2,
        )
        assert snap.outcome is DigestOutcome.READY


class TestDecisionTreeReady:
    def test_basic_ready_snapshot(self):
        now = time.time()
        clusters = [
            _mk_cluster(
                1, "goal", 5,
                "Voice biometric authentication primitive",
            ),
            _mk_cluster(
                2, "conversation", 3,
                "Ghost hands keyboard automation",
                comp=(("conversation", 3),),
            ),
        ]
        snap = compute_codebase_character(
            enabled=True, clusters=clusters,
            cluster_mode="kmeans", total_corpus_items=20,
            built_at_ts=now, generated_at_ts=now,
        )
        assert snap.outcome is DigestOutcome.READY
        assert len(snap.clusters) == 2
        assert snap.is_ready() is True
        assert snap.total_corpus_items == 20
        assert snap.cluster_mode == "kmeans"

    def test_ordering_size_desc_then_id_asc(self):
        # Three clusters, ascending IDs but mixed sizes; output must
        # be size desc, then cluster_id asc.
        now = time.time()
        clusters = [
            _mk_cluster(1, "goal", 3, "alpha"),
            _mk_cluster(2, "goal", 7, "beta"),
            _mk_cluster(3, "goal", 7, "gamma"),
        ]
        snap = compute_codebase_character(
            enabled=True, clusters=clusters,
            cluster_mode="kmeans", total_corpus_items=10,
            built_at_ts=now, generated_at_ts=now,
        )
        ids = [c.cluster_id for c in snap.clusters]
        # 2 and 3 tie at size=7 → ID ascending; 1 at size=3 last.
        assert ids == [2, 3, 1]

    def test_max_clusters_cap_truncates(self):
        now = time.time()
        clusters = [_mk_cluster(i, "goal", 10) for i in range(20)]
        snap = compute_codebase_character(
            enabled=True, clusters=clusters,
            cluster_mode="kmeans", total_corpus_items=200,
            built_at_ts=now, generated_at_ts=now,
            max_clusters_cap=5,
        )
        assert snap.outcome is DigestOutcome.READY
        assert len(snap.clusters) == 5
        assert snap.truncated_count == 15

    def test_theme_label_extraction(self):
        now = time.time()
        c = _mk_cluster(
            1, "goal", 5,
            "Voice biometric authentication via WebAuthn",
        )
        snap = compute_codebase_character(
            enabled=True, clusters=[c, _mk_cluster(2)],
            cluster_mode="kmeans", total_corpus_items=10,
            built_at_ts=now, generated_at_ts=now,
        )
        # First 4 alphanumeric tokens lowercased.
        assert snap.clusters[0].theme_label == (
            "voice biometric authentication via"
        )

    def test_excerpt_truncation_at_word_boundary(self):
        now = time.time()
        long_text = "word " * 80  # 400 chars
        c = _mk_cluster(1, "goal", 5, long_text)
        snap = compute_codebase_character(
            enabled=True, clusters=[c, _mk_cluster(2)],
            cluster_mode="kmeans", total_corpus_items=10,
            built_at_ts=now, generated_at_ts=now,
            excerpt_chars=50,
        )
        excerpt = snap.clusters[0].nearest_item_excerpt
        assert len(excerpt) <= 53  # 50 + "..."
        assert excerpt.endswith("...")
        # Word boundary preserved — no partial "wor"
        assert "wor..." not in excerpt or excerpt.endswith(
            "word...",
        )


# ---------------------------------------------------------------------------
# §E — Total guarantee (NEVER raises)
# ---------------------------------------------------------------------------


class TestTotalGuarantee:
    def test_garbage_cluster_objects_dont_raise(self):
        # Cluster missing all attributes — we silently coerce.
        class _Empty:
            pass
        now = time.time()
        snap = compute_codebase_character(
            enabled=True,
            clusters=[_Empty(), _Empty()],  # type: ignore[list-item]
            cluster_mode="kmeans", total_corpus_items=2,
            built_at_ts=now, generated_at_ts=now,
        )
        # Either READY (after coercion) or FAILED — but never raises.
        assert snap.outcome in DigestOutcome

    def test_explicit_attribute_exception_returns_failed(self):
        class _Bomb:
            cluster_id = 1
            kind = "goal"
            size = 1

            @property
            def nearest_item_text(self):
                raise RuntimeError("boom from property")

            nearest_item_source = "git_commit"
            source_composition = ()
            centroid_hash8 = "x"
        now = time.time()
        snap = compute_codebase_character(
            enabled=True,
            clusters=[_Bomb(), _Bomb()],  # type: ignore[list-item]
            cluster_mode="kmeans", total_corpus_items=2,
            built_at_ts=now, generated_at_ts=now,
        )
        # Must not propagate — total guarantee.
        assert snap.outcome in DigestOutcome

    def test_negative_built_at_treated_as_unbuilt(self):
        snap = compute_codebase_character(
            enabled=True, clusters=[_mk_cluster(), _mk_cluster(2)],
            cluster_mode="kmeans", total_corpus_items=2,
            built_at_ts=-1.0, generated_at_ts=time.time(),
        )
        assert snap.outcome is DigestOutcome.STALE_INDEX

    def test_none_safe_inputs_dont_crash(self):
        snap = compute_codebase_character(
            enabled=True, clusters=[],
            cluster_mode="", total_corpus_items=0,
            built_at_ts=0.0, generated_at_ts=0.0,
        )
        assert snap.outcome in DigestOutcome


# ---------------------------------------------------------------------------
# §F — to_dict / to_prompt_section
# ---------------------------------------------------------------------------


class TestToDict:
    def test_disabled_to_dict(self):
        snap = compute_codebase_character(
            enabled=False, clusters=[],
            cluster_mode="kmeans", total_corpus_items=0,
            built_at_ts=time.time(), generated_at_ts=time.time(),
        )
        d = snap.to_dict()
        assert d["schema_version"] == CODEBASE_CHARACTER_SCHEMA_VERSION
        assert d["outcome"] == "disabled"
        assert d["clusters"] == []
        assert "failure_reason" in d

    def test_ready_to_dict_round_trip_safe(self):
        now = time.time()
        snap = compute_codebase_character(
            enabled=True,
            clusters=[
                _mk_cluster(
                    1, "goal", 5,
                    "Voice biometric authentication",
                ),
                _mk_cluster(2, "conversation", 3, "ghost hands"),
            ],
            cluster_mode="kmeans", total_corpus_items=10,
            built_at_ts=now, generated_at_ts=now,
        )
        d = snap.to_dict()
        assert d["outcome"] == "ready"
        assert len(d["clusters"]) == 2
        # All values JSON-serializable.
        import json
        json.dumps(d)


class TestToPromptSection:
    def test_disabled_renders_empty(self):
        snap = compute_codebase_character(
            enabled=False, clusters=[],
            cluster_mode="kmeans", total_corpus_items=0,
            built_at_ts=time.time(), generated_at_ts=time.time(),
        )
        assert snap.to_prompt_section() == ""

    def test_failed_renders_empty(self):
        snap = CodebaseCharacterSnapshot(
            outcome=DigestOutcome.FAILED, clusters=(),
            generated_at_ts=1.0, total_corpus_items=0,
            cluster_mode="", built_at_ts=0.0,
            failure_reason="boom",
        )
        assert snap.to_prompt_section() == ""

    def test_ready_renders_header_and_clusters(self):
        now = time.time()
        snap = compute_codebase_character(
            enabled=True,
            clusters=[
                _mk_cluster(
                    1, "goal", 5,
                    "Voice biometric authentication primitive",
                ),
                _mk_cluster(
                    2, "conversation", 3,
                    "Ghost hands UI automation",
                    comp=(("conversation", 3),),
                ),
            ],
            cluster_mode="kmeans", total_corpus_items=20,
            built_at_ts=now, generated_at_ts=now,
        )
        rendered = snap.to_prompt_section()
        assert rendered.startswith("## Codebase Character\n")
        assert "Total corpus items: 20" in rendered
        assert "Cluster mode: kmeans" in rendered
        # Theme labels are lowercased; representative excerpts
        # preserve original casing.
        assert "voice biometric authentication" in rendered
        assert "ghost hands" in rendered.lower()
        assert "Ghost hands UI automation" in rendered
        assert "kind=goal" in rendered
        assert "kind=conversation" in rendered
        assert "Authority over Iron Gate" in rendered

    def test_max_chars_truncates_clusters_not_split(self):
        now = time.time()
        clusters = [
            _mk_cluster(
                i, "goal", 10,
                f"theme {i} " + ("filler " * 30),
            )
            for i in range(8)
        ]
        snap = compute_codebase_character(
            enabled=True, clusters=clusters,
            cluster_mode="kmeans", total_corpus_items=80,
            built_at_ts=now, generated_at_ts=now,
        )
        # Without cap: rendered is large.
        full = snap.to_prompt_section()
        assert len(full) > 800
        # With cap: trimmed but not mid-cluster.
        trimmed = snap.to_prompt_section(max_chars=600)
        assert len(trimmed) <= 600 or trimmed == ""
        if trimmed:
            # Must still contain the header and at least one cluster
            # body if any cluster fits.
            assert "## Codebase Character" in trimmed

    def test_ready_with_no_clusters_still_renders_empty(self):
        # Construct snapshot directly with READY but no clusters
        # (defense-in-depth — compute_codebase_character would never
        # produce this).
        snap = CodebaseCharacterSnapshot(
            outcome=DigestOutcome.READY, clusters=(),
            generated_at_ts=1.0, total_corpus_items=0,
            cluster_mode="kmeans", built_at_ts=1.0,
        )
        assert snap.to_prompt_section() == ""


# ---------------------------------------------------------------------------
# §G — Authority pins (no caller imports, no exec/eval/compile,
#                     no asyncio, no file I/O, no network)
# ---------------------------------------------------------------------------


def _module_imports() -> set:
    src = inspect.getsource(cc_mod)
    tree = ast.parse(src)
    imports: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.add(node.module)
    return imports


class TestAuthorityPins:
    def test_no_caller_module_imports(self):
        # Substrate must NOT import its consumers — no orchestrator,
        # candidate_generator, urgency_router, semantic_index,
        # strategic_direction, proactive_exploration_sensor,
        # iron_gate, risk_tier, change_engine, gate, policy.
        forbidden = {
            "orchestrator", "urgency_router", "semantic_index",
            "strategic_direction", "iron_gate", "risk_tier",
            "change_engine", "gate", "policy",
            "candidate_generator",
            "proactive_exploration_sensor",
        }
        imports = _module_imports()
        for imp in imports:
            for f in forbidden:
                assert f not in imp.split("."), (
                    f"forbidden caller-side import: {imp}"
                )

    def test_pure_stdlib_no_backend_imports(self):
        # PURE-stdlib substrate at Slice 1. Slice 3 graduation
        # introduced two registration-contract imports
        # (FlagRegistry + shipped_code_invariants) — both are
        # outbound description channels (substrate publishes
        # metadata, not consumes behavior). Everything else MUST
        # remain stdlib so the substrate stays caller-agnostic.
        ALLOWED_BACKEND_IMPORTS = {
            "backend.core.ouroboros.governance.flag_registry",
            "backend.core.ouroboros.governance.meta.shipped_code_invariants",
        }
        imports = _module_imports()
        for imp in imports:
            if imp.startswith("backend."):
                assert imp in ALLOWED_BACKEND_IMPORTS, (
                    f"non-stdlib import: {imp}"
                )

    def test_no_exec_eval_compile(self):
        src = inspect.getsource(cc_mod)
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                if isinstance(fn, ast.Name) and fn.id in (
                    "exec", "eval", "compile",
                ):
                    pytest.fail(
                        f"forbidden {fn.id} call at "
                        f"line {node.lineno}",
                    )

    def test_no_asyncio_import(self):
        imports = _module_imports()
        assert "asyncio" not in imports

    def test_no_file_io(self):
        # No open/write/read calls in module body.
        src = inspect.getsource(cc_mod)
        tree = ast.parse(src)
        forbidden_calls = {"open"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                if isinstance(fn, ast.Name) and fn.id in forbidden_calls:
                    pytest.fail(
                        f"forbidden {fn.id} call at "
                        f"line {node.lineno}",
                    )

    def test_no_network_imports(self):
        imports = _module_imports()
        for net in ("http", "urllib", "socket", "requests"):
            for imp in imports:
                assert not imp.startswith(net), (
                    f"forbidden network import: {imp}"
                )

    def test_compute_function_has_no_raise_in_body(self):
        # Total-function pin (the same shape as AdmissionGate's
        # compute_admission_decision_total). Outer try/except IS the
        # whole function body — no top-level ``raise``.
        src = inspect.getsource(compute_codebase_character)
        tree = ast.parse(src)
        # Walk the FunctionDef body for top-level raises (excluding
        # nested classes/functions and except handlers).
        fn = tree.body[0]
        assert isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
        for node in ast.walk(fn):
            if isinstance(node, ast.Raise):
                pytest.fail(
                    f"compute_codebase_character has unguarded "
                    f"raise at line {node.lineno}",
                )


# ---------------------------------------------------------------------------
# §H — Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_byte_stable_across_calls(self):
        now = 1700000000.0
        clusters = [
            _mk_cluster(1, "goal", 5, "voice bio"),
            _mk_cluster(2, "conversation", 3, "ghost hands"),
        ]
        a = compute_codebase_character(
            enabled=True, clusters=clusters,
            cluster_mode="kmeans", total_corpus_items=10,
            built_at_ts=now, generated_at_ts=now,
        )
        b = compute_codebase_character(
            enabled=True, clusters=clusters,
            cluster_mode="kmeans", total_corpus_items=10,
            built_at_ts=now, generated_at_ts=now,
        )
        assert a.to_dict() == b.to_dict()
        assert a.to_prompt_section() == b.to_prompt_section()

    def test_theme_label_byte_stable(self):
        from backend.core.ouroboros.governance.codebase_character import (  # noqa: E501
            _extract_theme_label,
        )
        for _ in range(5):
            assert _extract_theme_label(
                "Voice biometric AUTHENTICATION primitive",
            ) == "voice biometric authentication primitive"
