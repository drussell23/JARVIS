"""Tests for the A1 Black Box Flight Recorder node-side bundler.

TDD spine for ``scripts/a1_black_box.py``. NO real subprocess (git diff is
mocked), NO network, zero spend. Proves the data-preservation contract:

  * the bundler FREEZEs+DUMPs every PRESENT artifact into a compressed
    ``black_box_<run_id>.tar.gz`` + emits a matching ``.sha256`` (the sha256 of
    the archive itself);
  * a MISSING artifact is NOTED in the in-archive ``MANIFEST.txt`` and the bundle
    STILL SUCCEEDS (bounded + fail-soft per-artifact -- never aborts);
  * the archive carries the AST-diff section (chaos manifest + git diff of the
    target) and the PROVIDER-ROUTING telemetry section (DW-primary audit incl.
    the resolved JARVIS_DW_PRIMARY_OVERRIDE / JARVIS_PROVIDER_CLAUDE_DISABLED);
  * the printed stdout carries the archive path + the sha256 (the orchestrator
    reads them).
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import tarfile
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "a1_black_box.py"
_spec = importlib.util.spec_from_file_location("a1_black_box", _SCRIPT)
assert _spec and _spec.loader
black_box = importlib.util.module_from_spec(_spec)
sys.modules["a1_black_box"] = black_box
_spec.loader.exec_module(black_box)


# ===========================================================================
# Fixtures: a synthetic session/ledger tree the bundler walks.
# ===========================================================================


def _make_session_tree(root: Path, *, with_bus=True, with_dag=True):
    """Build a realistic .ouroboros + .jarvis tree under *root*."""
    # Ouroboros global context.
    sess = root / ".ouroboros" / "sessions" / "bt-20260624-000000"
    sess.mkdir(parents=True)
    (sess / "debug.log").write_text(
        "[A1Trace] emit goal=GOAL-1 source=roadmap\n"
        "[provider] generation served_by=openai/gpt-oss-120b route=STANDARD\n"
        "[DWSurface] surface=DIRECT_STREAMING healthy=true\n"
    )
    (sess / "summary.json").write_text(json.dumps({"session_outcome": "complete"}))
    # .jarvis ledgers.
    jarvis = root / ".jarvis"
    jarvis.mkdir(parents=True)
    (jarvis / "intake_dlq.jsonl").write_text('{"op": "GOAL-1"}\n')
    (jarvis / "graduation_ledger.jsonl").write_text('{"flag": "X"}\n')
    (jarvis / "decision_trace.jsonl").write_text('{"d": 1}\n')
    (jarvis / "op_ledger.jsonl").write_text('{"op": "GOAL-1", "phase": "APPLY"}\n')
    (jarvis / "dw_surface_health.json").write_text('{"transport_degraded": false}')
    (jarvis / "provider_quarantine.json").write_text('{"global_outage": false}')
    (jarvis / "chaos_manifest.json").write_text(json.dumps({
        "active": True,
        "target_file": "backend/foo.py",
        "function": "compute",
        "test_node": "tests/test_foo.py::test_compute",
    }))
    if with_bus:
        bus = jarvis / "agent_message_bus"
        bus.mkdir(parents=True)
        (bus / "graph-001.json").write_text('[{"msg": "hello"}]')
    if with_dag:
        dag = jarvis / "execution_graph_store"
        dag.mkdir(parents=True)
        (dag / "graph-001.json").write_text('{"nodes": [], "edges": []}')
    return sess


def _make_args(root: Path, out: Path, *, run_id="run-A1", git_diff="diff --git a/backend/foo.py..."):
    return black_box.BundleConfig(
        run_id=run_id,
        repo_root=str(root),
        out_dir=str(out),
        session_dir=str(root / ".ouroboros" / "sessions" / "bt-20260624-000000"),
        git_diff_runner=lambda target: git_diff,
        env={
            "JARVIS_DW_PRIMARY_OVERRIDE": "openai/gpt-oss-120b",
            "JARVIS_PROVIDER_CLAUDE_DISABLED": "true",
        },
    )


# ===========================================================================
# 1. The bundler collects present artifacts into a tar.gz + correct sha256.
# ===========================================================================


def test_bundle_creates_archive_and_correct_sha256(tmp_path):
    _make_session_tree(tmp_path)
    out = tmp_path / "out"
    cfg = _make_args(tmp_path, out)
    result = black_box.bundle(cfg)

    archive = Path(result.archive_path)
    sha_path = Path(result.sha256_path)
    assert archive.exists(), "the tar.gz must be written"
    assert sha_path.exists(), "the .sha256 sidecar must be written"
    assert archive.name == "black_box_run-A1.tar.gz"
    assert sha_path.name == "black_box_run-A1.tar.gz.sha256"

    # The emitted sha256 is the sha256 of the archive itself.
    actual = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert result.sha256 == actual
    # The sidecar file content is the same hex digest.
    assert actual in sha_path.read_text()


def test_bundle_captures_ouroboros_and_ledgers(tmp_path):
    _make_session_tree(tmp_path)
    out = tmp_path / "out"
    result = black_box.bundle(_make_args(tmp_path, out))
    with tarfile.open(result.archive_path, "r:gz") as tf:
        names = tf.getnames()
    blob = "\n".join(names)
    # Ouroboros global context.
    assert any("debug.log" in n for n in names)
    assert any("summary.json" in n for n in names)
    # .jarvis ledgers (intake, graduation, decision-trace, op-ledger).
    assert "intake_dlq.jsonl" in blob
    assert "graduation_ledger.jsonl" in blob
    assert "decision_trace.jsonl" in blob
    assert "op_ledger.jsonl" in blob


def test_bundle_captures_bus_and_dag(tmp_path):
    _make_session_tree(tmp_path)
    out = tmp_path / "out"
    result = black_box.bundle(_make_args(tmp_path, out))
    with tarfile.open(result.archive_path, "r:gz") as tf:
        names = tf.getnames()
    blob = "\n".join(names)
    # AgentMessageBus transcripts + DAG topology.
    assert "agent_message_bus" in blob, "swarm bus transcripts captured"
    assert "execution_graph_store" in blob, "DAG topology captured"


# ===========================================================================
# 2. The archive carries the AST-diff + provider-telemetry sections.
# ===========================================================================


def _extract_text(archive_path, member_suffix):
    with tarfile.open(archive_path, "r:gz") as tf:
        for m in tf.getmembers():
            if m.name.endswith(member_suffix):
                f = tf.extractfile(m)
                return f.read().decode("utf-8") if f else ""
    return None


def test_archive_contains_ast_diff_section(tmp_path):
    _make_session_tree(tmp_path)
    out = tmp_path / "out"
    cfg = _make_args(tmp_path, out, git_diff="diff --git a/backend/foo.py b/backend/foo.py\n-good\n+bug")
    result = black_box.bundle(cfg)
    # The AST diff section: chaos manifest + git diff of the target file.
    ast_diff = _extract_text(result.archive_path, "ast_diff.txt")
    assert ast_diff is not None, "ast_diff.txt must be in the archive"
    assert "backend/foo.py" in ast_diff
    assert "diff --git" in ast_diff
    assert "+bug" in ast_diff
    # The chaos manifest is captured alongside.
    man = _extract_text(result.archive_path, "chaos_manifest.json")
    assert man is not None and "compute" in man


def test_archive_contains_provider_routing_telemetry(tmp_path):
    _make_session_tree(tmp_path)
    out = tmp_path / "out"
    result = black_box.bundle(_make_args(tmp_path, out))
    prov = _extract_text(result.archive_path, "provider_telemetry.txt")
    assert prov is not None, "provider_telemetry.txt must be in the archive"
    # The resolved DW-primary + Claude-disabled values are recorded.
    assert "JARVIS_DW_PRIMARY_OVERRIDE" in prov
    assert "openai/gpt-oss-120b" in prov
    assert "JARVIS_PROVIDER_CLAUDE_DISABLED" in prov
    assert "true" in prov
    # The provider lines grepped from the debug.log are surfaced.
    assert "served_by" in prov or "provider" in prov


# ===========================================================================
# 3. A missing artifact -> noted in MANIFEST.txt, bundle STILL succeeds.
# ===========================================================================


def test_missing_artifact_noted_but_bundle_succeeds(tmp_path):
    # Build a tree WITHOUT the bus + WITHOUT the DAG store -> both absent.
    _make_session_tree(tmp_path, with_bus=False, with_dag=False)
    out = tmp_path / "out"
    result = black_box.bundle(_make_args(tmp_path, out))
    assert Path(result.archive_path).exists(), "bundle must still succeed"
    manifest = _extract_text(result.archive_path, "MANIFEST.txt")
    assert manifest is not None
    # The absent artifacts are NOTED (not silently dropped).
    assert "ABSENT" in manifest or "absent" in manifest
    # The bundle did not abort despite missing artifacts.
    assert result.captured_count >= 1
    assert result.absent_count >= 1


def test_missing_debug_log_does_not_abort(tmp_path):
    sess = _make_session_tree(tmp_path)
    (sess / "debug.log").unlink()  # remove the primary artifact
    out = tmp_path / "out"
    result = black_box.bundle(_make_args(tmp_path, out))
    assert Path(result.archive_path).exists()
    manifest = _extract_text(result.archive_path, "MANIFEST.txt")
    assert "debug.log" in manifest  # noted either captured or absent


def test_manifest_lists_captured_and_absent(tmp_path):
    _make_session_tree(tmp_path, with_bus=False)
    out = tmp_path / "out"
    result = black_box.bundle(_make_args(tmp_path, out))
    manifest = _extract_text(result.archive_path, "MANIFEST.txt")
    assert "CAPTURED" in manifest or "captured" in manifest
    assert "run-A1" in manifest  # run-id stamped


# ===========================================================================
# 4. stdout carries the archive path + sha256 (the orchestrator reads it).
# ===========================================================================


def test_main_prints_archive_path_and_sha256(tmp_path, capsys, monkeypatch):
    _make_session_tree(tmp_path)
    out = tmp_path / "out"
    monkeypatch.setenv("JARVIS_DW_PRIMARY_OVERRIDE", "openai/gpt-oss-120b")
    monkeypatch.setenv("JARVIS_PROVIDER_CLAUDE_DISABLED", "true")
    # git diff would be a real subprocess; stub it so no real git runs.
    monkeypatch.setattr(black_box, "_git_diff_of_target", lambda repo_root, target: "diff --git stub")
    rc = black_box.main([
        "--bundle", "--run-id", "run-CLI",
        "--out", str(out), "--repo-root", str(tmp_path),
    ])
    assert rc == 0
    captured = capsys.readouterr().out
    # The orchestrator parses these two structured stdout markers.
    assert "BLACK_BOX_ARCHIVE=" in captured
    assert "BLACK_BOX_SHA256=" in captured
    # The printed archive path actually exists.
    line = [l for l in captured.splitlines() if l.startswith("BLACK_BOX_ARCHIVE=")][0]
    arch = line.split("=", 1)[1].strip()
    assert Path(arch).exists()
    # The printed sha256 matches the archive.
    sha_line = [l for l in captured.splitlines() if l.startswith("BLACK_BOX_SHA256=")][0]
    sha = sha_line.split("=", 1)[1].strip()
    assert sha == hashlib.sha256(Path(arch).read_bytes()).hexdigest()


def test_help_runs(capsys):
    with pytest.raises(SystemExit) as ei:
        black_box.main(["--help"])
    assert ei.value.code == 0
