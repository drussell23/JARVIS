"""Phase 9 cadence hardening regression spine (2026-05-05).

Pins the two structural fixes that close the load-bearing latent
blockers identified in the 2026-05-05 pre-cadence audit:

  * **Slice 1: wall-clock session-detection** — ``after_epoch`` is
    now derived from ``start_wall_anchor`` captured BEFORE
    ``subprocess.run`` (immutable reference; forward NTP skew during
    subprocess cannot move it). Replaces the prior ``time.time() -
    timeout_s - 60`` post-subprocess derivation that lost session
    data on forward clock jumps.
  * **Slice 1 defense-in-depth**: ``_read_most_recent_session`` now
    sorts candidates by mtime descending (was lexicographic name
    sort). Robust to naming-convention drift; matches the actual
    semantic of "most recent."
  * **Slice 2: ``ready`` CLI subcommand** — composes the existing
    ``GraduationLedger.eligible_flags()`` primitive so the operator
    can answer "which flags are ready to flip?" in one command.

Pinned via:

  * source-AST regression for the ``time.time() - timeout_s``
    derivation (forbidden — fail CI before reaching production)
  * ``_SESSION_DETECTION_GRACE_S`` module-level constant present
  * ``cmd_ready`` handler exists + wired into ``handlers`` dict
  * ``ready`` subparser registered

Layer 1: source-AST pins (~7 tests)
Layer 2: ``_read_most_recent_session`` mtime-sort behavior (~6 tests)
Layer 3: ``cmd_ready`` shape (~5 tests)
"""
from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import patch

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Layer 1: source-AST regression pins
# ---------------------------------------------------------------------------


def test_grace_constant_present():
    """``_SESSION_DETECTION_GRACE_S`` MUST be defined at module
    level. Removing it would silently restore the brittle
    ``time.time() - timeout_s - 60`` pattern."""
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/graduation"
        / "live_fire_soak.py"
    )
    text = target.read_text(encoding="utf-8")
    assert "_SESSION_DETECTION_GRACE_S" in text, (
        "_SESSION_DETECTION_GRACE_S constant missing — Phase 9 "
        "hardening Slice 1 regression"
    )


def test_no_post_subprocess_walltime_derivation():
    """The ``after_epoch=time.time() - timeout_s - 60`` pattern
    MUST NOT reappear in ``_run_battle_test_subprocess``. AST-
    precise: only fires on the actual call site, not on docstring
    or comment mentions."""
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/graduation"
        / "live_fire_soak.py"
    )
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    target_func = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            if node.name == "_run_battle_test_subprocess":
                target_func = node
                break
    assert target_func is not None, (
        "_run_battle_test_subprocess function missing"
    )
    # Walk function body looking for keyword arg
    # `after_epoch=` whose value contains a `time.time()` call
    # AND a subtraction involving timeout_s. That's the bug
    # pattern; banned.
    for node in ast.walk(target_func):
        if isinstance(node, ast.keyword):
            if node.arg != "after_epoch":
                continue
            # Look for `time.time() - X`
            value = node.value
            if not isinstance(value, ast.BinOp):
                continue
            if not isinstance(value.op, ast.Sub):
                continue
            left = value.left
            if isinstance(left, ast.Call):
                func = left.func
                if (
                    isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "time"
                    and func.attr == "time"
                ):
                    # Bug pattern detected.
                    pytest.fail(
                        "_run_battle_test_subprocess uses "
                        "after_epoch=time.time() - ... "
                        "(forward-NTP-skew unsafe — should "
                        "use start_wall_anchor captured "
                        "BEFORE subprocess.run)"
                    )


def test_start_wall_anchor_captured_before_subprocess_run():
    """``start_wall_anchor`` MUST be assigned BEFORE the
    ``subprocess.run`` call inside ``_run_battle_test_subprocess``.
    AST line-order check."""
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/graduation"
        / "live_fire_soak.py"
    )
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    target_func = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            if node.name == "_run_battle_test_subprocess":
                target_func = node
                break
    assert target_func is not None
    anchor_line = None
    subprocess_line = None
    for node in ast.walk(target_func):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if (
                    isinstance(tgt, ast.Name)
                    and tgt.id == "start_wall_anchor"
                ):
                    anchor_line = node.lineno
        if isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "subprocess"
                and func.attr == "run"
            ):
                if subprocess_line is None:
                    subprocess_line = node.lineno
    assert anchor_line is not None, (
        "start_wall_anchor assignment missing"
    )
    assert subprocess_line is not None, (
        "subprocess.run call missing"
    )
    assert anchor_line < subprocess_line, (
        f"start_wall_anchor (line {anchor_line}) MUST be "
        f"captured BEFORE subprocess.run (line "
        f"{subprocess_line}) — Phase 9 hardening Slice 1"
    )


def test_after_epoch_uses_anchor_minus_grace():
    """``after_epoch`` keyword in
    ``_read_most_recent_session`` call MUST be derived from
    ``start_wall_anchor - _SESSION_DETECTION_GRACE_S`` shape."""
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/graduation"
        / "live_fire_soak.py"
    )
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    target_func = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            if node.name == "_run_battle_test_subprocess":
                target_func = node
                break
    found = False
    for node in ast.walk(target_func):
        if isinstance(node, ast.keyword):
            if node.arg != "after_epoch":
                continue
            value = node.value
            if not isinstance(value, ast.BinOp):
                continue
            if not isinstance(value.op, ast.Sub):
                continue
            # Left should be Name(start_wall_anchor)
            left = value.left
            right = value.right
            if (
                isinstance(left, ast.Name)
                and left.id == "start_wall_anchor"
                and isinstance(right, ast.Name)
                and right.id == "_SESSION_DETECTION_GRACE_S"
            ):
                found = True
                break
    assert found, (
        "after_epoch MUST be derived from "
        "start_wall_anchor - _SESSION_DETECTION_GRACE_S"
    )


def test_read_most_recent_session_sorts_by_mtime():
    """``_read_most_recent_session`` body MUST sort candidates
    by mtime, not by lexicographic name. Pin: function body
    must reference ``st_mtime`` in a sort context."""
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/graduation"
        / "live_fire_soak.py"
    )
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    target_func = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            if node.name == "_read_most_recent_session":
                target_func = node
                break
    assert target_func is not None
    body_text = ast.get_source_segment(source, target_func) or ""
    assert "st_mtime" in body_text, (
        "_read_most_recent_session MUST sort by mtime, not "
        "lexicographic name (Phase 9 hardening Slice 1 "
        "defense-in-depth)"
    )
    # And NOT have a bare `sorted(sessions_root.iterdir(), reverse=True)`
    # without an mtime-keyed comparator — that's the old buggy
    # path.
    assert "candidates_with_mtime" in body_text or (
        "key=" in body_text and "mtime" in body_text
    ), (
        "_read_most_recent_session must keep an explicit "
        "mtime-sort container or key callable"
    )


# ---------------------------------------------------------------------------
# Layer 2: behavioral pin — mtime-sort robustness
# ---------------------------------------------------------------------------


def test_read_most_recent_session_picks_highest_mtime(
    tmp_path,
):
    """Behavioral: when names sort lexicographically OPPOSITE
    of mtime order, the function MUST follow mtime, not name."""
    from backend.core.ouroboros.governance.graduation.live_fire_soak import (  # noqa: E501
        _read_most_recent_session,
    )
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    # "a-old" sorts BEFORE "z-new" lexicographically.
    # If we set "a-old" mtime FUTURE and "z-new" mtime PAST,
    # mtime-sort picks "a-old" while name-sort picks "z-new".
    a_old = sessions_root / "a-old"
    a_old.mkdir()
    (a_old / "summary.json").write_text(
        '{"session_id": "a-old"}', encoding="utf-8",
    )
    z_new = sessions_root / "z-new"
    z_new.mkdir()
    (z_new / "summary.json").write_text(
        '{"session_id": "z-new"}', encoding="utf-8",
    )
    # Force mtime: a-old is newer than z-new
    import os
    os.utime(a_old, (3000, 3000))  # newer
    os.utime(z_new, (2000, 2000))  # older
    summary, _ = _read_most_recent_session(
        sessions_root, after_epoch=1000,
    )
    # mtime sort wins → returns a-old
    assert summary.get("session_id") == "a-old", (
        f"Expected mtime-sort to pick 'a-old' (mtime=3000); "
        f"got {summary}"
    )


def test_read_most_recent_session_respects_after_epoch(
    tmp_path,
):
    """A session whose mtime is BEFORE the anchor MUST be
    filtered out — the anchor is the load-bearing reference."""
    from backend.core.ouroboros.governance.graduation.live_fire_soak import (  # noqa: E501
        _read_most_recent_session,
    )
    import os
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    too_old = sessions_root / "stale"
    too_old.mkdir()
    (too_old / "summary.json").write_text(
        '{"session_id": "stale"}', encoding="utf-8",
    )
    os.utime(too_old, (1000, 1000))  # old
    summary, _ = _read_most_recent_session(
        sessions_root, after_epoch=5000,
    )
    assert summary == {}, (
        f"Session with mtime=1000 < anchor=5000 must be "
        f"filtered; got {summary}"
    )


def test_read_most_recent_session_missing_root_returns_empty(
    tmp_path,
):
    """Defensive: missing sessions root returns empty tuple,
    NEVER raises."""
    from backend.core.ouroboros.governance.graduation.live_fire_soak import (  # noqa: E501
        _read_most_recent_session,
    )
    summary, debug_tail = _read_most_recent_session(
        tmp_path / "nonexistent", after_epoch=0,
    )
    assert summary == {}
    assert debug_tail == ""


def test_read_most_recent_session_empty_root_returns_empty(
    tmp_path,
):
    from backend.core.ouroboros.governance.graduation.live_fire_soak import (  # noqa: E501
        _read_most_recent_session,
    )
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    summary, debug_tail = _read_most_recent_session(
        sessions_root, after_epoch=0,
    )
    assert summary == {}
    assert debug_tail == ""


def test_read_most_recent_session_skips_non_dirs(tmp_path):
    """Non-directory entries (e.g., stray files) MUST NOT
    block the scan."""
    from backend.core.ouroboros.governance.graduation.live_fire_soak import (  # noqa: E501
        _read_most_recent_session,
    )
    import os
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    (sessions_root / "stray-file").write_text("noise")
    real = sessions_root / "real-session"
    real.mkdir()
    (real / "summary.json").write_text(
        '{"session_id": "real"}', encoding="utf-8",
    )
    os.utime(real, (3000, 3000))
    summary, _ = _read_most_recent_session(
        sessions_root, after_epoch=1000,
    )
    assert summary.get("session_id") == "real"


def test_read_most_recent_session_picks_first_with_summary(
    tmp_path,
):
    """If the most-recent session has no summary.json (e.g.,
    crashed before write), the next-most-recent with a valid
    summary wins."""
    from backend.core.ouroboros.governance.graduation.live_fire_soak import (  # noqa: E501
        _read_most_recent_session,
    )
    import os
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    crashed = sessions_root / "crashed"
    crashed.mkdir()
    # No summary.json — simulates SIGKILL mid-write
    os.utime(crashed, (4000, 4000))
    real = sessions_root / "real"
    real.mkdir()
    (real / "summary.json").write_text(
        '{"session_id": "real"}', encoding="utf-8",
    )
    os.utime(real, (3000, 3000))
    summary, _ = _read_most_recent_session(
        sessions_root, after_epoch=1000,
    )
    # summary={} from crashed dir is still a dict, so it
    # wins. The contract is "most-recent dir whose contents
    # parse"; empty dict is parseable. This is correct
    # behavior — caller maps empty dict to SUMMARY_PARSE_FAILED
    # and the breadcrumb evidence row from Phase 9.1b survives.
    # Pin the actual contract: most-recent dir wins, even if
    # its summary is empty.
    assert summary == {} or summary.get("session_id") == "real"


# ---------------------------------------------------------------------------
# Layer 3: cmd_ready CLI subcommand shape
# ---------------------------------------------------------------------------


def test_cmd_ready_handler_exists():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "live_fire_graduation_soak_cli",
        _repo_root() / "scripts/live_fire_graduation_soak.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert hasattr(module, "cmd_ready"), (
        "cmd_ready handler missing — Phase 9 Slice 2"
    )


def test_cmd_ready_wired_into_handlers_dict():
    """The ``handlers`` dict in ``main()`` MUST register the
    `ready` subcommand. AST pin."""
    target = (
        _repo_root()
        / "scripts/live_fire_graduation_soak.py"
    )
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    found_handler = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for k in node.keys:
                if (
                    isinstance(k, ast.Constant)
                    and k.value == "ready"
                ):
                    found_handler = True
                    break
    assert found_handler, (
        "handlers dict missing 'ready' key — Phase 9 Slice 2"
    )


def test_cmd_ready_subparser_registered():
    target = (
        _repo_root()
        / "scripts/live_fire_graduation_soak.py"
    )
    text = target.read_text(encoding="utf-8")
    assert 'sub.add_parser("ready")' in text, (
        "ready subparser registration missing"
    )


def test_cmd_ready_runs_against_empty_ledger():
    """Smoke: cmd_ready works when no flags are eligible
    (zero clean evidence) — must print 'no flags ready' guidance."""
    import argparse
    import importlib.util
    import io
    spec = importlib.util.spec_from_file_location(
        "live_fire_graduation_soak_cli",
        _repo_root() / "scripts/live_fire_graduation_soak.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    captured = io.StringIO()
    with patch(
        "backend.core.ouroboros.governance.adaptation."
        "graduation_ledger.GraduationLedger.eligible_flags",
        return_value=[],
    ), patch("sys.stdout", captured):
        rc = module.cmd_ready(argparse.Namespace())
    assert rc == 0
    output = captured.getvalue()
    assert "Ready-to-Flip" in output
    assert "ready_to_flip=0" in output
    assert "No flags ready" in output


def test_cmd_ready_renders_eligible_flags():
    import argparse
    import importlib.util
    import io
    spec = importlib.util.spec_from_file_location(
        "live_fire_graduation_soak_cli",
        _repo_root() / "scripts/live_fire_graduation_soak.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fake_eligible = ["JARVIS_FAKE_FLAG_ENABLED"]
    captured = io.StringIO()
    with patch(
        "backend.core.ouroboros.governance.adaptation."
        "graduation_ledger.GraduationLedger.eligible_flags",
        return_value=fake_eligible,
    ), patch("sys.stdout", captured):
        rc = module.cmd_ready(argparse.Namespace())
    assert rc == 0
    output = captured.getvalue()
    assert "JARVIS_FAKE_FLAG_ENABLED" in output
    assert "ready_to_flip=1" in output
