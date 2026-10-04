"""Resource-aware test execution lock (2026-10-04 guest OOM that killed a soak).

Pins the admission arithmetic, the learned need estimate, machine-wide soak
detection, the exemptions, the shared soak-cap formula, and -- live, where a
systemd user manager exists -- that the kernel really contains an overrun and
that pytest really refuses with exit 75.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import test_execution_lock as tel
from backend.core.ouroboros.governance.process_memory_budget import (
    resolve_process_memory_thresholds,
)

GIB = 1024 ** 3
_REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    for name in (tel.ENV_SAFETY_FRACTION, tel.ENV_FLOOR_FRACTION, tel.ENV_SCALE_BAND,
                 tel.ENV_TRUNCATION_MARGIN, tel.ENV_ENABLED, "PYTEST_XDIST_WORKER",
                 "JARVIS_OUROBOROS_SESSION_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(tel.ENV_LEDGER, str(tmp_path / "ledger.jsonl"))


# ── admission arithmetic ────────────────────────────────────────────────


def _tenant(cap_gib, tree_gib, pid=4242):
    return tel.SoakTenant(pid=pid, cap_bytes=int(cap_gib * GIB), tree_bytes=int(tree_gib * GIB))


def test_soak_licence_is_reserved_not_just_its_current_use():
    # 40 GiB guest, 34 free; the soak uses 3 but is licensed to 30.
    g = tel.compute_grant(total_bytes=40 * GIB, available_bytes=34 * GIB,
                          cgroup_headroom=None, tenants=[_tenant(30, 3)], sibling_reserve=0)
    assert g.admitted
    assert g.tenant_reserve_bytes == 27 * GIB
    assert g.granted_bytes == 34 * GIB - 27 * GIB - g.safety_bytes


def test_refused_when_the_soak_licence_leaves_less_than_the_floor():
    g = tel.compute_grant(total_bytes=40 * GIB, available_bytes=28 * GIB,
                          cgroup_headroom=None, tenants=[_tenant(30, 3)], sibling_reserve=0)
    assert not g.admitted and g.refusal == "insufficient_headroom"
    assert g.granted_bytes == 0


def test_unbounded_soak_refuses_everything():
    t = tel.SoakTenant(pid=7, cap_bytes=None, tree_bytes=GIB)
    g = tel.compute_grant(total_bytes=40 * GIB, available_bytes=39 * GIB,
                          cgroup_headroom=None, tenants=[t], sibling_reserve=0)
    assert g.refusal == "soak_cap_unbounded:7"


def test_sibling_grants_and_cgroup_ceiling_are_subtracted():
    base = dict(total_bytes=40 * GIB, available_bytes=30 * GIB, tenants=[])
    free = tel.compute_grant(cgroup_headroom=None, sibling_reserve=0, **base)
    sib = tel.compute_grant(cgroup_headroom=None, sibling_reserve=5 * GIB, **base)
    capped = tel.compute_grant(cgroup_headroom=10 * GIB, sibling_reserve=0, **base)
    assert free.granted_bytes - sib.granted_bytes == 5 * GIB
    assert capped.granted_bytes == 10 * GIB - capped.safety_bytes


def test_unprobeable_memory_is_refused():
    g = tel.compute_grant(total_bytes=0, available_bytes=0, cgroup_headroom=None,
                          tenants=[], sibling_reserve=0)
    assert g.refusal == "memory_unprobeable"


def test_safety_and_floor_follow_env(monkeypatch):
    monkeypatch.setenv(tel.ENV_SAFETY_FRACTION, "0.10")
    monkeypatch.setenv(tel.ENV_FLOOR_FRACTION, "0.50")
    g = tel.compute_grant(total_bytes=40 * GIB, available_bytes=30 * GIB,
                          cgroup_headroom=None, tenants=[], sibling_reserve=0)
    assert g.safety_bytes == 4 * GIB and g.floor_bytes == 20 * GIB
    assert g.admitted  # 30 - 4 = 26 >= 20


# ── learned need ────────────────────────────────────────────────────────


def _rec(items, peak_gib, oom=0):
    return tel.RunRecord(ts=0.0, items=items, peak_bytes=int(peak_gib * GIB),
                         granted_bytes=int(peak_gib * GIB), oom_kills=oom)


def test_no_history_falls_back_to_floor():
    est = tel.predict_need(1600, [], total_bytes=40 * GIB)
    assert est.basis == "floor_no_comparable_history"
    assert est.need_bytes == int(40 * GIB * 0.05)


def test_same_scale_worst_peak_wins_and_other_scales_are_ignored():
    records = [_rec(1500, 3), _rec(1700, 6), _rec(20, 30), _rec(40000, 25)]
    est = tel.predict_need(1600, records, total_bytes=40 * GIB)
    assert est.need_bytes == 6 * GIB and est.samples == 2


def test_an_oom_killed_run_proves_a_higher_need():
    est = tel.predict_need(1600, [_rec(1600, 8, oom=1)], total_bytes=40 * GIB)
    assert est.need_bytes == int(8 * GIB * 1.25)


def test_ledger_round_trip(tmp_path):
    path = tmp_path / "l.jsonl"
    assert tel.record_run(_rec(10, 1), path)
    assert tel.record_run(_rec(20, 2, oom=3), path)
    got = tel.read_ledger(path)
    assert [r.items for r in got] == [10, 20] and got[1].truncated


# ── machine-wide soak detection (fake /proc) ────────────────────────────


def _fake_proc(root: Path, pid: int, argv, environ=None):
    d = root / str(pid)
    d.mkdir(parents=True)
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    env = environ or {}
    (d / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in env.items()))


def test_daemon_found_from_any_checkout_supervisor_excluded(tmp_path):
    _fake_proc(tmp_path, 100, ["/v/bin/python3", "scripts/ouroboros_battle_test.py", "--headless"])
    _fake_proc(tmp_path, 101, ["/v/bin/python3", "/home/x/wt-other/scripts/ouroboros_battle_test.py"])
    # The supervisor embeds the daemon argv, but runs a different program.
    _fake_proc(tmp_path, 102, ["/v/bin/python3", "-m", "backend.core.ouroboros.battle_test.terminal_supervisor",
                               "--", "/v/bin/python3", "scripts/ouroboros_battle_test.py"])
    _fake_proc(tmp_path, 103, ["bash", "-c", "pgrep -f ouroboros_battle_test.py"])
    assert tel.find_soak_pids(tmp_path) == [100, 101]


def test_tenant_cap_uses_the_soaks_own_environment(tmp_path):
    _fake_proc(tmp_path, 100, ["py", "scripts/ouroboros_battle_test.py"],
               {"JARVIS_PROCESS_MEMORY_CAP_MB": "10240"})
    _fake_proc(tmp_path, 101, ["py", "scripts/ouroboros_battle_test.py"])
    tenants = tel.soak_tenants(total_bytes=40 * GIB, proc_root=tmp_path,
                               tree_probe=lambda pid: 1024.0)
    by_pid = {t.pid: t for t in tenants}
    assert by_pid[100].cap_bytes == 10 * GIB
    assert by_pid[101].cap_bytes == int(40 * GIB * 0.75)  # default fraction
    assert by_pid[100].reserve_bytes == 9 * GIB


def test_unprobeable_tree_reserves_the_whole_cap(tmp_path):
    _fake_proc(tmp_path, 100, ["py", "scripts/ouroboros_battle_test.py"],
               {"JARVIS_PROCESS_MEMORY_CAP_MB": "1024"})
    (t,) = tel.soak_tenants(total_bytes=40 * GIB, proc_root=tmp_path, tree_probe=lambda pid: None)
    assert t.reserve_bytes == GIB


# ── cgroup accounting (fake cgroupfs) ───────────────────────────────────


def _cg(root: Path, rel: str, max_="max", current=0):
    d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    (d / "memory.max").write_text(f"{max_}\n")
    (d / "memory.current").write_text(f"{current}\n")
    return d


def test_headroom_is_the_tightest_finite_ancestor(tmp_path):
    _cg(tmp_path, "user.slice", max_=str(20 * GIB), current=str(15 * GIB))
    _cg(tmp_path, "user.slice/app.slice", max_="max", current=str(GIB))
    assert tel.cgroup_headroom_bytes("/user.slice/app.slice", tmp_path) == 5 * GIB
    _cg(tmp_path, "init.scope")
    assert tel.cgroup_headroom_bytes("/init.scope", tmp_path) is None


def test_sibling_scopes_reserve_their_unused_grant(tmp_path):
    app = "user.slice/user-1000.slice/user@1000.service/app.slice"
    _cg(tmp_path, f"{app}/{tel.SCOPE_PREFIX}1.scope", max_=str(4 * GIB), current=str(GIB))
    _cg(tmp_path, f"{app}/{tel.SCOPE_PREFIX}2.scope", max_=str(2 * GIB), current=str(2 * GIB))
    _cg(tmp_path, f"{app}/unrelated.scope", max_=str(9 * GIB), current="0")
    assert tel.sibling_scope_reserve(tmp_path) == 3 * GIB
    assert tel.sibling_scope_reserve(tmp_path, exclude=f"{tel.SCOPE_PREFIX}1.scope") == 0


# ── exemptions ──────────────────────────────────────────────────────────


def _self_cgroup(tmp_path, path):
    (tmp_path / "self").mkdir(parents=True, exist_ok=True)
    (tmp_path / "self" / "cgroup").write_text(f"0::{path}\n")
    return tmp_path


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="linux exemption matrix")
def test_exemption_matrix(tmp_path, monkeypatch):
    plain = _self_cgroup(tmp_path / "a", "/init.scope")
    governed = _self_cgroup(tmp_path / "b", f"/user.slice/x/app.slice/{tel.SCOPE_PREFIX}9.scope")
    assert tel.exemption_reason({}, proc_root=plain) is None
    assert tel.exemption_reason({}, proc_root=governed) == "already_inside_governed_scope"
    assert tel.exemption_reason({"PYTEST_XDIST_WORKER": "gw0"}, proc_root=plain).startswith("xdist")
    monkeypatch.setenv(tel.ENV_ENABLED, "false")
    assert tel.exemption_reason({}, proc_root=plain) == "disabled"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="linux exemption matrix")
def test_soak_validate_child_is_exempt_only_while_the_soak_lives(tmp_path):
    proc = _self_cgroup(tmp_path / "p", "/init.scope")
    session = tmp_path / "session"
    session.mkdir()
    env = {"JARVIS_OUROBOROS_SESSION_DIR": str(session)}
    (session / "heartbeat.tick").write_text(str(time.time()))
    assert tel.exemption_reason(env, proc_root=proc) == "soak_tenant_inside_soak_budget"
    (session / "heartbeat.tick").write_text(str(time.time() - 10_000))
    assert tel.exemption_reason(env, proc_root=proc) is None  # dead soak: gate it


# ── one formula for the soak cap ────────────────────────────────────────


@pytest.mark.parametrize("env,total_mb,expected_cap", [
    ({}, 40960.0, 40960.0 * 0.75),
    ({"JARVIS_PROCESS_MEMORY_CAP_FRACTION": "0.5"}, 40960.0, 20480.0),
    ({"JARVIS_PROCESS_MEMORY_CAP_FRACTION": "5"}, 1000.0, 950.0),  # clamped
    ({"JARVIS_PROCESS_MEMORY_CAP_MB": "1234"}, 40960.0, 1234.0),
    ({"JARVIS_PROCESS_MEMORY_WATCHDOG_ENABLED": "false"}, 40960.0, None),
])
def test_cap_formula(env, total_mb, expected_cap):
    warn, cap, interval = resolve_process_memory_thresholds(env, total_mb=total_mb)
    assert cap == expected_cap
    if cap is not None:
        assert warn == pytest.approx(cap * 0.85) and 2.0 <= interval <= 120.0


def test_harness_watchdog_delegates_to_the_shared_formula():
    src = (_REPO / "backend/core/ouroboros/battle_test/harness.py").read_text(encoding="utf-8")
    body = src.split("def _resolve_process_memory_thresholds", 1)[1].split("\n    def ", 1)[0]
    assert "resolve_process_memory_thresholds()" in body
    # No second copy of the formula: no env parsing, no RAM probe of its own.
    assert "os.environ" not in body and "virtual_memory" not in body


def test_root_conftest_registers_the_plugin():
    src = (_REPO / "conftest.py").read_text(encoding="utf-8")
    assert '"tests.support.execution_lock_plugin"' in src


# ── live: the kernel contains, pytest refuses ───────────────────────────


def _user_manager_available() -> bool:
    if not sys.platform.startswith("linux") or not shutil.which("busctl") or not shutil.which("systemd-run"):
        return False
    probe = subprocess.run(["systemctl", "--user", "is-system-running"],
                           capture_output=True, text=True, timeout=10)
    return probe.stdout.strip() in ("running", "degraded")


live = pytest.mark.skipif(not _user_manager_available(), reason="needs a systemd user manager")


@live
def test_adopted_scope_kills_the_overrun_and_spares_the_parent():
    script = textwrap.dedent(f"""
        import subprocess, sys
        from backend.core.ouroboros.governance import test_execution_lock as tel
        unit = tel.adopt_into_scope(256 * 1024 * 1024)
        assert unit, "adoption failed"
        child = subprocess.run([sys.executable, "-c", "b = bytearray(768 * 1024 * 1024)"])
        peak, ooms = tel.scope_stats()
        print("RESULT", unit, child.returncode, ooms, peak)
    """)
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                         cwd=_REPO, timeout=60)
    line = next(l for l in out.stdout.splitlines() if l.startswith("RESULT"))
    _, unit, child_rc, ooms, peak = line.split()
    assert unit.startswith(tel.SCOPE_PREFIX)
    assert int(child_rc) == -9 and int(ooms) >= 1
    assert int(peak) <= 256 * 1024 * 1024


@live
def test_pytest_refuses_with_exit_75_when_headroom_is_short(tmp_path):
    (tmp_path / "test_trivial.py").write_text("def test_ok():\n    assert True\n")
    env = dict(os.environ, **{tel.ENV_FLOOR_FRACTION: "1.0", tel.ENV_LEDGER: str(tmp_path / "l.jsonl")})
    env.pop("JARVIS_OUROBOROS_SESSION_DIR", None)
    # A plain (non ov-pytest) scope: outside any governed scope, so the gate runs.
    cmd = ["systemd-run", "--user", "--scope", "--quiet", "--collect",
           # tmp_path is outside the repo, so the root conftest does not load;
           # name the plugin it registers (that registration is pinned above).
           sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
           "-p", "tests.support.execution_lock_plugin",
           "--rootdir", str(_REPO), "-c", str(_REPO / "pytest.ini"),
           str(tmp_path / "test_trivial.py")]
    out = subprocess.run(cmd, capture_output=True, text=True, cwd=_REPO, env=env, timeout=180)
    assert out.returncode == tel.EXIT_RESOURCE_EXHAUSTION, out.stdout[-2000:] + out.stderr[-2000:]
    assert "ResourceExhaustion" in out.stdout + out.stderr
