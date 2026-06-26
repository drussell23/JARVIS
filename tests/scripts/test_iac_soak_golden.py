# -*- coding: utf-8 -*-
"""Soak golden-image wiring tests for scripts/sovereign_iac_hypervisor.py.

No real GCP/SSH. Verifies (a) node-create uses jarvis-soak-golden when enabled +
present, debian-12 otherwise (byte-identical OFF); (b) the surgery SKIPS pip when
deps present + sha matches; (c) DELTA-ensures on a stale sha; (d) the
INDESTRUCTIBLE fallback: golden-unverified-within-timeout -> loud warn + full
pip path runs; (e) reuses the J-Prime baker shape + the existing deps path
(no dup -- the full-pip body is embedded verbatim in the golden body).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "sovereign_iac_hypervisor.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("sovereign_iac_hypervisor", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def iac():
    return _load_module()


@pytest.fixture(autouse=True)
def _gate_off_by_default(monkeypatch):
    """Default the master gate OFF so each test arms it explicitly."""
    monkeypatch.delenv("JARVIS_IAC_SOAK_GOLDEN_ENABLED", raising=False)


@pytest.fixture()
def args(iac):
    return iac.build_parser().parse_args([])


# --------------------------------------------------------------------------- #
# (e) reuse: hard-ensure list is a single source of truth, exported.
# --------------------------------------------------------------------------- #
def test_hard_ensure_deps_exported(iac):
    deps = iac.hard_ensure_deps()
    assert "uuid6" in deps and "pytest-asyncio" in deps and "aiohttp" in deps


# --------------------------------------------------------------------------- #
# OFF path: node-create is byte-identical debian-12 (gate off).
# --------------------------------------------------------------------------- #
def test_node_create_off_uses_debian12(iac, args, monkeypatch):
    # Gate OFF (default). golden_image_status must NEVER be consulted.
    called = {"status": False}

    def _boom(*a, **k):
        called["status"] = True
        return (True, "abc")

    monkeypatch.setattr(iac, "golden_image_status", _boom)
    args.soak_golden = False
    cmd = iac._create_node_cmd(args, "node-x", "/tmp/su.sh")
    joined = " ".join(cmd)
    assert "--image-family=debian-12" in joined
    assert "--image-project=debian-cloud" in joined
    assert "jarvis-soak-golden" not in joined
    assert called["status"] is False  # never probed when gate off


# --------------------------------------------------------------------------- #
# (a) ON + image present -> node-create uses jarvis-soak-golden in this project.
# --------------------------------------------------------------------------- #
def test_node_create_golden_present_uses_golden(iac, args, monkeypatch):
    monkeypatch.setattr(iac, "golden_image_status", lambda a: (True, "deadbeef"))
    args.soak_golden = True
    cmd = iac._create_node_cmd(args, "node-x", "/tmp/su.sh")
    joined = " ".join(cmd)
    assert "--image-family=jarvis-soak-golden" in joined
    # Golden image lives in THIS project, not debian-cloud.
    assert f"--image-project={args.project}" in joined
    assert "debian-cloud" not in joined


# --------------------------------------------------------------------------- #
# (a/d) ON but image ABSENT -> indestructible: falls back to debian-12.
# --------------------------------------------------------------------------- #
def test_node_create_golden_absent_falls_back_to_debian12(iac, args, monkeypatch):
    monkeypatch.setattr(iac, "golden_image_status", lambda a: (False, None))
    args.soak_golden = True
    cmd = iac._create_node_cmd(args, "node-x", "/tmp/su.sh")
    joined = " ".join(cmd)
    assert "--image-family=debian-12" in joined
    assert "jarvis-soak-golden" not in joined


def test_node_create_golden_probe_raises_falls_back(iac, args, monkeypatch):
    """A describe exception must NOT crash node-create -> debian-12 fallback."""
    def _raise(a):
        raise RuntimeError("gcloud blew up")

    monkeypatch.setattr(iac, "golden_image_status", _raise)
    args.soak_golden = True
    cmd = iac._create_node_cmd(args, "node-x", "/tmp/su.sh")
    assert "--image-family=debian-12" in " ".join(cmd)


# --------------------------------------------------------------------------- #
# golden_image_status: parses the req-sha label, fail-soft on describe error.
# --------------------------------------------------------------------------- #
def test_golden_image_status_reads_label(iac, args, monkeypatch):
    def _fake_run(cmd, *, timeout_s=120.0):
        assert "describe-from-family" in " ".join(cmd)
        return (0, "cafef00d\n")

    monkeypatch.setattr(iac, "_run", _fake_run)
    exists, label = iac.golden_image_status(args)
    assert exists is True
    assert label == "cafef00d"


def test_golden_image_status_failsoft(iac, args, monkeypatch):
    monkeypatch.setattr(iac, "_run", lambda c, **k: (1, "NOT_FOUND"))
    exists, label = iac.golden_image_status(args)
    assert exists is False and label is None


# --------------------------------------------------------------------------- #
# (OFF) deps step byte-identical to the legacy full-pip body.
# --------------------------------------------------------------------------- #
def test_deps_step_off_is_legacy_full_pip(iac, args):
    args.soak_golden = False
    step = iac._surgery_dep_step(args)
    assert step == iac._surgery_dep_install()  # byte-identical
    assert "skipping install" not in step  # no golden logic on the OFF path


# --------------------------------------------------------------------------- #
# (b) ON: deps present + sha matches -> SKIP pip (no install command in skip arm).
# --------------------------------------------------------------------------- #
def test_deps_step_golden_skips_pip(iac, args, tmp_path):
    args.soak_golden = True
    step = iac._surgery_dep_step(args)
    # Probes pre-installed deps.
    assert "probing pre-installed deps" in step
    assert "import aiohttp, uuid6, fastapi, pydantic, pytest_asyncio" in step
    # The skip path logs the mandated line.
    assert "golden image -- deps present, skipping install" in step
    # Has a verify timeout on the probe (CONSTRAINT 3 budget).
    assert "timeout " in step


# --------------------------------------------------------------------------- #
# (c) ON: stale sha -> DELTA-ensure (hard-ensure core re-installed), not full -r.
# --------------------------------------------------------------------------- #
def test_deps_step_golden_delta_ensures_on_stale(iac, args):
    args.soak_golden = True
    step = iac._surgery_dep_step(args)
    assert "STALE" in step
    assert "delta-ensuring core deps" in step
    # The delta arm pip-installs ONLY the hard-ensure core (not requirements -r).
    for core in ("aiohttp", "uuid6", "pytest-asyncio"):
        assert core in step


# --------------------------------------------------------------------------- #
# (d) THE INDESTRUCTIBLE FALLBACK: probe fail -> loud warn + FULL pip path.
# --------------------------------------------------------------------------- #
def test_deps_step_golden_fallback_runs_full_pip(iac, args):
    args.soak_golden = True
    step = iac._surgery_dep_step(args)
    # The loud, mandated fallback warning.
    assert "golden image unavailable/unverified -- FALLING BACK to raw Debian" in step
    # The FULL legacy pip body is embedded VERBATIM (reuse, no dup) in the fallback.
    full = iac._surgery_dep_install()
    # A distinctive fragment of the full-pip body must appear in the golden body.
    assert "installing jarvis host deps" in step
    assert "skip unbuildable" in step
    # And the embedded body is exactly the legacy one (reuse, not a reimpl).
    assert full in step


# --------------------------------------------------------------------------- #
# staleness sha helper (matches the baker's algorithm).
# --------------------------------------------------------------------------- #
def test_requirements_sha_matches_baker(iac, tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("aiohttp\nuuid6\n")
    sha = iac.requirements_sha(str(req))
    assert len(sha) == 16
    # Same algorithm as the baker.
    bake_path = Path(__file__).resolve().parents[2] / "scripts" / "bake_soak_golden_image.py"
    spec = importlib.util.spec_from_file_location("_bake_cmp", str(bake_path))
    bake = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bake)
    assert sha == bake.requirements_sha(str(req))


def test_requirements_sha_missing_failsoft(iac, tmp_path):
    assert iac.requirements_sha(str(tmp_path / "nope.txt")) == "norequirements"


# --------------------------------------------------------------------------- #
# Surgery body integration: the body uses the golden step when armed.
# --------------------------------------------------------------------------- #
def test_surgery_body_uses_golden_step_when_armed(iac, args):
    args.soak_golden = True
    body = iac._remote_surgery_body_script(args)
    assert "probing pre-installed deps" in body
    assert "FALLING BACK to raw Debian" in body


def test_surgery_body_legacy_when_gate_off(iac, args):
    args.soak_golden = False
    body = iac._remote_surgery_body_script(args)
    assert "probing pre-installed deps" not in body
    # The legacy full-pip body is present.
    assert "installing jarvis host deps" in body
