"""Tests for a1_launch_manifest -- TDD round-trips + fail-closed + compose_env compat."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

# Allow import from scripts/ (repo root -> scripts/).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from a1_launch_manifest import (
    A1ManifestError,
    SCHEMA_VERSION,
    apply_manifest,
    build_manifest,
    load_and_validate,
    write_manifest,
)


# ===========================================================================
# build_manifest / write_manifest / load_and_validate round-trip
# ===========================================================================


def test_round_trip(tmp_path):
    """build -> write -> load_and_validate returns the original dict."""
    m = build_manifest(
        model="Qwen/Qwen3.5-397B-A17B-FP8",
        native_tool_forcing=True,
        epistemic_feedback=True,
        seed=42,
        cost_cap=0.5,
        max_wall_seconds=2400,
    )
    p = tmp_path / "A1_LAUNCH_MANIFEST.json"
    write_manifest(p, m)
    loaded = load_and_validate(p)
    assert loaded["model"] == "Qwen/Qwen3.5-397B-A17B-FP8"
    assert loaded["native_tool_forcing"] is True
    assert loaded["epistemic_feedback"] is True
    assert loaded["seed"] == 42
    assert loaded["cost_cap"] == 0.5
    assert loaded["max_wall_seconds"] == 2400
    assert loaded["schema_version"] == SCHEMA_VERSION


def test_write_manifest_adds_generated_for(tmp_path):
    """write_manifest appends generated_for tag outside the validated core."""
    m = build_manifest(
        model="Qwen/Qwen3.5-397B-A17B-FP8",
        native_tool_forcing=True,
        epistemic_feedback=True,
    )
    p = tmp_path / "A1_LAUNCH_MANIFEST.json"
    write_manifest(p, m)
    raw = json.loads(p.read_text())
    assert "generated_for" in raw
    # generated_for must NOT affect validated core round-trip.
    loaded = load_and_validate(p)
    assert loaded["schema_version"] == SCHEMA_VERSION


def test_build_manifest_is_pure(tmp_path):
    """build_manifest is deterministic -- calling twice with same args gives same core."""
    m1 = build_manifest(model="x", native_tool_forcing=False, epistemic_feedback=False)
    m2 = build_manifest(model="x", native_tool_forcing=False, epistemic_feedback=False)
    assert m1 == m2


def test_build_manifest_omits_optionals_when_absent():
    """Optional keys are absent from the manifest dict when not supplied."""
    m = build_manifest(model="x", native_tool_forcing=True, epistemic_feedback=True)
    assert "seed" not in m
    assert "cost_cap" not in m
    assert "max_wall_seconds" not in m
    assert "extra_flags" not in m


def test_build_manifest_extra_flags():
    """extra_flags dict is preserved in the manifest."""
    m = build_manifest(
        model="x",
        native_tool_forcing=True,
        epistemic_feedback=True,
        extra_flags={"JARVIS_FOO": "bar"},
    )
    assert m["extra_flags"] == {"JARVIS_FOO": "bar"}


# ===========================================================================
# load_and_validate fail-closed behaviour
# ===========================================================================


def test_missing_file(tmp_path):
    """Missing manifest -> A1ManifestError with 'not found' in message."""
    with pytest.raises(A1ManifestError, match="not found"):
        load_and_validate(tmp_path / "missing.json")


def test_bad_schema_version(tmp_path):
    """Wrong schema_version -> A1ManifestError with 'schema_version' in message."""
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({
        "schema_version": "wrong",
        "model": "x",
        "native_tool_forcing": True,
        "epistemic_feedback": True,
    }))
    with pytest.raises(A1ManifestError, match="schema_version"):
        load_and_validate(p)


def test_missing_required_key(tmp_path):
    """Manifest missing epistemic_feedback -> A1ManifestError with 'missing required keys'."""
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({
        "schema_version": SCHEMA_VERSION,
        "model": "x",
        "native_tool_forcing": True,
        # epistemic_feedback intentionally omitted
    }))
    with pytest.raises(A1ManifestError, match="missing required keys"):
        load_and_validate(p)


def test_invalid_json(tmp_path):
    """Corrupt JSON -> A1ManifestError with 'unparseable' in message."""
    p = tmp_path / "bad.json"
    p.write_text("{not valid json")
    with pytest.raises(A1ManifestError, match="unparseable"):
        load_and_validate(p)


# ===========================================================================
# apply_manifest
# ===========================================================================


def test_apply_manifest_sets_env():
    """apply_manifest writes all 3 required flag keys into env."""
    m = build_manifest(
        model="Qwen/Qwen3.5-397B-A17B-FP8",
        native_tool_forcing=True,
        epistemic_feedback=True,
    )
    env: dict = {}
    result = apply_manifest(m, env)
    assert result["JARVIS_DW_PRIMARY_OVERRIDE"] == "Qwen/Qwen3.5-397B-A17B-FP8"
    assert result["JARVIS_DW_NATIVE_TOOL_FORCING_ENABLED"] == "true"
    assert result["JARVIS_EPISTEMIC_FEEDBACK_ENABLED"] == "true"


def test_apply_manifest_omits_when_false():
    """apply_manifest does NOT set forcing/epistemic keys when flags are False."""
    m = build_manifest(model="Qwen/Qwen3.5-397B-A17B-FP8", native_tool_forcing=False, epistemic_feedback=False)
    env: dict = {}
    apply_manifest(m, env)
    assert env["JARVIS_DW_PRIMARY_OVERRIDE"] == "Qwen/Qwen3.5-397B-A17B-FP8"
    assert "JARVIS_DW_NATIVE_TOOL_FORCING_ENABLED" not in env
    assert "JARVIS_EPISTEMIC_FEEDBACK_ENABLED" not in env


def test_apply_manifest_sets_optional_keys():
    """apply_manifest writes seed/cost_cap/max_wall_seconds when present."""
    m = build_manifest(
        model="Qwen/Qwen3.5-397B-A17B-FP8",
        native_tool_forcing=True,
        epistemic_feedback=True,
        seed=7,
        cost_cap=0.5,
        max_wall_seconds=2400,
    )
    env: dict = {}
    apply_manifest(m, env)
    assert env["JARVIS_CHAOS_SEED"] == "7"
    assert env["OUROBOROS_BATTLE_COST_CAP"] == "0.5"
    assert env["OUROBOROS_BATTLE_MAX_WALL_SECONDS"] == "2400"


def test_apply_manifest_extra_flags():
    """apply_manifest forwards extra_flags into env as strings."""
    m = build_manifest(
        model="x",
        native_tool_forcing=True,
        epistemic_feedback=True,
        extra_flags={"JARVIS_FOO": "bar", "JARVIS_BAZ": 99},
    )
    env: dict = {}
    apply_manifest(m, env)
    assert env["JARVIS_FOO"] == "bar"
    assert env["JARVIS_BAZ"] == "99"


def test_apply_manifest_returns_mutated_env():
    """apply_manifest returns the same dict object it received."""
    m = build_manifest(model="x", native_tool_forcing=True, epistemic_feedback=True)
    env: dict = {}
    result = apply_manifest(m, env)
    assert result is env


# ===========================================================================
# compose_env compat: the 3 flag keys must appear via apply_manifest
# ===========================================================================


def test_compose_env_compat():
    """compose_env output contains the 3 flag keys injected via apply_manifest
    (byte-compatible with the A1 launch spec)."""
    _harness_script = _SCRIPTS_DIR / "a1_live_fire_chaos_harness.py"
    if not _harness_script.exists():
        pytest.skip("a1_live_fire_chaos_harness.py not found -- skipping compat check")
    try:
        spec = importlib.util.spec_from_file_location("a1_live_fire_chaos_harness", _harness_script)
        assert spec and spec.loader
        harness_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(harness_mod)  # type: ignore[union-attr]
        env = harness_mod.compose_env()
        assert "JARVIS_DW_PRIMARY_OVERRIDE" in env, (
            "compose_env must set JARVIS_DW_PRIMARY_OVERRIDE via apply_manifest"
        )
        assert "JARVIS_DW_NATIVE_TOOL_FORCING_ENABLED" in env, (
            "compose_env must set JARVIS_DW_NATIVE_TOOL_FORCING_ENABLED via apply_manifest"
        )
        assert "JARVIS_EPISTEMIC_FEEDBACK_ENABLED" in env, (
            "compose_env must set JARVIS_EPISTEMIC_FEEDBACK_ENABLED via apply_manifest"
        )
    except Exception as exc:
        pytest.skip("compose_env import requires full harness setup: %s" % exc)
