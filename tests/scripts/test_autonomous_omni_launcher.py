from __future__ import annotations

"""Tests for the Autonomous Pre-Flight Provisioner (autonomous_omni_launcher.py).

ALL gcloud / subprocess is mocked -- these tests NEVER touch real GCP. They prove
the orchestration contract:

  * image fresh (sha match)        -> NO bake, golden armed, soak invoked
  * image stale / missing          -> bake invoked, then (on success) soak armed
  * bake FAILS                     -> degrade (golden UNSET, severe warning),
                                      soak STILL invoked (never-block guarantee)
  * the anti-zombie sweep fires in finally on success AND on exception AND on
    SIGTERM, deletes both jarvis-soak-bake-* and sovereign-sandbox-*, and never
    raises even when a delete fails
  * the launcher arms META_GOAL + OMNI_SOAK + FAULT_TOLERANT_OBS in the soak env
  * deploy/ouroboros_omni_prod.env exports the omni flags
"""

import importlib.util
import pathlib
import sys

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_LAUNCHER_PATH = _REPO_ROOT / "scripts" / "autonomous_omni_launcher.py"


def _load_launcher():
    spec = importlib.util.spec_from_file_location(
        "autonomous_omni_launcher", str(_LAUNCHER_PATH)
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["autonomous_omni_launcher"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture()
def L():
    return _load_launcher()


# --------------------------------------------------------------------------- #
# A fake _run that records every gcloud command and answers from a script.
# --------------------------------------------------------------------------- #
class FakeRun:
    """Records calls; answers `images list`-style queries from a fixed sha.

    image_sha=None simulates a MISSING image (empty list output).
    delete_rc lets a test force the sweep's delete to fail.
    """

    def __init__(self, image_sha=None, list_instances=None, delete_rc=0):
        self.calls = []
        self.image_sha = image_sha
        # instances the sweep's `instances list` returns, newline-joined.
        self.list_instances = list_instances or []
        self.delete_rc = delete_rc

    def __call__(self, cmd, timeout_s=120.0):
        self.calls.append(list(cmd))
        joined = " ".join(cmd)
        if "images" in cmd and "list" in cmd:
            # Return the sha label (or empty -> missing).
            return (0, (self.image_sha or "") + ("\n" if self.image_sha else ""))
        if "instances" in cmd and "list" in cmd:
            return (0, "\n".join(self.list_instances) + ("\n" if self.list_instances else ""))
        if "instances" in cmd and "delete" in cmd:
            return (self.delete_rc, "" if self.delete_rc == 0 else "boom")
        return (0, "")

    def deletes(self):
        return [c for c in self.calls if "instances" in c and "delete" in c]


# --------------------------------------------------------------------------- #
# requirements sha reuse (no dup).
# --------------------------------------------------------------------------- #
def test_requirements_sha_reused_from_baker(L):
    # The launcher must reuse the baker's sha helper -- same digest for same file.
    from importlib.util import spec_from_file_location, module_from_spec

    spec = spec_from_file_location(
        "_baker_for_test", str(_REPO_ROOT / "scripts" / "bake_soak_golden_image.py")
    )
    baker = module_from_spec(spec)
    spec.loader.exec_module(baker)  # type: ignore[union-attr]
    req = str(_REPO_ROOT / "requirements.txt")
    assert L.current_requirements_sha(req) == baker.requirements_sha(req)


# --------------------------------------------------------------------------- #
# Freshness probe.
# --------------------------------------------------------------------------- #
def test_freshness_fresh_when_label_matches(L, monkeypatch):
    cur = L.current_requirements_sha(L.default_requirements_path())
    fake = FakeRun(image_sha=cur)
    monkeypatch.setattr(L, "_run", fake)
    state = L.check_image_freshness(L.build_config())
    assert state == "fresh"


def test_freshness_stale_when_label_mismatches(L, monkeypatch):
    fake = FakeRun(image_sha="deadbeefdeadbeef")
    monkeypatch.setattr(L, "_run", fake)
    state = L.check_image_freshness(L.build_config())
    assert state == "stale"


def test_freshness_missing_when_no_image(L, monkeypatch):
    fake = FakeRun(image_sha=None)
    monkeypatch.setattr(L, "_run", fake)
    state = L.check_image_freshness(L.build_config())
    assert state == "missing"


# --------------------------------------------------------------------------- #
# Orchestration: fresh -> NO bake, golden armed, soak invoked.
# --------------------------------------------------------------------------- #
def test_fresh_skips_bake_arms_golden_runs_soak(L, monkeypatch):
    cur = L.current_requirements_sha(L.default_requirements_path())
    fake = FakeRun(image_sha=cur)
    monkeypatch.setattr(L, "_run", fake)

    bake_called = {"n": 0}
    monkeypatch.setattr(L, "bake_golden", lambda cfg, env: bake_called.__setitem__("n", bake_called["n"] + 1) or True)

    soak_env = {}

    def fake_soak(cfg, env):
        soak_env.update(env)
        return 0

    monkeypatch.setattr(L, "run_soak", fake_soak)
    monkeypatch.setattr(L, "anti_zombie_sweep", lambda cfg: None)

    rc = L.main(["--i-understand-this-spends-money"])
    assert rc == 0
    assert bake_called["n"] == 0, "fresh image must NOT bake"
    assert soak_env.get("JARVIS_IAC_SOAK_GOLDEN_ENABLED") == "1"


# --------------------------------------------------------------------------- #
# Orchestration: stale/missing -> bake invoked, then soak armed.
# --------------------------------------------------------------------------- #
def test_stale_triggers_bake_then_soak_with_golden(L, monkeypatch):
    fake = FakeRun(image_sha="staleeeeeeeeeeee")
    monkeypatch.setattr(L, "_run", fake)

    bake_called = {"n": 0}

    def fake_bake(cfg, env):
        bake_called["n"] += 1
        return True  # bake succeeds

    monkeypatch.setattr(L, "bake_golden", fake_bake)

    soak_env = {}
    monkeypatch.setattr(L, "run_soak", lambda cfg, env: soak_env.update(env) or 0)
    monkeypatch.setattr(L, "anti_zombie_sweep", lambda cfg: None)

    rc = L.main(["--i-understand-this-spends-money"])
    assert rc == 0
    assert bake_called["n"] == 1, "stale image must trigger exactly one bake"
    assert soak_env.get("JARVIS_IAC_SOAK_GOLDEN_ENABLED") == "1"


# --------------------------------------------------------------------------- #
# CONSTRAINT 2 -- bake FAILS -> degrade (golden UNSET), soak STILL runs.
# --------------------------------------------------------------------------- #
def test_bake_failure_degrades_but_soak_still_runs(L, monkeypatch, capsys):
    fake = FakeRun(image_sha=None)  # missing -> wants a bake
    monkeypatch.setattr(L, "_run", fake)
    monkeypatch.setattr(L, "bake_golden", lambda cfg, env: False)  # bake FAILS

    soak_called = {"n": 0}
    soak_env = {}

    def fake_soak(cfg, env):
        soak_called["n"] += 1
        soak_env.update(env)
        return 0

    monkeypatch.setattr(L, "run_soak", fake_soak)
    monkeypatch.setattr(L, "anti_zombie_sweep", lambda cfg: None)

    rc = L.main(["--i-understand-this-spends-money"])
    assert rc == 0
    assert soak_called["n"] == 1, "bake failure must NEVER abort the soak"
    # Golden must be UNSET (or not '1') so the harness uses raw Debian + pip.
    assert soak_env.get("JARVIS_IAC_SOAK_GOLDEN_ENABLED") != "1"
    out = capsys.readouterr().out
    assert "golden bake FAILED" in out
    assert "degrading to raw Debian" in out


# --------------------------------------------------------------------------- #
# CONSTRAINT 4 -- flags armed in the soak env.
# --------------------------------------------------------------------------- #
def test_launcher_arms_meta_goal_omni_and_fault_tolerant_flags(L, monkeypatch):
    cur = L.current_requirements_sha(L.default_requirements_path())
    fake = FakeRun(image_sha=cur)
    monkeypatch.setattr(L, "_run", fake)

    soak_env = {}
    monkeypatch.setattr(L, "run_soak", lambda cfg, env: soak_env.update(env) or 0)
    monkeypatch.setattr(L, "anti_zombie_sweep", lambda cfg: None)

    rc = L.main(["--i-understand-this-spends-money"])
    assert rc == 0
    assert soak_env.get("JARVIS_META_GOAL_AGGREGATOR_ENABLED") == "1"
    assert soak_env.get("JARVIS_A1_OMNI_SOAK") == "1"
    assert soak_env.get("JARVIS_IAC_FAULT_TOLERANT_OBS_ENABLED") == "1"


# --------------------------------------------------------------------------- #
# CONSTRAINT 3 -- the sweep fires in finally on SUCCESS, deletes both VM types.
# --------------------------------------------------------------------------- #
def test_sweep_fires_on_success_deletes_both_vm_types(L, monkeypatch):
    cur = L.current_requirements_sha(L.default_requirements_path())
    fake = FakeRun(
        image_sha=cur,
        list_instances=["jarvis-soak-bake-20260101-99", "sovereign-sandbox-20260101-1"],
    )
    monkeypatch.setattr(L, "_run", fake)
    monkeypatch.setattr(L, "run_soak", lambda cfg, env: 0)

    rc = L.main(["--i-understand-this-spends-money"])
    assert rc == 0
    deleted = " ".join(" ".join(d) for d in fake.deletes())
    assert "jarvis-soak-bake-20260101-99" in deleted
    assert "sovereign-sandbox-20260101-1" in deleted


# --------------------------------------------------------------------------- #
# CONSTRAINT 3 -- the sweep fires in finally even when the soak RAISES.
# --------------------------------------------------------------------------- #
def test_sweep_fires_on_exception(L, monkeypatch):
    cur = L.current_requirements_sha(L.default_requirements_path())
    fake = FakeRun(
        image_sha=cur,
        list_instances=["jarvis-soak-bake-x", "sovereign-sandbox-y"],
    )
    monkeypatch.setattr(L, "_run", fake)

    def boom(cfg, env):
        raise RuntimeError("catastrophic crash mid-soak")

    monkeypatch.setattr(L, "run_soak", boom)

    # main must still tear down (it may re-raise or return non-zero, but the
    # sweep MUST have run).
    try:
        L.main(["--i-understand-this-spends-money"])
    except RuntimeError:
        pass
    deleted = " ".join(" ".join(d) for d in fake.deletes())
    assert "jarvis-soak-bake-x" in deleted
    assert "sovereign-sandbox-y" in deleted


# --------------------------------------------------------------------------- #
# CONSTRAINT 3 -- the sweep fires on SIGTERM (signal handler -> sweep).
# --------------------------------------------------------------------------- #
def test_sweep_fires_on_sigterm(L, monkeypatch):
    fake = FakeRun(
        list_instances=["jarvis-soak-bake-sig", "sovereign-sandbox-sig"],
    )
    monkeypatch.setattr(L, "_run", fake)

    swept = {"n": 0}
    real_sweep = L.anti_zombie_sweep

    def counting_sweep(cfg):
        swept["n"] += 1
        return real_sweep(cfg)

    monkeypatch.setattr(L, "anti_zombie_sweep", counting_sweep)

    cfg = L.build_config()
    # Simulate the signal handler firing.
    with pytest.raises(SystemExit):
        L._signal_handler(cfg)(15, None)
    assert swept["n"] >= 1
    deleted = " ".join(" ".join(d) for d in fake.deletes())
    assert "jarvis-soak-bake-sig" in deleted
    assert "sovereign-sandbox-sig" in deleted


# --------------------------------------------------------------------------- #
# CONSTRAINT 3 -- the sweep NEVER raises even when delete fails.
# --------------------------------------------------------------------------- #
def test_sweep_never_raises_on_delete_failure(L, monkeypatch):
    fake = FakeRun(
        list_instances=["jarvis-soak-bake-fail", "sovereign-sandbox-fail"],
        delete_rc=1,  # every delete fails
    )
    monkeypatch.setattr(L, "_run", fake)
    # Must NOT raise.
    L.anti_zombie_sweep(L.build_config())
    assert len(fake.deletes()) >= 2


def test_sweep_never_raises_when_run_itself_throws(L, monkeypatch):
    def explode(cmd, timeout_s=120.0):
        raise OSError("gcloud not found")

    monkeypatch.setattr(L, "_run", explode)
    # Must swallow everything.
    L.anti_zombie_sweep(L.build_config())


# --------------------------------------------------------------------------- #
# Money gate -- refuse a real run without the safety flag.
# --------------------------------------------------------------------------- #
def test_refuses_without_money_gate(L, monkeypatch):
    monkeypatch.setattr(L, "run_soak", lambda cfg, env: 0)
    monkeypatch.setattr(L, "anti_zombie_sweep", lambda cfg: None)
    rc = L.main([])  # no --i-understand-this-spends-money
    assert rc != 0


# --------------------------------------------------------------------------- #
# --help / --dry-run smoke (no GCP).
# --------------------------------------------------------------------------- #
def test_dry_run_does_not_touch_gcloud(L, monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(L, "_run", lambda *a, **k: called.__setitem__("n", called["n"] + 1) or (0, ""))
    monkeypatch.setattr(L, "bake_golden", lambda *a, **k: True)
    soak = {"n": 0}
    monkeypatch.setattr(L, "run_soak", lambda *a, **k: soak.__setitem__("n", soak["n"] + 1) or 0)
    rc = L.main(["--dry-run"])
    assert rc == 0
    assert soak["n"] == 0, "dry-run must not invoke the real soak"


# --------------------------------------------------------------------------- #
# deploy/ouroboros_omni_prod.env now exports the omni flags.
# --------------------------------------------------------------------------- #
def test_prod_env_exports_omni_flags():
    env_text = (_REPO_ROOT / "deploy" / "ouroboros_omni_prod.env").read_text()
    assert "JARVIS_META_GOAL_AGGREGATOR_ENABLED" in env_text
    assert "JARVIS_IAC_SOAK_GOLDEN_ENABLED" in env_text
    assert "JARVIS_A1_OMNI_SOAK" in env_text
    assert "JARVIS_IAC_FAULT_TOLERANT_OBS_ENABLED" in env_text
