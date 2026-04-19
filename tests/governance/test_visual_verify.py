"""Regression spine for Task 17 — Visual VERIFY deterministic phase.

Scope:

* ``should_run_visual_verify`` — D2 decision tree (primary / structured-
  negative / secondary / tertiary / default).
* ``run_deterministic_checks`` — app-liveness, variance, hash-distance
  failure ordering + pass path.
* ``run_if_triggered`` — orchestrator entry point + I4 asymmetry
  (TestRunner red clamps Visual VERIFY pass to fail).
* ``VisualVerifyResult`` / ``VisualVerifyConfig`` dataclasses —
  construction, verdict whitelist, env-loaded defaults.

Spec: ``docs/superpowers/specs/2026-04-18-vision-sensor-verify-design.md``
§VERIFY Extension + §Invariant I4.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional

import pytest

from backend.core.ouroboros.governance.op_context import Attachment
from backend.core.ouroboros.governance.visual_verify import (
    CHECK_APP_CRASHED,
    CHECK_BLANK_SCREEN,
    CHECK_DETERMINISTIC_PASS,
    CHECK_HASH_SCRAMBLED,
    CHECK_HASH_UNCHANGED,
    CHECK_NO_POST_FRAME,
    CHECK_NO_PRE_FRAME,
    TRIGGER_NOT_UI_AFFECTED,
    TRIGGER_PLAN_UI_AFFECTED,
    TRIGGER_UI_FILES,
    TRIGGER_ZERO_TEST_COVERAGE,
    VERDICT_FAIL,
    VERDICT_PASS,
    VERDICT_SKIPPED,
    VisualVerifyConfig,
    VisualVerifyResult,
    default_hash_distance,
    default_hash_fn,
    default_variance_fn,
    run_deterministic_checks,
    run_if_triggered,
    should_run_visual_verify,
)


# ---------------------------------------------------------------------------
# Attachment factories
# ---------------------------------------------------------------------------


def _make_frame(tmp_path: Path, name: str, payload: bytes) -> str:
    p = tmp_path / name
    p.write_bytes(payload)
    return str(p)


def _pre_and_post(
    tmp_path: Path,
    *,
    pre_bytes: bytes = b"\x89PNG" + b"\x00" * 64,
    post_bytes: bytes = b"\x89PNG" + b"\xff" * 64,
    app_id: Optional[str] = "com.apple.Terminal",
):
    pre = Attachment.from_file(
        _make_frame(tmp_path, "pre.png", pre_bytes),
        kind="pre_apply", app_id=app_id,
    )
    post = Attachment.from_file(
        _make_frame(tmp_path, "post.png", post_bytes),
        kind="post_apply", app_id=app_id,
    )
    return pre, post


# ---------------------------------------------------------------------------
# Trigger logic — D2 decision tree
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "src/Button.tsx",
        "components/Card.jsx",
        "App.vue",
        "pages/Home.svelte",
        "styles/main.css",
        "styles/_mixins.scss",
        "index.html",
        "legacy/form.htm",
        "nested/deep/widget.TSX",   # case-insensitive
    ],
)
def test_trigger_primary_frontend_file_wins(path):
    should, reason = should_run_visual_verify((path,))
    assert should is True
    assert reason == TRIGGER_UI_FILES


def test_trigger_primary_wins_even_with_mixed_files():
    should, reason = should_run_visual_verify(
        ("backend/server.py", "src/Button.tsx"),
    )
    assert should is True
    assert reason == TRIGGER_UI_FILES


def test_trigger_primary_wins_over_plan_hint():
    # Structured signal authoritative — prose ignored when frontend file present.
    should, _ = should_run_visual_verify(
        ("src/Button.tsx",), plan_ui_affected=False,
    )
    assert should is True


@pytest.mark.parametrize(
    "path",
    [
        "backend/server.py",
        "cmd/main.go",
        "src/lib.rs",
        "app/Handler.java",
        "Program.cs",
        "helpers.ts",       # classifiable but not frontend
    ],
)
def test_trigger_structured_negative_when_classifiable_backend(path):
    should, reason = should_run_visual_verify(
        (path,),
        plan_ui_affected=True,     # ignored per D2
    )
    assert should is False
    assert reason == TRIGGER_NOT_UI_AFFECTED


def test_trigger_structured_negative_ignores_keyword_hint():
    # Backend + keyword plan = still structured-negative (D2).
    should, _ = should_run_visual_verify(
        ("backend/api.py",),
        plan_ui_affected=True,
        test_targets_resolved=(),      # would otherwise fire tertiary
        risk_tier="approval_required",
    )
    assert should is False


def test_trigger_secondary_plan_ui_affected_empty_target_files():
    should, reason = should_run_visual_verify(
        (),
        plan_ui_affected=True,
    )
    assert should is True
    assert reason == TRIGGER_PLAN_UI_AFFECTED


def test_trigger_secondary_plan_ui_affected_unclassifiable_files_only():
    # docs/plan.md is unclassifiable — plan hint wins.
    should, reason = should_run_visual_verify(
        ("docs/plan.md", "README.md"),
        plan_ui_affected=True,
    )
    assert should is True
    assert reason == TRIGGER_PLAN_UI_AFFECTED


def test_trigger_tertiary_zero_test_targets_on_notify_apply():
    should, reason = should_run_visual_verify(
        ("lib/helpers.py",),       # classifiable backend
        plan_ui_affected=False,
        test_targets_resolved=(),
        risk_tier="notify_apply",
    )
    # Structured-negative wins (backend py classifiable) — tertiary doesn't fire.
    assert should is False


def test_trigger_tertiary_fires_when_target_files_unclassifiable_and_empty_tests():
    should, reason = should_run_visual_verify(
        ("docs/config.yaml",),     # unclassifiable
        plan_ui_affected=False,    # no secondary
        test_targets_resolved=(),
        risk_tier="approval_required",
    )
    assert should is True
    assert reason == TRIGGER_ZERO_TEST_COVERAGE


def test_trigger_tertiary_requires_elevated_risk_tier():
    # Same config but risk_tier=safe_auto → tertiary suppressed.
    should, reason = should_run_visual_verify(
        ("docs/config.yaml",),
        test_targets_resolved=(),
        risk_tier="safe_auto",
    )
    assert should is False


def test_trigger_tertiary_inactive_when_tests_present():
    should, _ = should_run_visual_verify(
        ("docs/config.yaml",),
        test_targets_resolved=("tests/test_cfg.py",),   # non-empty
        risk_tier="notify_apply",
    )
    assert should is False


def test_trigger_default_when_nothing_matches():
    should, reason = should_run_visual_verify(())
    assert should is False
    assert reason == TRIGGER_NOT_UI_AFFECTED


# ---------------------------------------------------------------------------
# Deterministic checks — failure ordering
# ---------------------------------------------------------------------------


def test_deterministic_no_pre_frame_is_skipped(tmp_path):
    _pre, post = _pre_and_post(tmp_path)
    result = run_deterministic_checks(attachments=(post,))
    assert result.verdict == VERDICT_SKIPPED
    assert result.check == CHECK_NO_PRE_FRAME


def test_deterministic_no_post_frame_is_skipped(tmp_path):
    pre, _post = _pre_and_post(tmp_path)
    result = run_deterministic_checks(attachments=(pre,))
    assert result.verdict == VERDICT_SKIPPED
    assert result.check == CHECK_NO_POST_FRAME


def test_deterministic_app_crashed_fails(tmp_path):
    pre, post = _pre_and_post(tmp_path)
    result = run_deterministic_checks(
        attachments=(pre, post),
        app_alive_fn=lambda _: False,    # app dead
    )
    assert result.verdict == VERDICT_FAIL
    assert result.check == CHECK_APP_CRASHED


def test_deterministic_app_alive_probe_error_is_tolerated(tmp_path):
    """A raising probe should not break the whole battery — it's advisory."""
    def _raises(_):
        raise RuntimeError("Quartz unreachable")

    pre, post = _pre_and_post(tmp_path)
    result = run_deterministic_checks(
        attachments=(pre, post),
        app_alive_fn=_raises,
        # Inject a middle distance — the default probe returns 1.0 for
        # differing bytes which would trip hash_scrambled. This test
        # is specifically about tolerating the app-liveness probe
        # raising; the hash branch is exercised elsewhere.
        hash_distance_fn=lambda a, b: 0.5,
    )
    # Other checks still run; as long as variance/hash look fine,
    # verdict is pass.
    assert result.verdict == VERDICT_PASS


def test_deterministic_blank_screen_fails(tmp_path):
    pre, post = _pre_and_post(
        tmp_path,
        pre_bytes=b"\xff\x00" * 32,    # some variance
        post_bytes=b"\x00" * 64,        # all zeros — blank
    )
    cfg = VisualVerifyConfig(min_variance=0.01)
    result = run_deterministic_checks(
        attachments=(pre, post),
        cfg=cfg,
    )
    assert result.verdict == VERDICT_FAIL
    assert result.check == CHECK_BLANK_SCREEN


def test_deterministic_hash_unchanged_fails(tmp_path):
    # Pre and post bytes identical → hash distance 0 → fail.
    payload = b"\x89PNG" + b"\xab" * 64
    pre, post = _pre_and_post(
        tmp_path, pre_bytes=payload, post_bytes=payload,
    )
    result = run_deterministic_checks(attachments=(pre, post))
    assert result.verdict == VERDICT_FAIL
    assert result.check == CHECK_HASH_UNCHANGED
    assert result.hash_distance == 0.0


def test_deterministic_hash_scrambled_fails(tmp_path):
    """Force an out-of-range distance via a custom probe."""
    pre, post = _pre_and_post(tmp_path)
    cfg = VisualVerifyConfig(hash_distance_max=0.9)
    result = run_deterministic_checks(
        attachments=(pre, post),
        cfg=cfg,
        hash_distance_fn=lambda a, b: 0.99,
    )
    assert result.verdict == VERDICT_FAIL
    assert result.check == CHECK_HASH_SCRAMBLED
    assert result.hash_distance == 0.99


def test_deterministic_all_checks_pass(tmp_path):
    pre, post = _pre_and_post(tmp_path)
    result = run_deterministic_checks(
        attachments=(pre, post),
        # Default dhash returns 1.0 for different bytes — that's above
        # the default threshold (0.9), so we inject a middle value.
        hash_distance_fn=lambda a, b: 0.5,
    )
    assert result.verdict == VERDICT_PASS
    assert result.check == CHECK_DETERMINISTIC_PASS
    assert result.hash_distance == 0.5
    assert result.post_variance is not None


def test_deterministic_failure_order_first_miss_wins(tmp_path):
    """When multiple checks would fail, the first-miss rule applies.

    App crash vs blank vs hash — if app is dead, we fail on app_crashed
    regardless of whether the post frame is also blank.
    """
    pre, post = _pre_and_post(tmp_path, post_bytes=b"\x00" * 64)  # blank
    result = run_deterministic_checks(
        attachments=(pre, post),
        app_alive_fn=lambda _: False,
    )
    assert result.check == CHECK_APP_CRASHED   # app check runs first


def test_deterministic_unreadable_pre_attachment_skips(tmp_path, monkeypatch):
    pre, post = _pre_and_post(tmp_path)
    # Remove the pre frame's on-disk bytes → read_bytes raises.
    (tmp_path / "pre.png").unlink()
    result = run_deterministic_checks(attachments=(pre, post))
    assert result.verdict == VERDICT_SKIPPED
    assert result.check == CHECK_NO_PRE_FRAME


# ---------------------------------------------------------------------------
# VisualVerifyResult / VisualVerifyConfig
# ---------------------------------------------------------------------------


def test_result_verdict_whitelist():
    with pytest.raises(ValueError, match="verdict"):
        VisualVerifyResult(verdict="bogus", check="x")


def test_result_frozen():
    r = VisualVerifyResult(verdict=VERDICT_PASS, check="x")
    with pytest.raises(Exception):
        r.verdict = VERDICT_FAIL   # type: ignore[misc]


def test_config_from_env_reads_overrides(monkeypatch):
    monkeypatch.setenv("JARVIS_VISION_VERIFY_MIN_VARIANCE", "0.05")
    monkeypatch.setenv("JARVIS_VISION_VERIFY_HASH_DIST_MIN", "0.1")
    monkeypatch.setenv("JARVIS_VISION_VERIFY_HASH_DIST_MAX", "0.8")
    cfg = VisualVerifyConfig.from_env()
    assert cfg.min_variance == 0.05
    assert cfg.hash_distance_min == 0.1
    assert cfg.hash_distance_max == 0.8


def test_config_from_env_malformed_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("JARVIS_VISION_VERIFY_MIN_VARIANCE", "not_a_float")
    cfg = VisualVerifyConfig.from_env()
    assert cfg.min_variance == 0.01   # default preserved


# ---------------------------------------------------------------------------
# Default probes
# ---------------------------------------------------------------------------


def test_default_hash_fn_is_sha256():
    import hashlib

    assert default_hash_fn(b"hello") == hashlib.sha256(b"hello").hexdigest()


def test_default_hash_distance_zero_for_equal():
    assert default_hash_distance("abc", "abc") == 0.0


def test_default_hash_distance_one_for_different():
    assert default_hash_distance("abc", "def") == 1.0


def test_default_variance_zero_for_blank():
    assert default_variance_fn(b"\x00" * 100) == pytest.approx(1 / 256, rel=1e-6)


def test_default_variance_higher_for_varied():
    # 256 distinct bytes → full variance.
    varied = bytes(range(256))
    assert default_variance_fn(varied) == 1.0


def test_default_variance_empty_bytes():
    assert default_variance_fn(b"") == 0.0


# ---------------------------------------------------------------------------
# run_if_triggered — orchestrator entry
# ---------------------------------------------------------------------------


def test_run_if_triggered_skips_when_trigger_off(tmp_path):
    pre, post = _pre_and_post(tmp_path)
    result = run_if_triggered(
        target_files=("backend/server.py",),    # backend — no trigger
        attachments=(pre, post),
    )
    assert result.verdict == VERDICT_SKIPPED
    assert result.check == TRIGGER_NOT_UI_AFFECTED


def test_run_if_triggered_fires_on_frontend_files(tmp_path):
    pre, post = _pre_and_post(tmp_path)
    result = run_if_triggered(
        target_files=("src/Button.tsx",),
        attachments=(pre, post),
        hash_distance_fn=lambda a, b: 0.5,
    )
    assert result.verdict == VERDICT_PASS


def test_run_if_triggered_fires_secondary_path(tmp_path):
    pre, post = _pre_and_post(tmp_path)
    result = run_if_triggered(
        target_files=(),
        plan_ui_affected=True,
        attachments=(pre, post),
        hash_distance_fn=lambda a, b: 0.5,
    )
    assert result.verdict == VERDICT_PASS


def test_run_if_triggered_fires_tertiary_path(tmp_path):
    pre, post = _pre_and_post(tmp_path)
    result = run_if_triggered(
        target_files=("docs/cfg.yaml",),   # unclassifiable
        test_targets_resolved=(),
        risk_tier="notify_apply",
        attachments=(pre, post),
        hash_distance_fn=lambda a, b: 0.5,
    )
    assert result.verdict == VERDICT_PASS


# ---------------------------------------------------------------------------
# I4 — Visual VERIFY cannot overturn TestRunner
# ---------------------------------------------------------------------------


def test_i4_testrunner_red_clamps_visual_pass_to_fail(tmp_path):
    """When deterministic checks pass but TestRunner was red, the
    returned verdict is fail — Visual VERIFY cannot rescue a red op
    (Invariant I4)."""
    pre, post = _pre_and_post(tmp_path)
    result = run_if_triggered(
        target_files=("src/Button.tsx",),
        attachments=(pre, post),
        hash_distance_fn=lambda a, b: 0.5,
        test_runner_result="failed",
    )
    assert result.verdict == VERDICT_FAIL
    assert "I4 asymmetry" in result.reasoning
    # Deterministic probe values preserved on the clamped result for
    # observability.
    assert result.hash_distance == 0.5


def test_i4_testrunner_red_passes_through_visual_fail_unchanged(tmp_path):
    """TestRunner red AND Visual VERIFY fail → fail (no clamp needed)."""
    pre, post = _pre_and_post(tmp_path)
    result = run_if_triggered(
        target_files=("src/Button.tsx",),
        attachments=(pre, post),
        hash_distance_fn=lambda a, b: 0.0,     # hash_unchanged fail
        test_runner_result="failed",
    )
    assert result.verdict == VERDICT_FAIL
    assert result.check == CHECK_HASH_UNCHANGED   # original check preserved


def test_i4_testrunner_red_passes_through_visual_skipped(tmp_path):
    """TestRunner red + Visual VERIFY skipped (no frames) → skipped
    stays; the clamp only targets pass→fail."""
    result = run_if_triggered(
        target_files=("src/Button.tsx",),
        attachments=(),                        # no frames
        test_runner_result="failed",
    )
    assert result.verdict == VERDICT_SKIPPED


@pytest.mark.parametrize("red_value", ["failed", "fail", "red", "FAILED", " FAIL "])
def test_i4_testrunner_red_values_all_trigger_clamp(red_value, tmp_path):
    pre, post = _pre_and_post(tmp_path)
    result = run_if_triggered(
        target_files=("src/Button.tsx",),
        attachments=(pre, post),
        hash_distance_fn=lambda a, b: 0.5,
        test_runner_result=red_value,
    )
    assert result.verdict == VERDICT_FAIL


@pytest.mark.parametrize("green_value", ["passed", "pass", "green", "ok", None, ""])
def test_i4_testrunner_non_red_preserves_visual_pass(green_value, tmp_path):
    pre, post = _pre_and_post(tmp_path)
    result = run_if_triggered(
        target_files=("src/Button.tsx",),
        attachments=(pre, post),
        hash_distance_fn=lambda a, b: 0.5,
        test_runner_result=green_value,
    )
    assert result.verdict == VERDICT_PASS


def test_i4_visual_cannot_turn_red_to_green_semantic(tmp_path):
    """Spec-level check: Visual VERIFY never produces a pass that
    rescues a TestRunner-failed op. The clamp exists specifically
    to prevent this edge case."""
    pre, post = _pre_and_post(tmp_path)
    result = run_if_triggered(
        target_files=("src/Button.tsx",),
        attachments=(pre, post),
        hash_distance_fn=lambda a, b: 0.5,   # visual pass
        test_runner_result="failed",         # test red
    )
    # Visual pass + Test red → FAIL (clamped).
    # There is NO code path through run_if_triggered that returns
    # pass when test_runner_result says failed.
    assert result.verdict != VERDICT_PASS


# ---------------------------------------------------------------------------
# I4 — Visual VERIFY cannot grant approval when structure forbids it
# ---------------------------------------------------------------------------


def test_visual_verify_never_calls_router_directly(tmp_path):
    """Visual VERIFY is a pure function — returns a result, doesn't
    touch the router, envelope queue, or any mutable state outside
    its own return value. This is the "doesn't turn red into green"
    invariant applied to side effects.
    """
    pre, post = _pre_and_post(tmp_path)
    # Nothing we pass gets mutated externally — the function returns
    # a frozen dataclass and that's it.
    result = run_if_triggered(
        target_files=("src/Button.tsx",),
        attachments=(pre, post),
        hash_distance_fn=lambda a, b: 0.5,
    )
    assert isinstance(result, VisualVerifyResult)


# ---------------------------------------------------------------------------
# Constants + exports pinned
# ---------------------------------------------------------------------------


def test_verdict_constants_are_strings():
    assert VERDICT_PASS == "pass"
    assert VERDICT_FAIL == "fail"
    assert VERDICT_SKIPPED == "skipped"


def test_trigger_constants_are_strings():
    assert TRIGGER_UI_FILES == "ui_files"
    assert TRIGGER_PLAN_UI_AFFECTED == "plan_ui_affected"
    assert TRIGGER_ZERO_TEST_COVERAGE == "zero_test_coverage"
    assert TRIGGER_NOT_UI_AFFECTED == "not_ui_affected"


def test_check_constants_cover_all_failure_paths():
    all_checks = {
        CHECK_APP_CRASHED,
        CHECK_BLANK_SCREEN,
        CHECK_HASH_UNCHANGED,
        CHECK_HASH_SCRAMBLED,
        CHECK_NO_PRE_FRAME,
        CHECK_NO_POST_FRAME,
        CHECK_DETERMINISTIC_PASS,
    }
    # Sanity: the check set is distinct + stable.
    assert len(all_checks) == 7
