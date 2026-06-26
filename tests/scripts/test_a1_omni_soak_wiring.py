"""Omni-Soak wiring TDD spine.

Proves the ``JARVIS_A1_OMNI_SOAK`` flag arms the omni activation overlay (full
MAS / fan-out stack) + the DecomposableChaosInjector 3-target inject (3-way swarm
fan-out) -- and that with the flag OFF the normal A1 soak is byte-identical (the
linux prod overlay + single-target inject).

Constraints mirror the harness: ``from __future__ import annotations``, Python
3.9+, ASCII-only. No spend, no subprocess to the real injector -- the chaos
``runner`` is mocked so we assert the exact argv the harness would subprocess.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "scripts", "a1_live_fire_chaos_harness.py",
)
_spec = importlib.util.spec_from_file_location("a1_live_fire_chaos_harness", _SCRIPT)
assert _spec and _spec.loader
harness = importlib.util.module_from_spec(_spec)
sys.modules["a1_live_fire_chaos_harness"] = harness
_spec.loader.exec_module(harness)


# ===========================================================================
# Fakes (mirror test_a1_live_fire_harness.py so the sequence test composes).
# ===========================================================================


class FakeChaos:
    """Records the chaos lifecycle calls in order -- supports BOTH the single
    ``inject`` and the decomposable ``inject_decomposable`` paths."""

    def __init__(self, *, candidates=3, inject_red=True, decomp_count=3):
        self.calls = []
        self._candidates = candidates
        self._inject_red = inject_red
        self._decomp_count = decomp_count

    def status(self):
        self.calls.append("status")
        return {"active": False}

    def list_candidates(self):
        self.calls.append("list_candidates")
        return self._candidates

    def inject(self, seed):
        self.calls.append(("inject", seed))
        return self._inject_red

    def inject_decomposable(self, n=3):
        self.calls.append(("inject_decomposable", n))
        return (self._inject_red, self._decomp_count)

    def revert(self):
        self.calls.append("revert")
        return True


class FakeSoak:
    def __init__(self, debug_log):
        self._debug_log = debug_log
        self.calls = []

    def launch(self, env, run_dir):
        self.calls.append(("launch", run_dir, env))
        return harness.SoakHandle(debug_log=self._debug_log, session_dir=run_dir, proc=None)

    def stop(self):
        self.calls.append("stop")


class FakeAuditor:
    def __init__(self, *, proven=True):
        self._proven = proven
        self.calls = []

    def watch(self, *, base, log_file, timeout_s, verdict_out):
        self.calls.append(("watch", base, log_file))
        verdict = {"verdict": "proven" if self._proven else "failed", "proven": self._proven}
        Path(verdict_out).write_text(json.dumps(verdict))
        return verdict


def _make_run(tmp_path, *, chaos, soak, auditor, **kw):
    return harness.HarnessRun(
        run_id="omni-test-run",
        run_root=str(tmp_path / "a1_runs"),
        autopsy_root=str(tmp_path / "a1_autopsy"),
        cost_cap=0.0,
        wall_seconds=120,
        seed=7,
        sse_base="http://127.0.0.1:8099",
        chaos=chaos,
        soak=soak,
        auditor=auditor,
        **kw,
    )


# ===========================================================================
# 1. compose_env overlay selection: OFF -> linux prod, ON -> omni.
# ===========================================================================


def test_omni_flag_off_loads_linux_overlay(monkeypatch):
    monkeypatch.delenv("JARVIS_A1_OMNI_SOAK", raising=False)
    monkeypatch.delenv("JARVIS_A1_AUDIT_FLAGS", raising=False)
    env = harness.compose_env()
    # Linux prod overlay keys present.
    assert env.get("JARVIS_PROVIDER_CLAUDE_DISABLED") == "true"
    # The omni-only MAS flags are NOT injected by the linux overlay.
    assert "JARVIS_SWARM_ORCHESTRATOR_ENABLED" not in _overlay_only(harness, omni=False)


def test_omni_flag_on_loads_omni_overlay(monkeypatch):
    monkeypatch.setenv("JARVIS_A1_OMNI_SOAK", "1")
    monkeypatch.delenv("JARVIS_A1_AUDIT_FLAGS", raising=False)
    env = harness.compose_env()
    # The omni overlay sources the linux base (superset) AND adds the MAS stack.
    assert env.get("JARVIS_PROVIDER_CLAUDE_DISABLED") == "true", "linux base inherited"
    assert env.get("JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED") == "true"
    assert env.get("JARVIS_SWARM_ORCHESTRATOR_ENABLED") == "true"
    assert env.get("JARVIS_WAVE3_DAG_COMPOSE_ENABLED") == "true"


def test_omni_overlay_is_a_superset_of_linux(monkeypatch):
    """OFF env must be byte-identical to the linux overlay (no omni keys leak)."""
    monkeypatch.delenv("JARVIS_A1_AUDIT_FLAGS", raising=False)
    linux = _overlay_only(harness, omni=False)
    omni = _overlay_only(harness, omni=True)
    # Every linux key is in omni (superset), and omni adds the MAS flags.
    for k, v in linux.items():
        assert omni.get(k) == v, "omni must inherit linux base byte-identical for %s" % k
    assert "JARVIS_SWARM_ORCHESTRATOR_ENABLED" in omni
    assert "JARVIS_SWARM_ORCHESTRATOR_ENABLED" not in linux


def _overlay_only(h, *, omni):
    """The composed overlay dict the harness would merge (no process env noise).
    The omni env ``source``s the linux base in bash, which the line parser cannot
    follow, so the harness composes the inheritance: linux base first, omni on top.
    """
    merged = dict(h._parse_env_overlay(h._LINUX_ENV_OVERLAY))
    if omni:
        merged.update(h._parse_env_overlay(h._OMNI_ENV_OVERLAY))
    return merged


def test_overlay_selection_helper(monkeypatch):
    monkeypatch.delenv("JARVIS_A1_OMNI_SOAK", raising=False)
    assert harness.omni_soak_enabled() is False
    assert harness._selected_overlay_path() == harness._LINUX_ENV_OVERLAY
    monkeypatch.setenv("JARVIS_A1_OMNI_SOAK", "true")
    assert harness.omni_soak_enabled() is True
    assert harness._selected_overlay_path() == harness._OMNI_ENV_OVERLAY


# ===========================================================================
# 2. inject_decomposable parses the decomposable JSON.
# ===========================================================================


def _decomp_runner(count, *, all_red=True):
    """A fake subprocess runner returning the decomposable injector JSON."""
    import subprocess as _sp

    def run(argv):
        if "--inject-decomposable" in argv:
            payload = {
                "status": "injected_decomposable",
                "count": count,
                "requested_n": count,
                "targets": [
                    {
                        "target_file": "pkg/mod_%d.py" % i,
                        "function": "fn_%d" % i,
                        "line": 10 + i,
                        "mutation_kind": "comparison_flip",
                        "test_node": "tests/test_%d.py::test_fn_%d" % (i, i),
                        "test_red_post": all_red,
                    }
                    for i in range(count)
                ],
            }
            return _sp.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")
        if "--status" in argv:
            return _sp.CompletedProcess(argv, 0, stdout=json.dumps({"active": True}), stderr="")
        return _sp.CompletedProcess(argv, 0, stdout="{}", stderr="")

    return run


def test_inject_decomposable_parses_json_ok():
    cc = harness.ChaosController(runner=_decomp_runner(3, all_red=True))
    ok, count = cc.inject_decomposable(3)
    assert ok is True
    assert count == 3


def test_inject_decomposable_subprocesses_correct_cli(monkeypatch):
    seen = {}

    import subprocess as _sp

    def run(argv):
        seen["argv"] = list(argv)
        payload = {"status": "injected_decomposable", "count": 3, "requested_n": 3,
                   "targets": [{"test_red_post": True} for _ in range(3)]}
        return _sp.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")

    cc = harness.ChaosController(runner=run)
    cc.inject_decomposable(3)
    assert "--inject-decomposable" in seen["argv"]
    # The CLI uses -n/--num-targets for the count (zero hardcoding of 3 in argv).
    assert "3" in seen["argv"]
    assert ("-n" in seen["argv"]) or ("--num-targets" in seen["argv"])


def test_inject_decomposable_not_ok_when_short():
    # Asked for 3 but only 2 injected -> not ok.
    cc = harness.ChaosController(runner=_decomp_runner(2, all_red=True))
    ok, count = cc.inject_decomposable(3)
    assert ok is False
    assert count == 2


def test_inject_decomposable_not_ok_when_not_all_red():
    cc = harness.ChaosController(runner=_decomp_runner(3, all_red=False))
    ok, count = cc.inject_decomposable(3)
    assert ok is False


# ===========================================================================
# 3. revert covers the N-entry decomposable manifest (reuses --revert path).
# ===========================================================================


def test_revert_reuses_existing_plumbing():
    seen = {}

    import subprocess as _sp

    def run(argv):
        seen["argv"] = list(argv)
        return _sp.CompletedProcess(argv, 0, stdout="{}", stderr="")

    cc = harness.ChaosController(runner=run)
    assert cc.revert() is True
    assert "--revert" in seen["argv"]


# ===========================================================================
# 4. The inject STEP: OFF -> single inject; ON -> inject_decomposable(3).
# ===========================================================================


def test_inject_step_off_uses_single_target(tmp_path, monkeypatch):
    monkeypatch.delenv("JARVIS_A1_OMNI_SOAK", raising=False)
    debug_log = tmp_path / "debug.log"
    debug_log.write_text("")
    chaos = FakeChaos()
    soak = FakeSoak(str(debug_log))
    auditor = FakeAuditor(proven=True)
    run = _make_run(tmp_path, chaos=chaos, soak=soak, auditor=auditor, omni_soak=False)
    rc = run.execute()
    assert rc == 0
    # Single-target inject was called; decomposable was NOT.
    assert ("inject", 7) in chaos.calls
    assert not any(c[0] == "inject_decomposable" for c in chaos.calls if isinstance(c, tuple))
    assert "revert" in chaos.calls


def test_inject_step_on_uses_decomposable(tmp_path, monkeypatch):
    debug_log = tmp_path / "debug.log"
    debug_log.write_text("")
    chaos = FakeChaos(decomp_count=3)
    soak = FakeSoak(str(debug_log))
    auditor = FakeAuditor(proven=True)
    run = _make_run(tmp_path, chaos=chaos, soak=soak, auditor=auditor, omni_soak=True)
    rc = run.execute()
    assert rc == 0
    # Decomposable 3-target inject was called; single-target was NOT.
    assert ("inject_decomposable", 3) in chaos.calls
    assert not any(
        isinstance(c, tuple) and c[0] == "inject" for c in chaos.calls
    )
    # Revert-ALWAYS still covers it (the N-entry manifest).
    assert "revert" in chaos.calls


def test_inject_step_on_aborts_when_not_all_red(tmp_path):
    debug_log = tmp_path / "debug.log"
    debug_log.write_text("")
    chaos = FakeChaos(inject_red=False, decomp_count=3)
    soak = FakeSoak(str(debug_log))
    auditor = FakeAuditor(proven=True)
    run = _make_run(tmp_path, chaos=chaos, soak=soak, auditor=auditor, omni_soak=True)
    rc = run.execute()
    # Decomposable inject did not turn all RED -> abort, but revert ALWAYS.
    assert rc != 0
    assert "revert" in chaos.calls
    # Soak never launched.
    assert soak.calls == []


def test_default_run_field_off_byte_identical(tmp_path):
    """A HarnessRun built without omni_soak defaults to single-target (OFF)."""
    debug_log = tmp_path / "debug.log"
    debug_log.write_text("")
    chaos = FakeChaos()
    soak = FakeSoak(str(debug_log))
    auditor = FakeAuditor(proven=True)
    run = _make_run(tmp_path, chaos=chaos, soak=soak, auditor=auditor)  # no omni_soak
    assert run.omni_soak is False
    run.execute()
    assert ("inject", 7) in chaos.calls
