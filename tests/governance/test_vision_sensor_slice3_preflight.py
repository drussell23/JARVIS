"""Slice 3 pre-flight dry-run — Task 18.

The real Slice 3 graduation arc (3 consecutive clean sessions with
deterministic Visual VERIFY active on UI-affected ops) requires real
UI changes + real human judgement ("did the phase catch a regression
TestRunner missed?"). See operator checklist at
``docs/operations/vision-sensor-slice-3-graduation.md``.

What CAN be exercised autonomously is the **Visual VERIFY integration
smoke test**: the full Task 17 module exercised through synthetic
Attachment pairs as the orchestrator would produce, plus the master-
switch env defaults remaining OFF until Step 3.

Scenarios:

1. UI op (frontend target_files) + healthy pre/post → deterministic
   pass.
2. UI op + blank post frame → deterministic fail, ``blank_screen``.
3. UI op + identical pre/post → deterministic fail, ``hash_unchanged``.
4. UI op + app crashed probe → deterministic fail, ``app_crashed``.
5. UI op + healthy frames + TestRunner red → **clamped to fail** per
   I4 asymmetry (Visual VERIFY cannot overturn TestRunner red).
6. Backend op (no UI files) → skipped with reason ``not_ui_affected``.
7. Tertiary trigger: unclassifiable files + zero tests +
   approval_required → Visual VERIFY runs.
8. Missing pre or post Attachment → skipped gracefully, no crash.
9. Env master switch: ``JARVIS_VISION_VERIFY_ENABLED`` default
   ``false`` in source; ``MODEL_ASSISTED_ENABLED`` default
   ``false``.
"""
from __future__ import annotations

import pathlib

import pytest

from backend.core.ouroboros.governance.op_context import Attachment
from backend.core.ouroboros.governance.visual_verify import (
    CHECK_APP_CRASHED,
    CHECK_BLANK_SCREEN,
    CHECK_DETERMINISTIC_PASS,
    CHECK_HASH_UNCHANGED,
    CHECK_NO_POST_FRAME,
    CHECK_NO_PRE_FRAME,
    TRIGGER_NOT_UI_AFFECTED,
    TRIGGER_ZERO_TEST_COVERAGE,
    VERDICT_FAIL,
    VERDICT_PASS,
    VERDICT_SKIPPED,
    run_if_triggered,
    visual_verify_enabled,
    visual_verify_model_assisted_enabled,
)


# ---------------------------------------------------------------------------
# Autouse isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _slice3_env(monkeypatch, tmp_path):
    """Pin Slice 3 operator env config exactly.

    Important: we do NOT set ``JARVIS_VISION_VERIFY_ENABLED`` here —
    Slice 3 entry default is ``false``. Tests that want it on set it
    explicitly (mimicking the operator opting in per-session).
    """
    monkeypatch.delenv("JARVIS_VISION_VERIFY_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_VISION_VERIFY_MODEL_ASSISTED_ENABLED", raising=False)
    monkeypatch.chdir(tmp_path)
    yield


# ---------------------------------------------------------------------------
# Attachment builder
# ---------------------------------------------------------------------------


def _pre_post(tmp_path, *, pre_bytes, post_bytes, app_id="com.apple.Terminal"):
    pre_path = tmp_path / "pre.png"
    pre_path.write_bytes(pre_bytes)
    post_path = tmp_path / "post.png"
    post_path.write_bytes(post_bytes)
    pre = Attachment.from_file(str(pre_path), kind="pre_apply", app_id=app_id)
    post = Attachment.from_file(str(post_path), kind="post_apply", app_id=app_id)
    return pre, post


# ---------------------------------------------------------------------------
# Scenario 1 — UI op + healthy frames → deterministic pass
# ---------------------------------------------------------------------------


def test_slice3_preflight_ui_op_deterministic_pass(tmp_path):
    pre, post = _pre_post(
        tmp_path,
        pre_bytes=b"\x89PNG" + b"\x00" * 64,
        post_bytes=b"\x89PNG" + b"\xff" * 64,
    )
    result = run_if_triggered(
        target_files=("src/components/Button.tsx",),
        attachments=(pre, post),
        hash_distance_fn=lambda a, b: 0.5,   # inside [min=0, max=0.9]
    )
    assert result.verdict == VERDICT_PASS
    assert result.check == CHECK_DETERMINISTIC_PASS


# ---------------------------------------------------------------------------
# Scenario 2 — blank post frame
# ---------------------------------------------------------------------------


def test_slice3_preflight_blank_post_fails(tmp_path):
    pre, post = _pre_post(
        tmp_path,
        pre_bytes=b"\x89PNG" + b"\xab" * 64,   # varied
        post_bytes=b"\x00" * 64,               # blank
    )
    result = run_if_triggered(
        target_files=("app/App.vue",),
        attachments=(pre, post),
    )
    assert result.verdict == VERDICT_FAIL
    assert result.check == CHECK_BLANK_SCREEN


# ---------------------------------------------------------------------------
# Scenario 3 — identical pre/post
# ---------------------------------------------------------------------------


def test_slice3_preflight_hash_unchanged_fails(tmp_path):
    payload = b"\x89PNG" + b"\xbe" * 64
    pre, post = _pre_post(tmp_path, pre_bytes=payload, post_bytes=payload)
    result = run_if_triggered(
        target_files=("styles/main.css",),
        attachments=(pre, post),
    )
    assert result.verdict == VERDICT_FAIL
    assert result.check == CHECK_HASH_UNCHANGED


# ---------------------------------------------------------------------------
# Scenario 4 — app crashed
# ---------------------------------------------------------------------------


def test_slice3_preflight_app_crashed_fails(tmp_path):
    pre, post = _pre_post(
        tmp_path,
        pre_bytes=b"\x89PNG" + b"\x00" * 64,
        post_bytes=b"\x89PNG" + b"\xff" * 64,
    )
    result = run_if_triggered(
        target_files=("pages/Home.svelte",),
        attachments=(pre, post),
        app_alive_fn=lambda _: False,
    )
    assert result.verdict == VERDICT_FAIL
    assert result.check == CHECK_APP_CRASHED


# ---------------------------------------------------------------------------
# Scenario 5 — I4 asymmetry (TestRunner red clamps visual pass)
# ---------------------------------------------------------------------------


def test_slice3_preflight_i4_clamps_pass_to_fail_on_test_red(tmp_path):
    pre, post = _pre_post(
        tmp_path,
        pre_bytes=b"\x89PNG" + b"\x00" * 64,
        post_bytes=b"\x89PNG" + b"\xff" * 64,
    )
    result = run_if_triggered(
        target_files=("components/Card.jsx",),
        attachments=(pre, post),
        hash_distance_fn=lambda a, b: 0.5,
        test_runner_result="failed",
    )
    assert result.verdict == VERDICT_FAIL
    assert "I4 asymmetry" in result.reasoning


# ---------------------------------------------------------------------------
# Scenario 6 — Backend op skips
# ---------------------------------------------------------------------------


def test_slice3_preflight_backend_op_skipped(tmp_path):
    pre, post = _pre_post(
        tmp_path,
        pre_bytes=b"\x89PNG" + b"\x00" * 64,
        post_bytes=b"\x89PNG" + b"\xff" * 64,
    )
    result = run_if_triggered(
        target_files=("backend/server.py",),
        attachments=(pre, post),
    )
    assert result.verdict == VERDICT_SKIPPED
    assert result.check == TRIGGER_NOT_UI_AFFECTED


# ---------------------------------------------------------------------------
# Scenario 7 — tertiary trigger
# ---------------------------------------------------------------------------


def test_slice3_preflight_tertiary_trigger_fires(tmp_path):
    pre, post = _pre_post(
        tmp_path,
        pre_bytes=b"\x89PNG" + b"\x00" * 64,
        post_bytes=b"\x89PNG" + b"\xff" * 64,
    )
    result = run_if_triggered(
        target_files=("docs/config.yaml",),    # unclassifiable
        attachments=(pre, post),
        test_targets_resolved=(),              # zero tests
        risk_tier="approval_required",
        hash_distance_fn=lambda a, b: 0.5,
    )
    assert result.verdict == VERDICT_PASS


# ---------------------------------------------------------------------------
# Scenario 8 — missing frames → skipped
# ---------------------------------------------------------------------------


def test_slice3_preflight_missing_post_frame_skips_gracefully(tmp_path):
    pre, _post = _pre_post(
        tmp_path,
        pre_bytes=b"\x89PNG" + b"\x00" * 64,
        post_bytes=b"\x89PNG" + b"\xff" * 64,
    )
    result = run_if_triggered(
        target_files=("src/App.tsx",),
        attachments=(pre,),   # only pre
    )
    assert result.verdict == VERDICT_SKIPPED
    assert result.check == CHECK_NO_POST_FRAME


def test_slice3_preflight_no_attachments_skips_gracefully():
    result = run_if_triggered(
        target_files=("src/App.tsx",),
        attachments=(),
    )
    assert result.verdict == VERDICT_SKIPPED
    assert result.check == CHECK_NO_PRE_FRAME


# ---------------------------------------------------------------------------
# Env master switches — Slice 3 entry defaults OFF
# ---------------------------------------------------------------------------


def test_slice3_master_switch_default_off_at_runtime():
    # No env set → helper returns False.
    assert visual_verify_enabled() is False


def test_slice3_model_assisted_default_off_at_runtime():
    assert visual_verify_model_assisted_enabled() is False


def test_slice3_master_switch_respects_truthy_env(monkeypatch):
    monkeypatch.setenv("JARVIS_VISION_VERIFY_ENABLED", "true")
    assert visual_verify_enabled() is True


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on", "On"])
def test_slice3_master_switch_truthy_values(monkeypatch, val):
    monkeypatch.setenv("JARVIS_VISION_VERIFY_ENABLED", val)
    assert visual_verify_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "", "False"])
def test_slice3_master_switch_falsy_values(monkeypatch, val):
    monkeypatch.setenv("JARVIS_VISION_VERIFY_ENABLED", val)
    assert visual_verify_enabled() is False


# ---------------------------------------------------------------------------
# Source-level guard: master switches still default off in production code
# ---------------------------------------------------------------------------


def test_slice3_master_switch_default_off_in_source():
    """Safety guard: even after Task 17 shipped, the master switch
    must default to ``false`` in production code until Task 18 Step 3
    completes.
    """
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    src = (
        repo_root
        / "backend/core/ouroboros/governance/visual_verify.py"
    ).read_text(encoding="utf-8")
    assert 'JARVIS_VISION_VERIFY_ENABLED", "false"' in src, (
        "Slice 3 hasn't graduated — visual_verify_enabled default "
        "must remain 'false' until the 3-session arc passes."
    )
    assert 'JARVIS_VISION_VERIFY_MODEL_ASSISTED_ENABLED", "false"' in src, (
        "Slice 4 hasn't graduated — model_assisted default must "
        "remain 'false'."
    )


def test_slice3_earlier_slice_switches_unaffected_by_slice3_graduation():
    """The Slice 1/2 defaults (Sensor ENABLED, TIER2_ENABLED, CHAIN_MAX)
    are unrelated to Slice 3's flip; we guard them here too so a Task
    18 Step 3 commit can't accidentally touch them.
    """
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    sensor_src = (
        repo_root
        / "backend/core/ouroboros/governance/intake/sensors/vision_sensor.py"
    ).read_text(encoding="utf-8")
    # Slice 1 master switch remains operator-opt-in in source until
    # its own graduation (Task 14 Step 5). Slice 2 defaults were
    # guarded in the Slice 2 pre-flight — this test doesn't duplicate
    # but confirms the Slice 3 flip is scoped narrowly to visual_verify.py.
    assert 'JARVIS_VISION_SENSOR_TIER2_ENABLED", "false"' in sensor_src
    assert 'JARVIS_VISION_CHAIN_MAX", "1"' in sensor_src
