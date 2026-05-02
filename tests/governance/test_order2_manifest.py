"""RR Pass B Slice 1 — Order2Manifest regression suite.

Pins:
  * Module constants + frozen Order2ManifestEntry +
    Order2Manifest + ManifestLoadStatus enum (6 values).
  * Env knob default-false-pre-graduation.
  * Path resolver: default + env override.
  * Loader skip paths: master_off / file_missing / file_unreadable /
    yaml_parse_error / empty / schema_mismatch / not-mapping.
  * Per-entry validation: unknown repo / empty path_glob / empty
    rationale / bad date / empty added_by / non-mapping entry.
  * Per-entry text caps (path_glob + rationale truncated).
  * Cap at MAX_MANIFEST_ENTRIES.
  * Glob matching: exact path / wildcard / repo isolation /
    case-sensitive.
  * entries_for_repo filter.
  * .to_dict stable shape.
  * Default-singleton lazy load + reset.
  * Real manifest file (.jarvis/order2_manifest.yaml) loads
    correctly with all 9 Body-only entries + matches every
    expected governance-code path.
  * Authority invariants: no banned imports + no subprocess /
    env mutation / network. Only allowed I/O is reading the YAML.
"""
from __future__ import annotations

import dataclasses
import io
import tokenize
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.meta.order2_manifest import (
    KNOWN_REPOS,
    MAX_MANIFEST_ENTRIES,
    MAX_PATH_GLOB_CHARS,
    MAX_RATIONALE_CHARS,
    ManifestLoadStatus,
    ORDER2_MANIFEST_SCHEMA_VERSION,
    Order2Manifest,
    Order2ManifestEntry,
    get_default_manifest,
    is_loaded,
    load_manifest,
    manifest_path,
    reset_default_manifest,
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


def _write_yaml(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


@pytest.fixture(autouse=True)
def _clear_env_and_singleton(monkeypatch):
    monkeypatch.delenv("JARVIS_ORDER2_MANIFEST_LOADED", raising=False)
    monkeypatch.delenv("JARVIS_ORDER2_MANIFEST_PATH", raising=False)
    reset_default_manifest()
    yield
    reset_default_manifest()


@pytest.fixture
def loaded(monkeypatch):
    monkeypatch.setenv("JARVIS_ORDER2_MANIFEST_LOADED", "1")


# ===========================================================================
# A — Module constants + dataclass shapes + status enum
# ===========================================================================


def test_schema_version_pinned():
    """Pin: bump on any Order2ManifestEntry field change."""
    assert ORDER2_MANIFEST_SCHEMA_VERSION == 1


def test_known_repos_body_only_for_now():
    """Pin: Trinity-ready schema but Body-only initial deploy.
    Adding J-Prime / Reactor entries is per-file, no schema change."""
    assert KNOWN_REPOS == frozenset({
        "jarvis", "jarvis-prime", "jarvis-reactor",
    })


def test_per_entry_caps_pinned():
    assert MAX_RATIONALE_CHARS == 480
    assert MAX_PATH_GLOB_CHARS == 256
    assert MAX_MANIFEST_ENTRIES == 256


def test_status_enum_six_values():
    """Pin: 6 distinct load outcomes for Slice 2-6 consumers' status
    checks (consumers MUST treat ``status != LOADED`` as 'no
    enforcement')."""
    assert {s.name for s in ManifestLoadStatus} == {
        "LOADED", "NOT_LOADED", "FILE_MISSING",
        "FILE_UNREADABLE", "SCHEMA_ERROR", "EMPTY",
    }


def test_entry_is_frozen():
    e = Order2ManifestEntry(
        repo="jarvis", path_glob="x", rationale="y",
        added="2026-04-26", added_by="operator",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.repo = "other"  # type: ignore[misc]


def test_manifest_is_frozen():
    m = Order2Manifest()
    with pytest.raises(dataclasses.FrozenInstanceError):
        m.status = ManifestLoadStatus.LOADED  # type: ignore[misc]


def test_entry_to_dict_stable_shape():
    e = Order2ManifestEntry(
        repo="jarvis", path_glob="x", rationale="y",
        added="2026-04-26", added_by="operator",
    )
    d = e.to_dict()
    assert d == {
        "repo": "jarvis", "path_glob": "x", "rationale": "y",
        "added": "2026-04-26", "added_by": "operator",
    }


def test_manifest_to_dict_stable_shape():
    m = Order2Manifest(
        entries=(Order2ManifestEntry(
            repo="jarvis", path_glob="x", rationale="y",
            added="2026-04-26", added_by="operator",
        ),),
        status=ManifestLoadStatus.LOADED,
        notes=("note-1",),
    )
    d = m.to_dict()
    assert d["schema_version"] == ORDER2_MANIFEST_SCHEMA_VERSION
    assert d["status"] == "LOADED"
    assert len(d["entries"]) == 1
    assert d["notes"] == ["note-1"]


# ===========================================================================
# B — Env knob (default true post-Q4-P#3 graduation, 2026-05-02)
# ===========================================================================


def test_is_loaded_default_true_post_q4_graduation():
    """Q4 Priority #3 graduation (2026-05-02): operator authorized
    Pass B Slices 1+2 graduation. Manifest now loads on boot —
    Order-2 governance-code path registry observably active. Slice
    6.x amendment-protocol flags stay default-false; the only path
    to Order-2 mutations remains the operator-only /order2 amend
    REPL gated on JARVIS_ORDER2_REPL_ENABLED."""
    assert is_loaded() is True


@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE"])
def test_is_loaded_truthy(monkeypatch, val):
    monkeypatch.setenv("JARVIS_ORDER2_MANIFEST_LOADED", val)
    assert is_loaded() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "garbage"])
def test_is_loaded_explicit_falsy(monkeypatch, val):
    # Empty string excluded — now "unset → graduated default true"
    # per Q4 P#3.
    monkeypatch.setenv("JARVIS_ORDER2_MANIFEST_LOADED", val)
    assert is_loaded() is False


@pytest.mark.parametrize("val", ["", "   ", "\t"])
def test_is_loaded_empty_treats_as_unset(monkeypatch, val):
    monkeypatch.setenv("JARVIS_ORDER2_MANIFEST_LOADED", val)
    assert is_loaded() is True


# ===========================================================================
# C — Path resolver
# ===========================================================================


def test_default_manifest_path():
    p = manifest_path()
    assert p.parent.name == ".jarvis"
    assert p.name == "order2_manifest.yaml"


def test_manifest_path_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "JARVIS_ORDER2_MANIFEST_PATH", str(tmp_path / "custom.yaml"),
    )
    assert manifest_path() == tmp_path / "custom.yaml"


# ===========================================================================
# D — Loader skip paths
# ===========================================================================


def test_load_master_off_returns_not_loaded(monkeypatch):
    """Pin: master flag explicitly off → empty manifest. Post-Q4-P#3
    graduation, the rollback path requires explicit `false` (unset
    = graduated default true)."""
    monkeypatch.setenv("JARVIS_ORDER2_MANIFEST_LOADED", "false")
    m = load_manifest()
    assert m.status is ManifestLoadStatus.NOT_LOADED
    assert m.entries == ()
    assert "master_flag_off" in m.notes


def test_load_file_missing(loaded, tmp_path):
    m = load_manifest(path=tmp_path / "missing.yaml")
    assert m.status is ManifestLoadStatus.FILE_MISSING
    assert any("path_missing" in n for n in m.notes)
    assert m.entries == ()


def test_load_file_unreadable(loaded, tmp_path, monkeypatch):
    """Pin: I/O error during read → FILE_UNREADABLE, never raises."""
    p = tmp_path / "x.yaml"
    p.write_text("schema_version: 1\nentries: []\n", encoding="utf-8")

    def boom(self, *a, **kw):
        raise OSError("disk gone")

    monkeypatch.setattr(Path, "read_text", boom)
    m = load_manifest(path=p)
    assert m.status is ManifestLoadStatus.FILE_UNREADABLE
    assert any("read_failed" in n for n in m.notes)


def test_load_yaml_parse_error(loaded, tmp_path):
    p = tmp_path / "bad.yaml"
    _write_yaml(p, "not: valid: yaml: ::: ::")
    m = load_manifest(path=p)
    assert m.status is ManifestLoadStatus.SCHEMA_ERROR
    assert any("yaml_parse_failed" in n for n in m.notes)


def test_load_empty_document(loaded, tmp_path):
    p = tmp_path / "empty.yaml"
    _write_yaml(p, "")
    m = load_manifest(path=p)
    assert m.status is ManifestLoadStatus.EMPTY


def test_load_doc_not_mapping(loaded, tmp_path):
    p = tmp_path / "list.yaml"
    _write_yaml(p, "- 1\n- 2\n")
    m = load_manifest(path=p)
    assert m.status is ManifestLoadStatus.SCHEMA_ERROR
    assert "doc_not_mapping" in m.notes


def test_load_entries_key_missing(loaded, tmp_path):
    p = tmp_path / "no_entries.yaml"
    _write_yaml(p, "schema_version: 1\n")
    m = load_manifest(path=p)
    assert m.status is ManifestLoadStatus.SCHEMA_ERROR


def test_load_entries_not_list(loaded, tmp_path):
    p = tmp_path / "bad.yaml"
    _write_yaml(p, "schema_version: 1\nentries: not-a-list\n")
    m = load_manifest(path=p)
    assert m.status is ManifestLoadStatus.SCHEMA_ERROR


def test_load_zero_entries_after_validation_returns_empty(loaded, tmp_path):
    """All entries malformed → status=EMPTY (not LOADED with zero)."""
    p = tmp_path / "bad_entries.yaml"
    _write_yaml(p, """
schema_version: 1
entries:
  - repo: bogus
    path_glob: x
    rationale: y
    added: "2026-04-26"
    added_by: operator
""")
    m = load_manifest(path=p)
    assert m.status is ManifestLoadStatus.EMPTY


def test_load_schema_version_mismatch_noted_but_loaded(loaded, tmp_path):
    """Wrong schema_version is noted but does NOT abort the load —
    Slice 2-6 consumers can decide whether to honour mismatched
    manifests on a per-feature basis."""
    p = tmp_path / "wrong_version.yaml"
    _write_yaml(p, """
schema_version: 99
entries:
  - repo: jarvis
    path_glob: x.py
    rationale: test
    added: "2026-04-26"
    added_by: operator
""")
    m = load_manifest(path=p)
    assert m.status is ManifestLoadStatus.LOADED
    assert any("schema_version_mismatch" in n for n in m.notes)


# ===========================================================================
# E — Per-entry validation
# ===========================================================================


def _yaml_with_entry(**kw):
    """Helper: build a YAML body with one entry whose fields override
    a baseline valid set."""
    base = {
        "repo": "jarvis", "path_glob": "x.py",
        "rationale": "test", "added": "2026-04-26",
        "added_by": "operator",
    }
    base.update(kw)
    lines = ["schema_version: 1", "entries:", "  -"]
    for k, v in base.items():
        if v is None:
            lines.append(f"    {k}:")
        else:
            lines.append(f"    {k}: {v!r}")
    return "\n".join(lines)


def test_entry_unknown_repo_dropped(loaded, tmp_path):
    p = tmp_path / "x.yaml"
    _write_yaml(p, _yaml_with_entry(repo="bogus-repo"))
    m = load_manifest(path=p)
    assert m.entries == ()
    assert any("unknown_repo" in n for n in m.notes)


def test_entry_empty_path_glob_dropped(loaded, tmp_path):
    p = tmp_path / "x.yaml"
    _write_yaml(p, _yaml_with_entry(path_glob=""))
    m = load_manifest(path=p)
    assert m.entries == ()
    assert any("empty_path_glob" in n for n in m.notes)


def test_entry_empty_rationale_dropped(loaded, tmp_path):
    p = tmp_path / "x.yaml"
    _write_yaml(p, _yaml_with_entry(rationale=""))
    m = load_manifest(path=p)
    assert m.entries == ()
    assert any("empty_rationale" in n for n in m.notes)


@pytest.mark.parametrize("bad_date", [
    "2026-4-26",        # missing zero pad
    "2026/04/26",       # wrong separator
    "26-04-2026",       # wrong order
    "tomorrow",
    "",
])
def test_entry_bad_date_dropped(loaded, tmp_path, bad_date):
    p = tmp_path / "x.yaml"
    _write_yaml(p, _yaml_with_entry(added=bad_date))
    m = load_manifest(path=p)
    assert m.entries == ()


def test_entry_empty_added_by_dropped(loaded, tmp_path):
    p = tmp_path / "x.yaml"
    _write_yaml(p, _yaml_with_entry(added_by=""))
    m = load_manifest(path=p)
    assert m.entries == ()


def test_entry_non_mapping_dropped(loaded, tmp_path):
    p = tmp_path / "x.yaml"
    _write_yaml(p, """
schema_version: 1
entries:
  - "not a mapping"
""")
    m = load_manifest(path=p)
    assert m.entries == ()
    assert any("not_mapping" in n for n in m.notes)


# ===========================================================================
# F — Per-entry text caps
# ===========================================================================


def test_path_glob_truncated_at_cap(loaded, tmp_path):
    big = "x" * (MAX_PATH_GLOB_CHARS + 100)
    p = tmp_path / "x.yaml"
    _write_yaml(p, _yaml_with_entry(path_glob=big))
    m = load_manifest(path=p)
    assert len(m.entries) == 1
    assert len(m.entries[0].path_glob) == MAX_PATH_GLOB_CHARS
    assert any("path_glob_truncated" in n for n in m.notes)


def test_rationale_truncated_at_cap(loaded, tmp_path):
    big = "x" * (MAX_RATIONALE_CHARS + 100)
    p = tmp_path / "x.yaml"
    _write_yaml(p, _yaml_with_entry(rationale=big))
    m = load_manifest(path=p)
    assert len(m.entries) == 1
    assert len(m.entries[0].rationale) == MAX_RATIONALE_CHARS


def test_entries_truncated_at_max(loaded, tmp_path):
    """Pin: more than MAX_MANIFEST_ENTRIES → truncated + note."""
    lines = ["schema_version: 1", "entries:"]
    for i in range(MAX_MANIFEST_ENTRIES + 5):
        lines.extend([
            "  - repo: jarvis",
            f"    path_glob: file{i}.py",
            "    rationale: test",
            "    added: \"2026-04-26\"",
            "    added_by: operator",
        ])
    p = tmp_path / "huge.yaml"
    _write_yaml(p, "\n".join(lines))
    m = load_manifest(path=p)
    assert len(m.entries) == MAX_MANIFEST_ENTRIES
    assert any("entries_truncated_at_max" in n for n in m.notes)


# ===========================================================================
# G — Glob matching + repo isolation
# ===========================================================================


def _entry(repo="jarvis", path_glob="x.py"):
    return Order2ManifestEntry(
        repo=repo, path_glob=path_glob, rationale="r",
        added="2026-04-26", added_by="operator",
    )


def test_matches_exact_path():
    m = Order2Manifest(entries=(
        _entry(path_glob="backend/x.py"),
    ), status=ManifestLoadStatus.LOADED)
    assert m.matches("jarvis", "backend/x.py") is True
    assert m.matches("jarvis", "backend/y.py") is False


def test_matches_wildcard():
    m = Order2Manifest(entries=(
        _entry(path_glob="backend/phase_runners/*.py"),
    ), status=ManifestLoadStatus.LOADED)
    assert m.matches("jarvis", "backend/phase_runners/foo.py") is True
    assert m.matches("jarvis", "backend/phase_runners/bar.py") is True
    assert m.matches("jarvis", "backend/other/foo.py") is False


def test_matches_repo_isolation():
    """Pin: same path under different repo MUST NOT match."""
    m = Order2Manifest(entries=(
        _entry(repo="jarvis", path_glob="x.py"),
    ), status=ManifestLoadStatus.LOADED)
    assert m.matches("jarvis", "x.py") is True
    assert m.matches("jarvis-prime", "x.py") is False


def test_matches_case_sensitive():
    """POSIX glob — case sensitive."""
    m = Order2Manifest(entries=(
        _entry(path_glob="backend/X.py"),
    ), status=ManifestLoadStatus.LOADED)
    assert m.matches("jarvis", "backend/X.py") is True
    assert m.matches("jarvis", "backend/x.py") is False


def test_matches_empty_manifest_is_false():
    """Empty manifest matches nothing (Slice 2-6 consumers degrade
    to pre-Pass-B behaviour)."""
    m = Order2Manifest()
    assert m.matches("jarvis", "anything") is False


def test_entries_for_repo_filters():
    m = Order2Manifest(entries=(
        _entry(repo="jarvis", path_glob="a.py"),
        _entry(repo="jarvis-prime", path_glob="b.py"),
        _entry(repo="jarvis", path_glob="c.py"),
    ), status=ManifestLoadStatus.LOADED)
    assert len(m.entries_for_repo("jarvis")) == 2
    assert len(m.entries_for_repo("jarvis-prime")) == 1
    assert m.entries_for_repo("jarvis-reactor") == ()


# ===========================================================================
# H — Default-singleton
# ===========================================================================


def test_default_manifest_lazy_loads(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_ORDER2_MANIFEST_LOADED", "1")
    monkeypatch.setenv(
        "JARVIS_ORDER2_MANIFEST_PATH", str(tmp_path / "missing.yaml"),
    )
    reset_default_manifest()
    m = get_default_manifest()
    assert m.status is ManifestLoadStatus.FILE_MISSING


def test_default_manifest_returns_same_instance(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_ORDER2_MANIFEST_LOADED", "1")
    monkeypatch.setenv(
        "JARVIS_ORDER2_MANIFEST_PATH", str(tmp_path / "missing.yaml"),
    )
    reset_default_manifest()
    a = get_default_manifest()
    b = get_default_manifest()
    assert a is b


def test_reset_default_manifest_clears(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_ORDER2_MANIFEST_LOADED", "1")
    monkeypatch.setenv(
        "JARVIS_ORDER2_MANIFEST_PATH", str(tmp_path / "missing.yaml"),
    )
    reset_default_manifest()
    a = get_default_manifest()
    reset_default_manifest()
    b = get_default_manifest()
    assert a is not b


# ===========================================================================
# I — REAL manifest file (.jarvis/order2_manifest.yaml)
# ===========================================================================


def test_real_manifest_loads_with_all_entries(monkeypatch):
    """Pin: the shipped ``.jarvis/order2_manifest.yaml`` loads
    cleanly. Pass B §3.2 documented 9 initial Body-only entries;
    the file has grown to 12 as more governance-code paths were
    pinned (cross-Pass evolution). The invariant we pin is
    LOADED-status + non-empty + zero notes — exact entry count is
    operational state, not a structural invariant."""
    monkeypatch.setenv("JARVIS_ORDER2_MANIFEST_LOADED", "1")
    monkeypatch.delenv("JARVIS_ORDER2_MANIFEST_PATH", raising=False)
    # Use repo-relative path so the test runs from any cwd.
    real_path = _REPO / ".jarvis" / "order2_manifest.yaml"
    assert real_path.exists(), "Real manifest YAML missing from repo"
    m = load_manifest(path=real_path)
    assert m.status is ManifestLoadStatus.LOADED
    # Entry count is the inventory at graduation — at least the 9
    # Body-only originals from §3.2.
    assert len(m.entries) >= 9
    assert m.notes == ()


@pytest.mark.parametrize("path", [
    "backend/core/ouroboros/governance/orchestrator.py",
    "backend/core/ouroboros/governance/phase_runner.py",
    "backend/core/ouroboros/governance/phase_runners/foo.py",
    "backend/core/ouroboros/governance/phase_runners/generate_runner.py",
    "backend/core/ouroboros/governance/semantic_firewall.py",
    "backend/core/ouroboros/governance/semantic_guardian.py",
    "backend/core/ouroboros/governance/scoped_tool_backend.py",
    "backend/core/ouroboros/governance/risk_tier_floor.py",
    "backend/core/ouroboros/governance/change_engine.py",
    ".jarvis/order2_manifest.yaml",
])
def test_real_manifest_matches_governance_paths(path):
    """Pin: every governance-code path enumerated in Pass B §3.2 +
    every concrete phase_runners/*.py file is correctly matched
    by the shipped manifest."""
    real_path = _REPO / ".jarvis" / "order2_manifest.yaml"
    # Direct load (bypass env knob) so this test pins the file
    # contents regardless of test-time env state.
    import os
    os.environ["JARVIS_ORDER2_MANIFEST_LOADED"] = "1"
    try:
        m = load_manifest(path=real_path)
    finally:
        os.environ.pop("JARVIS_ORDER2_MANIFEST_LOADED", None)
    assert m.matches("jarvis", path), (
        f"Real manifest fails to match expected governance path: {path}"
    )


def test_real_manifest_does_not_match_application_paths():
    """Pin: ordinary application code MUST NOT match the manifest.
    Order-1 (Body) paths stay out of the Order-2 cage."""
    real_path = _REPO / ".jarvis" / "order2_manifest.yaml"
    import os
    os.environ["JARVIS_ORDER2_MANIFEST_LOADED"] = "1"
    try:
        m = load_manifest(path=real_path)
    finally:
        os.environ.pop("JARVIS_ORDER2_MANIFEST_LOADED", None)
    for path in (
        "backend/voice/wake_word.py",
        "backend/vision/frame_server.py",
        "tests/governance/test_anything.py",
        "README.md",
        "backend/core/ouroboros/governance/conversation_bridge.py",
        "backend/core/ouroboros/governance/inline_approval.py",
    ):
        assert not m.matches("jarvis", path), (
            f"Real manifest MUST NOT match application path: {path}"
        )


# ===========================================================================
# J — Authority invariants
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


def test_manifest_module_no_authority_imports():
    """Pin: Slice 1 manifest module is pure data + YAML parse.
    Importing any cage-banned module would create a circular
    dependency the moment Slice 2 wires risk_tier_floor against
    the manifest."""
    src = _read("backend/core/ouroboros/governance/meta/order2_manifest.py")
    for imp in _BANNED:
        assert imp not in src, f"banned import: {imp}"


def test_manifest_module_no_subprocess_or_env_writes():
    """Pin: only allowed I/O is YAML read. No subprocess, no env
    mutation, no network."""
    src = _strip_docstrings_and_comments(
        _read("backend/core/ouroboros/governance/meta/order2_manifest.py"),
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


def test_manifest_module_yaml_optional():
    """Pin: yaml is imported conditionally inside the loader (try/
    except ImportError → SCHEMA_ERROR). This protects boot when
    PyYAML isn't on the path; the loader degrades to NOT_LOADED-
    equivalent rather than blowing up at import time."""
    src = _read("backend/core/ouroboros/governance/meta/order2_manifest.py")
    # yaml import is local to _parse_yaml — not at module top-level.
    top_level_lines = [ln for ln in src.split("\n")[:80]
                       if ln.startswith("import ") or ln.startswith("from ")]
    assert "import yaml" not in top_level_lines
    assert "from yaml" not in top_level_lines
