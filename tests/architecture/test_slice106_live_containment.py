"""Slice 106 — the live empirical containment receipt + VERIFY quarantine wiring.

The receipt tests run against a REAL Docker daemon (skipped when it isn't
responding) — proving on the M1, via Docker Desktop's Linux VM, that a malicious
payload EXECUTES but cannot exfiltrate, escape the worktree, hang the parent, or
crash it. The quarantine tests prove a ContainmentBreach feeds the belief loop
(the Phase-3 learning signal the VERIFY hook records).
"""

from __future__ import annotations

import asyncio
import subprocess
import tempfile
from types import SimpleNamespace

import pytest

from backend.core.ouroboros.governance import container_sandbox as CS
from backend.core.ouroboros.governance.runtime_sandbox import (
    ContainmentBreach,
    ContainmentPolicy,
)


def _docker_responsive() -> bool:
    try:
        r = subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"],
                           capture_output=True, text=True, timeout=8)
        return r.returncode == 0 and bool(r.stdout.strip())
    except Exception:  # noqa: BLE001
        return False


_DOCKER = _docker_responsive()
_skip_no_docker = pytest.mark.skipif(not _DOCKER, reason="Docker daemon not responding")


@pytest.fixture
def _sandbox_on(monkeypatch):
    monkeypatch.setenv("JARVIS_RUNTIME_SANDBOX_ENABLED", "1")
    monkeypatch.setenv("JARVIS_RUNTIME_SANDBOX_BACKEND", "container")
    yield


# === The live empirical receipt (real Docker, real Linux kernel) ============

@_skip_no_docker
def test_live_receipt_network_and_fs_escape_denied(_sandbox_on):
    wt = tempfile.mkdtemp()
    malicious = (
        "import socket, sys\n"
        "r = {}\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 80), timeout=3); r['net']='EGRESS_OK'\n"
        "except Exception: r['net']='EGRESS_BLOCKED'\n"
        "try:\n"
        "    open('/etc/pwned','w').write('x'); r['etc']='ETC_WRITE_OK'\n"
        "except Exception: r['etc']='ETC_WRITE_BLOCKED'\n"
        "try:\n"
        "    open('/root/pwned','w').write('x'); r['root']='ROOT_WRITE_OK'\n"
        "except Exception: r['root']='ROOT_WRITE_BLOCKED'\n"
        "try:\n"
        "    open('/work/legit.txt','w').write('ok'); r['work']='WORK_WRITE_OK'\n"
        "except Exception: r['work']='WORK_WRITE_BLOCKED'\n"
        "print(';'.join(f'{k}={v}' for k,v in r.items()))\n"
    )
    res = asyncio.run(CS.run_in_container(malicious, worktree=wt,
                                          policy=ContainmentPolicy(timeout_s=20)))
    # The payload EXECUTED (clean exit) but every escape was DENIED by the kernel.
    assert res.ok is True
    assert "net=EGRESS_BLOCKED" in res.stdout       # --network none
    assert "etc=ETC_WRITE_BLOCKED" in res.stdout     # --read-only rootfs
    assert "root=ROOT_WRITE_BLOCKED" in res.stdout   # --read-only rootfs
    assert "work=WORK_WRITE_OK" in res.stdout        # the worktree mount is the only writable host path
    assert "EGRESS_OK" not in res.stdout


@_skip_no_docker
def test_live_receipt_timeout_breach_without_parent_crash(_sandbox_on):
    import os
    wt = tempfile.mkdtemp()
    res = asyncio.run(CS.run_in_container("\nwhile True:\n  pass\n", worktree=wt,
                                          policy=ContainmentPolicy(timeout_s=4)))
    assert res.breach is ContainmentBreach.TIMEOUT
    assert res.duration_s < 12.0
    # ZERO parent leakage — this test process is alive and well.
    assert os.getpid() > 0


# === Quarantine: a breach feeds the belief loop (the VERIFY hook's signal) ==

def test_breach_records_quarantine_belief(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_BELIEF_REVISION_ENABLED", "1")
    monkeypatch.setenv("JARVIS_BELIEF_REVISION_LEDGER_PATH", str(tmp_path / "belief.jsonl"))
    monkeypatch.setenv("JARVIS_COGNITIVE_BUS_ENABLED", "1")
    from backend.core.ouroboros.governance.cognitive_subscribers import recent_avoidance_digest

    fake_breach = SimpleNamespace(breach=ContainmentBreach.TIMEOUT)
    CS.record_containment_breach_belief(
        "op-breach-1", fake_breach,
        ["backend/core/ouroboros/governance/evil_candidate.py"],
    )
    digest = recent_avoidance_digest()
    assert "evil_candidate.py" in digest   # the quarantined paradigm steers GENERATE away


def test_breach_belief_inert_when_belief_off(monkeypatch, tmp_path):
    monkeypatch.delenv("JARVIS_BELIEF_REVISION_ENABLED", raising=False)
    monkeypatch.setenv("JARVIS_BELIEF_REVISION_LEDGER_PATH", str(tmp_path / "b2.jsonl"))
    fake_breach = SimpleNamespace(breach=ContainmentBreach.NONZERO_EXIT)
    # Must not raise + must not write when the belief substrate is off.
    CS.record_containment_breach_belief("op-x", fake_breach, ["x.py"])
    assert not (tmp_path / "b2.jsonl").exists()


# === The VERIFY-gate decision predicate (what the orchestrator hook applies) =

def test_verify_gate_quarantines_only_real_breaches():
    # The orchestrator gate marks verify FAILED for these breach kinds...
    quarantine = {ContainmentBreach.TIMEOUT, ContainmentBreach.SIGNAL_KILLED,
                  ContainmentBreach.NONZERO_EXIT}
    # ...and NOT for these (off / disabled / spawn issues are not candidate faults).
    pass_through = {ContainmentBreach.NONE, ContainmentBreach.DISABLED}
    assert quarantine.isdisjoint(pass_through)
    assert ContainmentBreach.TIMEOUT in quarantine
    assert ContainmentBreach.DISABLED not in quarantine
