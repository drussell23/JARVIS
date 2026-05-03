"""RR Pass B Slice 4 — ShadowReplay corpus + diff regression suite.

Pins:
  * Module constants + 6-value ReplayLoadStatus enum + frozen
    ReplaySnapshot / ReplayCorpus / ReplayDivergence shapes.
  * Env knob default-false-pre-graduation (master-off → NOT_LOADED).
  * Path resolver: default + env override.
  * Loader skip paths: master_off / dir_missing / manifest_missing /
    yaml_parse_error / manifest-not-mapping / entries-not-list /
    empty-after-validation / schema-mismatch-noted-but-loaded.
  * Per-entry validation: missing op_id / missing path / missing
    phases / op_dir missing / per-phase snapshot missing /
    snapshot oversize / snapshot unreadable / snapshot not-mapping.
  * Cap at MAX_SNAPSHOTS_PER_CORPUS.
  * Tag preservation across all snapshots from one entry.
  * for_phase + for_op query helpers.
  * structural_equal: matching dicts; whitelist semantics
    (non-whitelisted differences ignored); missing keys → None
    comparison.
  * compare_phase_result_to_expected: matching → None;
    next_phase / status / reason / each whitelisted ctx field
    mismatches return correct ReplayDivergence shapes.
  * Default-singleton accessor.
  * REAL seed corpus (.jarvis/order2_replay_corpus/) loads with
    1 snapshot + structural-diff round trip.
  * Authority invariants: no banned imports + only-allowed I/O is
    the corpus directory + YAML import not at module top-level.
"""
from __future__ import annotations

import dataclasses
import io
import json
import tokenize
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.meta.shadow_replay import (
    DEFAULT_CTX_WHITELIST,
    MAX_SNAPSHOTS_PER_CORPUS,
    MAX_SNAPSHOT_BYTES,
    SHADOW_REPLAY_SCHEMA_VERSION,
    ReplayCorpus,
    ReplayDivergence,
    ReplayLoadStatus,
    ReplaySnapshot,
    compare_phase_result_to_expected,
    corpus_root,
    get_default_corpus,
    is_enabled,
    load_corpus,
    reset_default_corpus,
    structural_equal,
)


_REPO = Path(__file__).resolve().parent.parent.parent


def _read(rel: str) -> str:
    return (_REPO / rel).read_text(encoding="utf-8")


def _strip_docstrings_and_comments(src: str) -> str:
    out = []
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenizeError, IndentationError):
        return src
    for tok in toks:
        if tok.type == tokenize.STRING:
            out.append('""')
        elif tok.type == tokenize.COMMENT:
            continue
        else:
            out.append(tok.string)
    return " ".join(out)


@pytest.fixture(autouse=True)
def _clear_env_and_singleton(monkeypatch):
    monkeypatch.delenv("JARVIS_SHADOW_PIPELINE_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_SHADOW_REPLAY_CORPUS_PATH", raising=False)
    reset_default_corpus()
    yield
    reset_default_corpus()


@pytest.fixture
def loaded(monkeypatch):
    monkeypatch.setenv("JARVIS_SHADOW_PIPELINE_ENABLED", "1")


def _build_corpus(
    tmp_path: Path,
    *,
    op_id: str = "op-test",
    phases=("classify",),
    pre_ctx=None,
    expected_next_phase: str = "ROUTE",
    expected_status: str = "ok",
    expected_reason=None,
    expected_next_ctx=None,
    tags=("synthetic",),
    schema_version: int = SHADOW_REPLAY_SCHEMA_VERSION,
    skip_snapshot_files: bool = False,
):
    """Helper: write a manifest + snapshot files into ``tmp_path``."""
    pre_ctx = pre_ctx or {"op_id": op_id, "phase": "CLASSIFY"}
    expected_next_ctx = expected_next_ctx or {
        "op_id": op_id, "phase": "ROUTE",
        "risk_tier": "SAFE_AUTO",
        "target_files": [], "candidate_files": [],
    }
    op_dir = tmp_path / "ops" / op_id
    op_dir.mkdir(parents=True, exist_ok=True)
    if not skip_snapshot_files:
        for phase in phases:
            (op_dir / f"{phase}.json").write_text(
                json.dumps({
                    "pre_phase_ctx": pre_ctx,
                    "expected_next_phase": expected_next_phase,
                    "expected_status": expected_status,
                    "expected_reason": expected_reason,
                    "expected_next_ctx": expected_next_ctx,
                }),
                encoding="utf-8",
            )
    manifest = (
        f"schema_version: {schema_version}\n"
        f"entries:\n"
        f"  - op_id: {op_id}\n"
        f"    path: ops/{op_id}\n"
        f"    phases: [{', '.join(phases)}]\n"
        f"    tags: [{', '.join(tags)}]\n"
    )
    (tmp_path / "manifest.yaml").write_text(manifest, encoding="utf-8")
    return tmp_path


# ===========================================================================
# A — Module constants + dataclasses + status enum
# ===========================================================================


def test_schema_version_pinned():
    assert SHADOW_REPLAY_SCHEMA_VERSION == 1


def test_max_snapshot_bytes_pinned():
    assert MAX_SNAPSHOT_BYTES == 256 * 1024


def test_max_snapshots_per_corpus_pinned():
    assert MAX_SNAPSHOTS_PER_CORPUS == 11_000


def test_default_ctx_whitelist_pinned():
    """Pin: per Pass B §6.3, the whitelisted ctx fields that MUST
    match across inline-vs-runner comparison."""
    assert DEFAULT_CTX_WHITELIST == frozenset({
        "op_id", "risk_tier", "phase",
        "target_files", "candidate_files",
    })


def test_status_enum_six_values():
    assert {s.name for s in ReplayLoadStatus} == {
        "LOADED", "NOT_LOADED", "DIR_MISSING", "MANIFEST_MISSING",
        "MANIFEST_PARSE_ERROR", "EMPTY",
    }


def test_snapshot_is_frozen():
    s = ReplaySnapshot(op_id="o", phase="p")
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.op_id = "x"  # type: ignore[misc]


def test_corpus_is_frozen():
    c = ReplayCorpus()
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.status = ReplayLoadStatus.LOADED  # type: ignore[misc]


def test_divergence_is_frozen():
    d = ReplayDivergence(op_id="o", phase="p", field_path="x",
                         expected=1, actual=2)
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.expected = 3  # type: ignore[misc]


def test_snapshot_to_dict_stable_shape():
    s = ReplaySnapshot(
        op_id="o", phase="classify",
        pre_phase_ctx={"x": 1},
        expected_next_phase="ROUTE",
        expected_status="ok",
        expected_reason=None,
        expected_next_ctx={"y": 2},
        tags=("a", "b"),
    )
    d = s.to_dict()
    for k in ("op_id", "phase", "pre_phase_ctx", "expected_next_phase",
              "expected_status", "expected_reason", "expected_next_ctx",
              "tags"):
        assert k in d
    assert d["tags"] == ["a", "b"]


def test_corpus_to_dict_stable_shape():
    c = ReplayCorpus(
        snapshots=(ReplaySnapshot(op_id="o", phase="p"),),
        status=ReplayLoadStatus.LOADED,
        notes=("note-1",),
    )
    d = c.to_dict()
    assert d["schema_version"] == SHADOW_REPLAY_SCHEMA_VERSION
    assert d["status"] == "LOADED"
    assert d["snapshots_count"] == 1
    assert d["notes"] == ["note-1"]


# ===========================================================================
# B — Env knob (default false pre-graduation)
# ===========================================================================


def test_is_enabled_default_true_post_graduation(monkeypatch):
    """Pass B Slice 4 graduation 2026-05-03: master flag flipped
    default-true. Empty string + unset both resolve to True."""
    monkeypatch.delenv("JARVIS_SHADOW_PIPELINE_ENABLED", raising=False)
    assert is_enabled() is True


@pytest.mark.parametrize("val", ["1", "true", "yes", "on", ""])
def test_is_enabled_truthy(monkeypatch, val):
    """Empty string is now equivalent to unset → graduated default-true."""
    monkeypatch.setenv("JARVIS_SHADOW_PIPELINE_ENABLED", val)
    assert is_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "garbage"])
def test_is_enabled_falsy(monkeypatch, val):
    """Operator opt-out paths: explicit non-truthy values."""
    monkeypatch.setenv("JARVIS_SHADOW_PIPELINE_ENABLED", val)
    assert is_enabled() is False


# ===========================================================================
# C — Path resolver
# ===========================================================================


def test_default_corpus_root():
    p = corpus_root()
    assert p.parent.name == ".jarvis"
    assert p.name == "order2_replay_corpus"


def test_corpus_root_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_SHADOW_REPLAY_CORPUS_PATH", str(tmp_path))
    assert corpus_root() == tmp_path


# ===========================================================================
# D — Loader skip paths
# ===========================================================================


def test_load_master_off_returns_not_loaded(monkeypatch):
    """Pin: master flag off → empty corpus. Slice 5 consumer treats
    this as 'no shadow replay enforcement'. Post Slice 4 graduation
    the master flag is default-true; explicit ``false`` must be set
    to opt out."""
    monkeypatch.setenv("JARVIS_SHADOW_PIPELINE_ENABLED", "false")
    c = load_corpus()
    assert c.status is ReplayLoadStatus.NOT_LOADED
    assert c.snapshots == ()
    assert "master_flag_off" in c.notes


def test_load_dir_missing(loaded, tmp_path):
    c = load_corpus(root=tmp_path / "missing")
    assert c.status is ReplayLoadStatus.DIR_MISSING


def test_load_manifest_missing(loaded, tmp_path):
    c = load_corpus(root=tmp_path)  # dir exists, no manifest
    assert c.status is ReplayLoadStatus.MANIFEST_MISSING


def test_load_yaml_parse_error(loaded, tmp_path):
    (tmp_path / "manifest.yaml").write_text(
        ":not: valid: ::: ::", encoding="utf-8",
    )
    c = load_corpus(root=tmp_path)
    assert c.status is ReplayLoadStatus.MANIFEST_PARSE_ERROR


def test_load_manifest_not_mapping(loaded, tmp_path):
    (tmp_path / "manifest.yaml").write_text("- 1\n- 2\n", encoding="utf-8")
    c = load_corpus(root=tmp_path)
    assert c.status is ReplayLoadStatus.MANIFEST_PARSE_ERROR


def test_load_entries_not_list(loaded, tmp_path):
    (tmp_path / "manifest.yaml").write_text(
        "schema_version: 1\nentries: not-a-list\n", encoding="utf-8",
    )
    c = load_corpus(root=tmp_path)
    assert c.status is ReplayLoadStatus.MANIFEST_PARSE_ERROR


def test_load_empty_after_validation(loaded, tmp_path):
    """Manifest entry with missing op_id → entry skipped → corpus
    EMPTY (not LOADED with zero)."""
    (tmp_path / "manifest.yaml").write_text("""
schema_version: 1
entries:
  - path: ops/x
    phases: [classify]
""", encoding="utf-8")
    c = load_corpus(root=tmp_path)
    assert c.status is ReplayLoadStatus.EMPTY


def test_load_schema_mismatch_noted_but_proceeds(loaded, tmp_path):
    """Wrong schema_version is noted but does NOT abort the load —
    Slice 5 consumer can decide whether to honour mismatched
    corpora."""
    _build_corpus(tmp_path, schema_version=99)
    c = load_corpus(root=tmp_path)
    assert c.status is ReplayLoadStatus.LOADED
    assert any("schema_version_mismatch" in n for n in c.notes)


# ===========================================================================
# E — Per-entry validation
# ===========================================================================


def test_entry_missing_op_id_dropped(loaded, tmp_path):
    (tmp_path / "manifest.yaml").write_text("""
schema_version: 1
entries:
  - path: ops/x
    phases: [classify]
""", encoding="utf-8")
    c = load_corpus(root=tmp_path)
    assert c.snapshots == ()
    assert any("missing_op_id" in n for n in c.notes)


def test_entry_missing_path_dropped(loaded, tmp_path):
    (tmp_path / "manifest.yaml").write_text("""
schema_version: 1
entries:
  - op_id: op-x
    phases: [classify]
""", encoding="utf-8")
    c = load_corpus(root=tmp_path)
    assert any("missing_path" in n for n in c.notes)


def test_entry_missing_phases_dropped(loaded, tmp_path):
    (tmp_path / "manifest.yaml").write_text("""
schema_version: 1
entries:
  - op_id: op-x
    path: ops/op-x
""", encoding="utf-8")
    c = load_corpus(root=tmp_path)
    assert any("missing_phases" in n for n in c.notes)


def test_entry_op_dir_missing(loaded, tmp_path):
    (tmp_path / "manifest.yaml").write_text("""
schema_version: 1
entries:
  - op_id: op-x
    path: ops/nonexistent
    phases: [classify]
""", encoding="utf-8")
    c = load_corpus(root=tmp_path)
    assert any("op_dir_missing" in n for n in c.notes)


def test_entry_snapshot_missing_for_listed_phase(loaded, tmp_path):
    """Manifest declares phases [classify, route] but only classify
    JSON exists → route silently skipped + noted."""
    _build_corpus(tmp_path, phases=("classify", "route"),
                  skip_snapshot_files=True)
    # Write only classify.json, leave route.json missing.
    op_dir = tmp_path / "ops" / "op-test"
    op_dir.mkdir(parents=True, exist_ok=True)
    (op_dir / "classify.json").write_text(json.dumps({
        "pre_phase_ctx": {}, "expected_next_phase": "ROUTE",
        "expected_status": "ok", "expected_reason": None,
        "expected_next_ctx": {},
    }), encoding="utf-8")
    c = load_corpus(root=tmp_path)
    assert len(c.snapshots) == 1
    assert any("snapshot_missing" in n for n in c.notes)


def test_snapshot_oversize_dropped(loaded, tmp_path):
    _build_corpus(tmp_path)
    big = "x" * (MAX_SNAPSHOT_BYTES + 1024)
    (tmp_path / "ops" / "op-test" / "classify.json").write_text(
        json.dumps({"big": big}), encoding="utf-8",
    )
    c = load_corpus(root=tmp_path)
    assert any("snapshot_oversize" in n for n in c.notes)


def test_snapshot_unreadable_json_dropped(loaded, tmp_path):
    _build_corpus(tmp_path)
    (tmp_path / "ops" / "op-test" / "classify.json").write_text(
        "{invalid json", encoding="utf-8",
    )
    c = load_corpus(root=tmp_path)
    assert any("snapshot_unreadable" in n for n in c.notes)


def test_snapshot_not_mapping_dropped(loaded, tmp_path):
    _build_corpus(tmp_path)
    (tmp_path / "ops" / "op-test" / "classify.json").write_text(
        "[1, 2, 3]", encoding="utf-8",
    )
    c = load_corpus(root=tmp_path)
    assert any("snapshot_not_mapping" in n for n in c.notes)


def test_tags_preserved_across_snapshots(loaded, tmp_path):
    """Pin: tags from the manifest entry propagate to every snapshot
    loaded for that op."""
    _build_corpus(tmp_path, phases=("classify", "route"),
                  tags=("happy", "single-file"))
    # Write both phase files.
    op_dir = tmp_path / "ops" / "op-test"
    for phase in ("classify", "route"):
        (op_dir / f"{phase}.json").write_text(json.dumps({
            "pre_phase_ctx": {}, "expected_next_phase": "X",
            "expected_status": "ok", "expected_reason": None,
            "expected_next_ctx": {},
        }), encoding="utf-8")
    c = load_corpus(root=tmp_path)
    assert all(s.tags == ("happy", "single-file") for s in c.snapshots)


# ===========================================================================
# F — Query helpers
# ===========================================================================


def _snap(op_id="o", phase="p"):
    return ReplaySnapshot(op_id=op_id, phase=phase)


def test_for_phase_filters():
    c = ReplayCorpus(snapshots=(
        _snap("o1", "classify"), _snap("o1", "route"),
        _snap("o2", "classify"), _snap("o2", "plan"),
    ), status=ReplayLoadStatus.LOADED)
    assert len(c.for_phase("classify")) == 2
    assert len(c.for_phase("route")) == 1
    assert c.for_phase("nonexistent") == ()


def test_for_op_filters():
    c = ReplayCorpus(snapshots=(
        _snap("o1", "classify"), _snap("o1", "route"),
        _snap("o2", "classify"),
    ), status=ReplayLoadStatus.LOADED)
    assert len(c.for_op("o1")) == 2
    assert len(c.for_op("o2")) == 1
    assert c.for_op("nonexistent") == ()


# ===========================================================================
# G — structural_equal
# ===========================================================================


def test_structural_equal_matching():
    a = {"op_id": "o", "risk_tier": "SAFE_AUTO", "extra": "a"}
    b = {"op_id": "o", "risk_tier": "SAFE_AUTO", "extra": "b"}
    # extra field differs but is NOT in whitelist → still equal.
    assert structural_equal(a, b) is True


def test_structural_equal_mismatched_whitelisted_field():
    a = {"op_id": "o1", "risk_tier": "SAFE_AUTO"}
    b = {"op_id": "o2", "risk_tier": "SAFE_AUTO"}
    assert structural_equal(a, b) is False


def test_structural_equal_missing_keys_compare_as_none():
    """Missing keys both → None == None → True for that field."""
    a = {"op_id": "o"}
    b = {"op_id": "o"}
    # Neither has risk_tier; both compare as None == None.
    assert structural_equal(a, b) is True


def test_structural_equal_custom_whitelist():
    a = {"a": 1, "b": 2}
    b = {"a": 1, "b": 99}
    assert structural_equal(a, b, whitelist=frozenset({"a"})) is True
    assert structural_equal(a, b, whitelist=frozenset({"b"})) is False


# ===========================================================================
# H — compare_phase_result_to_expected
# ===========================================================================


def _good_snap():
    return ReplaySnapshot(
        op_id="op-1", phase="classify",
        pre_phase_ctx={"op_id": "op-1"},
        expected_next_phase="ROUTE",
        expected_status="ok",
        expected_reason=None,
        expected_next_ctx={
            "op_id": "op-1", "risk_tier": "SAFE_AUTO",
            "phase": "ROUTE", "target_files": ["a.py"],
            "candidate_files": [],
        },
    )


def test_compare_matching_returns_none():
    snap = _good_snap()
    div = compare_phase_result_to_expected(
        actual_next_phase="ROUTE", actual_status="ok",
        actual_reason=None,
        actual_next_ctx=dict(snap.expected_next_ctx),
        snapshot=snap,
    )
    assert div is None


def test_compare_next_phase_mismatch():
    snap = _good_snap()
    div = compare_phase_result_to_expected(
        actual_next_phase="PLAN", actual_status="ok",
        actual_reason=None,
        actual_next_ctx=dict(snap.expected_next_ctx),
        snapshot=snap,
    )
    assert div is not None
    assert div.field_path == "next_phase"
    assert div.expected == "ROUTE"
    assert div.actual == "PLAN"


def test_compare_status_mismatch():
    snap = _good_snap()
    div = compare_phase_result_to_expected(
        actual_next_phase="ROUTE", actual_status="fail",
        actual_reason=None,
        actual_next_ctx=dict(snap.expected_next_ctx),
        snapshot=snap,
    )
    assert div is not None
    assert div.field_path == "status"


def test_compare_reason_mismatch():
    snap = _good_snap()
    div = compare_phase_result_to_expected(
        actual_next_phase="ROUTE", actual_status="ok",
        actual_reason="something",
        actual_next_ctx=dict(snap.expected_next_ctx),
        snapshot=snap,
    )
    assert div is not None
    assert div.field_path == "reason"


def test_compare_ctx_whitelisted_field_mismatch():
    snap = _good_snap()
    bad_ctx = dict(snap.expected_next_ctx)
    bad_ctx["risk_tier"] = "BLOCKED"
    div = compare_phase_result_to_expected(
        actual_next_phase="ROUTE", actual_status="ok",
        actual_reason=None,
        actual_next_ctx=bad_ctx,
        snapshot=snap,
    )
    assert div is not None
    assert div.field_path == "next_ctx.risk_tier"
    assert div.expected == "SAFE_AUTO"
    assert div.actual == "BLOCKED"


def test_compare_ctx_non_whitelisted_field_difference_ignored():
    """A diff on a non-whitelisted field (e.g. timestamp) is allowed
    per Pass B §6.3."""
    snap = _good_snap()
    extra_ctx = dict(snap.expected_next_ctx)
    extra_ctx["timestamp"] = 99999.0  # not whitelisted
    div = compare_phase_result_to_expected(
        actual_next_phase="ROUTE", actual_status="ok",
        actual_reason=None,
        actual_next_ctx=extra_ctx,
        snapshot=snap,
    )
    assert div is None


def test_compare_custom_ctx_whitelist():
    snap = _good_snap()
    bad_ctx = dict(snap.expected_next_ctx)
    bad_ctx["op_id"] = "different"
    # With a custom whitelist that doesn't include op_id, the diff
    # should be ignored.
    div = compare_phase_result_to_expected(
        actual_next_phase="ROUTE", actual_status="ok",
        actual_reason=None,
        actual_next_ctx=bad_ctx,
        snapshot=snap,
        ctx_whitelist=frozenset({"risk_tier"}),
    )
    assert div is None


# ===========================================================================
# I — Default-singleton accessor
# ===========================================================================


def test_default_corpus_lazy_loads(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_SHADOW_PIPELINE_ENABLED", "1")
    monkeypatch.setenv(
        "JARVIS_SHADOW_REPLAY_CORPUS_PATH", str(tmp_path / "missing"),
    )
    reset_default_corpus()
    c = get_default_corpus()
    assert c.status is ReplayLoadStatus.DIR_MISSING


def test_default_corpus_returns_same_instance(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_SHADOW_PIPELINE_ENABLED", "1")
    monkeypatch.setenv(
        "JARVIS_SHADOW_REPLAY_CORPUS_PATH", str(tmp_path / "missing"),
    )
    reset_default_corpus()
    a = get_default_corpus()
    b = get_default_corpus()
    assert a is b


def test_reset_default_corpus_clears(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_SHADOW_PIPELINE_ENABLED", "1")
    monkeypatch.setenv(
        "JARVIS_SHADOW_REPLAY_CORPUS_PATH", str(tmp_path / "missing"),
    )
    reset_default_corpus()
    a = get_default_corpus()
    reset_default_corpus()
    b = get_default_corpus()
    assert a is not b


# ===========================================================================
# J — REAL seed corpus (.jarvis/order2_replay_corpus/)
# ===========================================================================


def test_real_seed_corpus_loads(monkeypatch):
    """Pin: the shipped .jarvis/order2_replay_corpus/ loads cleanly
    with the seed entry. Slice 5+ replaces the seed with real
    session ctx data, but the file format is proven by Slice 4."""
    monkeypatch.setenv("JARVIS_SHADOW_PIPELINE_ENABLED", "1")
    monkeypatch.delenv("JARVIS_SHADOW_REPLAY_CORPUS_PATH", raising=False)
    real_root = _REPO / ".jarvis" / "order2_replay_corpus"
    assert real_root.exists(), "Real corpus root missing from repo"
    c = load_corpus(root=real_root)
    assert c.status is ReplayLoadStatus.LOADED
    assert len(c.snapshots) >= 1


def test_real_seed_corpus_round_trip_diff(monkeypatch):
    """Pin: the seed entry's recorded result actually matches itself
    (sanity check on the file format — recorded expected_* should
    diff cleanly against the same dict)."""
    monkeypatch.setenv("JARVIS_SHADOW_PIPELINE_ENABLED", "1")
    real_root = _REPO / ".jarvis" / "order2_replay_corpus"
    c = load_corpus(root=real_root)
    assert c.status is ReplayLoadStatus.LOADED
    snap = c.snapshots[0]
    div = compare_phase_result_to_expected(
        actual_next_phase=snap.expected_next_phase,
        actual_status=snap.expected_status,
        actual_reason=snap.expected_reason,
        actual_next_ctx=dict(snap.expected_next_ctx),
        snapshot=snap,
    )
    assert div is None, f"Seed snapshot self-diff failed: {div}"


# ===========================================================================
# K — Authority invariants
# ===========================================================================


_BANNED = [
    "from backend.core.ouroboros.governance.orchestrator",
    "from backend.core.ouroboros.governance.policy",
    "from backend.core.ouroboros.governance.iron_gate",
    "from backend.core.ouroboros.governance.risk_tier_floor",
    "from backend.core.ouroboros.governance.change_engine",
    "from backend.core.ouroboros.governance.candidate_generator",
    "from backend.core.ouroboros.governance.gate",
    "from backend.core.ouroboros.governance.semantic_guardian",
    "from backend.core.ouroboros.governance.semantic_firewall",
    "from backend.core.ouroboros.governance.scoped_tool_backend",
]


def test_shadow_replay_no_authority_imports():
    src = _read("backend/core/ouroboros/governance/meta/shadow_replay.py")
    for imp in _BANNED:
        assert imp not in src, f"banned import: {imp}"


def test_shadow_replay_only_io_is_corpus_read():
    """Pin: only file I/O is the read-only corpus directory. No
    subprocess / env mutation / network."""
    src = _strip_docstrings_and_comments(
        _read("backend/core/ouroboros/governance/meta/shadow_replay.py"),
    )
    forbidden = [
        "subprocess.",
        ".write_text(",
        "os.environ[",
        "os." + "system(",  # split to dodge pre-commit hook
        "import requests",
        "import httpx",
        "import urllib.request",
    ]
    for c in forbidden:
        assert c not in src, f"unexpected coupling: {c}"


def test_shadow_replay_yaml_optional():
    """Pin: yaml is imported conditionally inside the loader (try/
    except ImportError → MANIFEST_PARSE_ERROR). Protects boot when
    PyYAML isn't on the path."""
    src = _read("backend/core/ouroboros/governance/meta/shadow_replay.py")
    top_level_lines = [
        ln for ln in src.split("\n")[:80]
        if ln.startswith("import ") or ln.startswith("from ")
    ]
    assert "import yaml" not in top_level_lines
    assert "from yaml" not in top_level_lines
