"""Slice 107 — Ephemeral Project Container pipeline.

Mock matrix (deterministic, no Docker) for the image provisioner + the pytest-in-
container argv/parse, plus live Docker tests (skipped when the daemon is down) that
prove the real pytest suite runs inside the hardened image and the provisioner's
hash-check + rebuild work end-to-end.
"""

from __future__ import annotations

import asyncio
import subprocess
import tempfile
import os

import pytest

from backend.core.ouroboros.governance import image_provisioner as IP
from backend.core.ouroboros.governance import container_sandbox as CS
from backend.core.ouroboros.governance.runtime_sandbox import ContainmentPolicy


def _docker_responsive() -> bool:
    try:
        r = subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"],
                           capture_output=True, text=True, timeout=8)
        return r.returncode == 0 and bool(r.stdout.strip())
    except Exception:  # noqa: BLE001
        return False


_skip_no_docker = pytest.mark.skipif(not _docker_responsive(), reason="Docker daemon not responding")


class _FakeDocker:
    def __init__(self, *responses):
        # responses: list of (rc, out, err) returned in order; last repeats.
        self._responses = list(responses) or [(0, "", "")]
        self.calls = []

    async def __call__(self, argv, timeout_s):
        self.calls.append(list(argv))
        idx = min(len(self.calls) - 1, len(self._responses) - 1)
        return self._responses[idx]


# === Provisioner: hash + present/current + rebuild (mocked Docker) ==========

def test_state_hash_is_deterministic_and_input_sensitive():
    h1 = IP.image_state_hash()
    h2 = IP.image_state_hash()
    assert h1 == h2 and h1 != "unknown" and len(h1) == 16


def test_present_and_current_via_label(monkeypatch):
    want = IP.image_state_hash()
    fake = _FakeDocker((0, want + "\n", ""))   # inspect returns the matching label
    present, current = asyncio.run(IP.image_present_and_current(docker_run=fake, expected_hash=want))
    assert present is True and current is True


def test_stale_label_is_not_current(monkeypatch):
    fake = _FakeDocker((0, "OLDHASH\n", ""))
    present, current = asyncio.run(IP.image_present_and_current(docker_run=fake, expected_hash="NEWHASH"))
    assert present is True and current is False


def test_missing_image_is_not_present(monkeypatch):
    fake = _FakeDocker((1, "", "No such image"))
    present, current = asyncio.run(IP.image_present_and_current(docker_run=fake))
    assert present is False and current is False


def test_provision_rebuilds_when_stale(monkeypatch):
    monkeypatch.setenv("JARVIS_IMAGE_PROVISIONER_ENABLED", "1")
    # 1st call = inspect (stale label) ; 2nd call = build (success)
    fake = _FakeDocker((0, "OLDHASH", ""), (0, "built", ""))
    res = asyncio.run(IP.provision_image(docker_run=fake))
    assert res.rebuilt is True and res.action == "rebuilt"
    # the build argv carried the state-hash label
    build_call = fake.calls[-1]
    assert "build" in build_call and "--label" in build_call


def test_provision_skips_when_current(monkeypatch):
    monkeypatch.setenv("JARVIS_IMAGE_PROVISIONER_ENABLED", "1")
    want = IP.image_state_hash()
    fake = _FakeDocker((0, want, ""))     # inspect → already current
    res = asyncio.run(IP.provision_image(docker_run=fake))
    assert res.rebuilt is False and res.action == "current"
    assert len(fake.calls) == 1           # never invoked build


def test_provision_rebuild_failure_is_structured(monkeypatch):
    monkeypatch.setenv("JARVIS_IMAGE_PROVISIONER_ENABLED", "1")
    fake = _FakeDocker((1, "", "no image"), (1, "", "build broke"))
    res = asyncio.run(IP.provision_image(docker_run=fake))
    assert res.action == "rebuild_failed" and res.rebuilt is False


def test_provisioner_disabled_by_default(monkeypatch):
    monkeypatch.delenv("JARVIS_IMAGE_PROVISIONER_ENABLED", raising=False)
    res = asyncio.run(IP.run_provisioner_daemon())
    assert res.action == "disabled"


# === pytest-in-container: hardened argv + general summary parse =============

def test_pytest_container_argv_is_hardened():
    argv = CS.build_pytest_container_argv(["tests/governance/test_x.py"],
                                          worktree="/tmp/wt", image="jarvis-verify-sandbox:latest")
    s = " ".join(argv)
    assert "--network none" in s and "--cap-drop ALL" in s and "--read-only" in s
    assert "-v /tmp/wt:/work:ro" in s and "-w /work" in s     # worktree READ-ONLY
    assert "--platform" not in s                              # native arch (no forced amd64)
    assert "no:cacheprovider" in s


def test_pytest_summary_parser():
    out = "..F\nFAILED test_x.py::test_c - assert\n1 failed, 2 passed in 0.06s\n"
    passed, failed, errors, total, names = CS._parse_pytest_summary(out)
    assert passed == 2 and failed == 1 and total == 3
    assert names == ("test_x.py::test_c",)


def test_run_pytest_in_container_parses_mocked_output(monkeypatch):
    monkeypatch.setenv("JARVIS_RUNTIME_SANDBOX_ENABLED", "1")
    fake = _FakeDocker((1, "1 failed, 2 passed in 0.1s\nFAILED t.py::t_c - x\n", ""))
    res = asyncio.run(CS.run_pytest_in_container(["t.py"], worktree=tempfile.mkdtemp(), docker_run=fake))
    assert res.passed == 2 and res.failed == 1 and res.total == 3 and res.ok is False


# === LIVE Docker: real pytest in the hardened image + live provisioner ======

@_skip_no_docker
def test_live_real_pytest_runs_inside_hardened_container(monkeypatch):
    monkeypatch.setenv("JARVIS_RUNTIME_SANDBOX_ENABLED", "1")
    monkeypatch.setenv("JARVIS_IMAGE_PROVISIONER_ENABLED", "1")
    # Ensure the image is present + current (rebuilds if needed).
    prov = asyncio.run(IP.provision_image())
    assert prov.current is True
    wt = tempfile.mkdtemp()
    open(os.path.join(wt, "test_live_sc.py"), "w").write(
        "def test_pass(): assert 1 + 1 == 2\n"
        "def test_pass2(): assert sorted([3,1,2]) == [1,2,3]\n"
        "def test_fail(): assert 'a' == 'b'\n"
    )
    res = asyncio.run(CS.run_pytest_in_container(
        ["test_live_sc.py"], worktree=wt, policy=ContainmentPolicy(timeout_s=60)))
    assert res.passed == 2 and res.failed == 1 and res.total == 3
    assert any("test_fail" in n for n in res.failed_names)
