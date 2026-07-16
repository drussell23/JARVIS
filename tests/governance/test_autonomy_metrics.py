"""Goal Metric Dashboard — aggregator + endpoint spine.

Step 1 of the autonomy pivot: an OBJECTIVE measurement surface for the O+V
manifesto goal (a codebase that engineers its own evolution). These tests
pin the four mandates:

  1. Root-cause / measurement only — the aggregator reads git + session
     summaries, computes nothing behavioral.
  2. Architectural purity — dynamically aggregated from existing state, no
     hardcoded success criteria (only counts + division-guarded ratios).
  3. DRY — composes auto_committer.ov_coauthor_line() (the canonical marker)
     and the existing summary.json set; rides the existing observability GET.
  4. Bulletproof — a landed change is IMPOSSIBLE to count without a
     trailer-bearing, NON-EMPTY, branch-reachable commit; every zero-output
     state returns a clean snapshot, never an exception.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Dict, List, Optional

from aiohttp.test_utils import make_mocked_request

import backend.core.ouroboros.governance.autonomy_metrics as am


# ---------------------------------------------------------------------------
# git-log fixture: a fake subprocess.run producing the module's own format
# ---------------------------------------------------------------------------

_TRAILER = "Co-Authored-By: Ouroboros+Venom <ouroboros@jarvis.trinity>"
_SIG = "Ouroboros+Venom [O+V] — Autonomous Self-Development Engine"


def _commit_block(sha: str, ct: int, body: str, files: List[str]) -> str:
    hdr = f"{am._REC_SEP}\n{sha}\n{ct}\n{body}\n__OV_AUTONOMY_ENDHDR__"
    return hdr + "\n" + "\n".join(files) + "\n"


def _ov_body(subject: str, risk: str = "SAFE_AUTO", op: str = "op-x") -> str:
    return (
        f"{subject}\n\nWhy: because.\n\nOp-ID: {op}\n"
        f"Risk: {risk} (Green)\nProvider: claude ($0.01)\nFiles: a.py\n\n"
        f"{_SIG}\n{_TRAILER}\n"
    )


class _FakeGit:
    """subprocess.run stand-in returning canned git-log stdout."""

    def __init__(self, stdout: str, returncode: int = 0):
        self._stdout = stdout
        self._rc = returncode

    def __call__(self, *args, **kwargs):
        class _R:
            pass
        r = _R()
        r.stdout = self._stdout
        r.returncode = self._rc
        return r


# now, and session epochs a few hours inside the 30-day window of it. The
# fake git ignores --since (git does that server-side), so commit times don't
# matter; sessions ARE filtered in-process by dir-name epoch, so they must sit
# inside the window. `bt-iso-<epoch>` naming makes the intended epoch explicit.
_NOW = 4102444800.0            # year ~2100
_SESS_EPOCH_BASE = int(_NOW - 4800)  # 80 min before now → in-window


def _make_repo(tmp_path: Path, summaries: List[Dict]) -> Path:
    """Create a repo skeleton with .ouroboros/sessions/*/summary.json."""
    (tmp_path / ".git").mkdir()
    sess = tmp_path / ".ouroboros" / "sessions"
    sess.mkdir(parents=True)
    for i, s in enumerate(summaries):
        d = sess / f"bt-iso-{_SESS_EPOCH_BASE + i}"
        d.mkdir()
        (d / "summary.json").write_text(json.dumps(s), encoding="utf-8")
    return tmp_path


def _agg(tmp_path, git_stdout, summaries, **env):
    """Run the aggregator against a fake repo + fake git."""
    repo = _make_repo(tmp_path, summaries)
    for k, v in env.items():
        os.environ[k] = v
    try:
        return am.aggregate_autonomy_metrics(
            repo_root=repo,
            now=_NOW,
            git_runner=_FakeGit(git_stdout),
        )
    finally:
        for k in env:
            os.environ.pop(k, None)


# ---------------------------------------------------------------------------
# Bulletproof: what counts as a landed change (mandate 4)
# ---------------------------------------------------------------------------


def test_trailered_nonempty_commit_counts(tmp_path):
    raw = _commit_block("aaaa1111", 4000000000, _ov_body("fix(x): real"), ["x.py"])
    snap = _agg(tmp_path, raw, [])
    assert snap.landed_count == 1
    assert snap.net_positive_landed == 1


def test_commit_without_trailer_never_counts(tmp_path):
    body = "fix(x): human change\n\nCo-Authored-By: Someone <a@b.c>\n"
    raw = _commit_block("bbbb2222", 4000000000, body, ["x.py"])
    snap = _agg(tmp_path, raw, [])
    assert snap.landed_count == 0


def test_trailered_but_empty_diff_never_counts(tmp_path):
    """Mandate 4: no files changed → NOT genuinely mutated → not landed."""
    raw = _commit_block("cccc3333", 4000000000, _ov_body("chore: empty"), [])
    snap = _agg(tmp_path, raw, [])
    assert snap.landed_count == 0


def test_orange_review_commit_excluded(tmp_path):
    """Human-gated Orange-tier review commits carry the review subject +
    DO NOT AUTO-MERGE and no trailer — excluded defense-in-depth."""
    body = (
        "chore(ouroboros-review): parked change\n\nDO NOT AUTO-MERGE\n\n"
        "Op-ID: op-o\nRisk: APPROVAL_REQUIRED (Orange)\n"
    )  # note: no trailer anyway
    raw = _commit_block("dddd4444", 4000000000, body, ["x.py"])
    snap = _agg(tmp_path, raw, [])
    assert snap.landed_count == 0


def test_no_ov_marker_reason_code(monkeypatch, tmp_path):
    """If the canonical marker can't resolve, fail-closed to zero + reason."""
    monkeypatch.setattr(am, "_ov_trailer", lambda: "")
    raw = _commit_block("eeee5555", 4000000000, _ov_body("fix: x"), ["x.py"])
    snap = _agg(tmp_path, raw, [])
    assert snap.landed_count == 0
    assert snap.reason_code == "no_ov_marker"


# ---------------------------------------------------------------------------
# Regression rate (mandate 2 — objective, git-provable)
# ---------------------------------------------------------------------------


def test_reverted_landed_reduces_net_positive(tmp_path):
    landed = _commit_block(
        "ffff6666aaaa", 4000000000, _ov_body("test(req): torch"), ["r.txt"]
    )
    revert = _commit_block(
        "1111revert", 4000000100,
        "Revert \"test(req): torch\"\n\nThis reverts commit ffff6666aaaa.\n",
        ["r.txt"],
    )
    snap = _agg(tmp_path, landed + revert, [])
    assert snap.landed_count == 1
    assert snap.reverted_landed == 1
    assert snap.net_positive_landed == 0
    assert snap.post_landing_regression_rate == 1.0


def test_revert_of_nonautonomous_commit_ignored(tmp_path):
    landed = _commit_block("aaaa7777", 4000000000, _ov_body("fix: keep"), ["k.py"])
    revert = _commit_block(
        "2222revert", 4000000100,
        "Revert \"human thing\"\n\nThis reverts commit deadbeef.\n", ["h.py"],
    )
    snap = _agg(tmp_path, landed + revert, [])
    assert snap.landed_count == 1
    assert snap.reverted_landed == 0
    assert snap.post_landing_regression_rate == 0.0


def test_pipeline_verify_regressions_from_summaries(tmp_path):
    raw = _commit_block("aaaa8888", 4000000000, _ov_body("fix: y"), ["y.py"])
    summaries = [{
        "duration_s": 1000.0, "cost_total": 0.5,
        "operations": [
            {"op_id": "1", "status": "failed",
             "terminal_reason_code": "verify_regression"},
            {"op_id": "2", "status": "completed",
             "terminal_reason_code": "done"},
            {"op_id": "3", "status": "failed",
             "terminal_reason_code": "verify_regression"},
        ],
    }]
    snap = _agg(tmp_path, raw, summaries)
    assert snap.pipeline_caught_verify_regressions == 2
    assert snap.total_ops_in_window == 3


# ---------------------------------------------------------------------------
# Throughput + cost normalization
# ---------------------------------------------------------------------------


def test_per_unattended_day_and_cost_per_change(tmp_path):
    raw = (
        _commit_block("c1aaaa", 4000000000, _ov_body("fix: a", "SAFE_AUTO"), ["a.py"])
        + _commit_block("c2bbbb", 4000000001, _ov_body("fix: b", "NOTIFY_APPLY"), ["b.py"])
    )
    # 43200s = 0.5 day of honest runtime; $1.00 total.
    summaries = [
        {"duration_s": 43200.0, "cost_total": 0.6,
         "cost_breakdown": {"claude": 0.6}, "operations": []},
        {"duration_s": 0.0, "cost_total": 0.4,
         "cost_breakdown": {"doubleword": 0.4}, "operations": []},
    ]
    snap = _agg(tmp_path, raw, summaries)
    assert snap.landed_count == 2
    assert abs(snap.unattended_days - 0.5) < 1e-6
    assert abs(snap.landed_per_unattended_day - 4.0) < 1e-6  # 2 / 0.5
    assert abs(snap.total_cost_usd - 1.0) < 1e-9
    assert abs(snap.cost_per_landed_change_usd - 0.5) < 1e-9
    assert snap.landed_by_risk_tier == {"safe_auto": 1, "notify_apply": 1}


def test_suspension_likely_session_excluded_from_runtime(tmp_path):
    raw = _commit_block("susp1", 4000000000, _ov_body("fix: s"), ["s.py"])
    summaries = [
        {"duration_s": 9999.0, "cost_total": 0.1, "suspension_likely": True,
         "operations": []},
        {"duration_s": 86400.0, "cost_total": 0.2, "operations": []},
    ]
    snap = _agg(tmp_path, raw, summaries)
    # Only the honest 1-day session counts toward runtime.
    assert abs(snap.unattended_days - 1.0) < 1e-6
    assert snap.sessions_counted == 1
    assert snap.sessions_suspension_excluded == 1


# ---------------------------------------------------------------------------
# Zero-output safety (mandate 4) — never divide-by-zero, never raise
# ---------------------------------------------------------------------------


def test_zero_landed_zero_runtime_is_clean(tmp_path):
    snap = _agg(tmp_path, "", [])
    assert snap.landed_count == 0
    assert snap.landed_per_unattended_day is None
    assert snap.post_landing_regression_rate is None
    assert snap.cost_per_landed_change_usd is None
    assert snap.reason_code in ("no_git_history", "ok")
    # to_dict must be fully-formed with Nones, not an exception.
    d = snap.to_dict()
    assert d["throughput"]["landed_per_unattended_day"] is None
    assert d["cost"]["per_landed_change_usd"] is None


def test_git_unavailable_returns_clean_snapshot(tmp_path):
    repo = _make_repo(tmp_path, [])
    snap = am.aggregate_autonomy_metrics(
        repo_root=repo, now=4102444800.0,
        git_runner=_FakeGit("", returncode=128),  # git failure
    )
    assert snap.landed_count == 0
    assert snap.reason_code in ("no_git_history", "ok")


def test_malformed_summary_does_not_raise(tmp_path):
    repo = tmp_path
    (repo / ".git").mkdir()
    sess = repo / ".ouroboros" / "sessions" / "bt-2026-07-16-000000"
    sess.mkdir(parents=True)
    (sess / "summary.json").write_text("{not valid json", encoding="utf-8")
    snap = am.aggregate_autonomy_metrics(
        repo_root=repo, now=4102444800.0, git_runner=_FakeGit(""),
    )
    assert snap.sessions_counted == 0  # unparseable summary skipped, no raise


def test_aggregator_never_raises_on_internal_fault(monkeypatch, tmp_path):
    def _boom(*a, **k):
        raise RuntimeError("git exploded")

    monkeypatch.setattr(am, "_run_git_log", _boom)
    snap = am.aggregate_autonomy_metrics(repo_root=tmp_path, now=1.0)
    assert snap.reason_code == "aggregation_error"


# ---------------------------------------------------------------------------
# Env knobs + master flag
# ---------------------------------------------------------------------------


def test_master_flag_default_true(monkeypatch):
    monkeypatch.delenv("JARVIS_AUTONOMY_METRICS_ENABLED", raising=False)
    assert am.master_enabled() is True
    monkeypatch.setenv("JARVIS_AUTONOMY_METRICS_ENABLED", "0")
    assert am.master_enabled() is False


def test_snapshot_master_off_returns_disabled(monkeypatch):
    monkeypatch.setenv("JARVIS_AUTONOMY_METRICS_ENABLED", "false")
    am.reset_cache_for_tests()
    d = am.snapshot(force=True)
    assert d["enabled"] is False and d["reason_code"] == "disabled"


def test_window_and_branch_env(monkeypatch):
    monkeypatch.setenv("JARVIS_AUTONOMY_METRICS_WINDOW_DAYS", "7")
    assert am.window_days() == 7
    monkeypatch.setenv("JARVIS_AUTONOMY_METRICS_WINDOW_DAYS", "bad")
    assert am.window_days() == 30
    monkeypatch.setenv("JARVIS_AUTONOMY_METRICS_BRANCH", "develop")
    assert am.target_branch() == "develop"


# ---------------------------------------------------------------------------
# Endpoint — mirrors test_ide_observability conventions
# ---------------------------------------------------------------------------


def _make_request(path: str, remote: str = "127.0.0.1"):
    req = make_mocked_request("GET", path, headers={})
    req._transport_peername = (remote, 0)  # type: ignore[attr-defined]
    return req


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _router():
    from backend.core.ouroboros.governance.ide_observability import (
        IDEObservabilityRouter,
    )
    return IDEObservabilityRouter()


def test_endpoint_disabled_when_master_off(monkeypatch):
    monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "false")
    resp = _run(_router()._handle_autonomy(_make_request("/observability/autonomy")))
    assert resp.status == 403
    body = json.loads(resp.body.decode("utf-8"))
    assert body["reason_code"] == "ide_observability.disabled"
    assert "schema_version" in body


def test_endpoint_autonomy_substrate_off(monkeypatch):
    monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "true")
    monkeypatch.setenv("JARVIS_AUTONOMY_METRICS_ENABLED", "false")
    resp = _run(_router()._handle_autonomy(_make_request("/observability/autonomy")))
    assert resp.status == 403
    assert json.loads(resp.body.decode("utf-8"))["reason_code"] == (
        "ide_observability.autonomy_disabled"
    )


def test_endpoint_returns_snapshot(monkeypatch):
    monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "true")
    monkeypatch.setenv("JARVIS_AUTONOMY_METRICS_ENABLED", "true")
    am.reset_cache_for_tests()
    resp = _run(_router()._handle_autonomy(_make_request("/observability/autonomy")))
    assert resp.status == 200
    body = json.loads(resp.body.decode("utf-8"))
    assert body["schema_version"] == am.AUTONOMY_METRICS_SCHEMA_VERSION
    assert "landed" in body and "throughput" in body and "cost" in body
    assert resp.headers.get("Cache-Control") == "no-store"


def test_endpoint_never_500s(monkeypatch):
    monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "true")
    monkeypatch.setenv("JARVIS_AUTONOMY_METRICS_ENABLED", "true")

    def _boom(**k):
        raise RuntimeError("snapshot exploded")

    monkeypatch.setattr(am, "snapshot", _boom)
    resp = _run(_router()._handle_autonomy(_make_request("/observability/autonomy")))
    assert resp.status == 200
    assert json.loads(resp.body.decode("utf-8"))["reason_code"] == (
        "autonomy.unavailable"
    )
