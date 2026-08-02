"""`flock` excludes processes. It does not exclude hosts.

Two machines sharing a checkout over NFS each take the lock successfully, each
believe they hold it, and both proceed. Nothing errors. Nothing times out. The
critical section simply is not one.

This module does not implement a distributed lock — `core.distributed_lock_
manager` (v3.0: Redis backend, fencing tokens, lease keepalive) already does,
and `cross_process_jsonl` already owns the file lock. It answers the prior
question: *which of those is worth asking here*, and says so when the answer is
"less than you think".

THE DESIGN DECISION WORTH DEFENDING
-------------------------------------
`UNVERIFIED` is a distinct value from `HOST_LOCAL`, and neither warns. Most
installs are a laptop. A module that cried "possible split-brain!" every time
it could not read a mount table would train its operator to ignore the one
occasion it mattered. Silence and verified-safe are different facts, so they
are different values — the same rule that keeps `waived()` distinct from an
omitted argument, and `UNKNOWN` distinct from `UNSET`.

Only `UNSAFE` — a filesystem we VERIFIED is shared, with no distributed
backend — is worth a warning, because it is the one case where no error will
ever surface the exposure. Both hosts succeed.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.ouroboros.governance import coordination_substrate as cs
from backend.core.ouroboros.governance.coordination_substrate import Guarantee


@pytest.fixture(autouse=True)
def _clean_cache():
    cs.reset_cache()
    yield
    cs.reset_cache()


def _mount(monkeypatch, fstype: str, mountpoint: str = "/"):
    """Pretend the world is mounted a particular way."""
    monkeypatch.setattr(cs, "_mount_for", lambda p: (mountpoint, fstype))


class TestReadingTheSubstrate:
    def test_a_local_filesystem_is_host_local(self, monkeypatch):
        _mount(monkeypatch, "apfs")
        assert probe_g(monkeypatch) is Guarantee.HOST_LOCAL

    @pytest.mark.parametrize("fstype", [
        "nfs", "nfs4", "cifs", "smbfs", "glusterfs", "cephfs", "lustre",
        "fuse.sshfs", "9p", "virtiofs",
    ])
    def test_shared_filesystems_are_unsafe_without_a_backend(
            self, monkeypatch, fstype):
        _mount(monkeypatch, fstype)
        monkeypatch.setattr(cs, "_distributed_reachable", lambda: False)
        assert probe_g(monkeypatch) is Guarantee.UNSAFE

    def test_a_shared_filesystem_with_a_backend_is_cross_host(self, monkeypatch):
        _mount(monkeypatch, "nfs4")
        monkeypatch.setattr(cs, "_distributed_reachable", lambda: True)
        assert probe_g(monkeypatch) is Guarantee.CROSS_HOST

    def test_an_unreadable_mount_table_is_UNVERIFIED_not_unsafe(self, monkeypatch):
        """The distinction the whole module turns on. `UNVERIFIED` is the
        ordinary laptop case where flock IS correct and we simply could not
        read `/proc/mounts`. Calling it unsafe would cry wolf everywhere and
        make the real signal worthless."""
        monkeypatch.setattr(cs, "_mount_for", lambda p: ("", ""))
        r = cs.probe(Path("/anywhere"))
        assert r.guarantee is Guarantee.UNVERIFIED
        assert r.guarantee is not Guarantee.UNSAFE

    def test_unverified_is_still_sufficient_to_proceed(self):
        assert Guarantee.UNVERIFIED.is_sufficient
        assert Guarantee.HOST_LOCAL.is_sufficient
        assert Guarantee.CROSS_HOST.is_sufficient
        assert not Guarantee.UNSAFE.is_sufficient

    def test_the_real_journal_path_reads_cleanly(self):
        """Whatever this machine is, the probe must return a usable answer."""
        r = cs.probe(Path(".jarvis") / "cu_failure_journal.jsonl")
        assert isinstance(r.guarantee, Guarantee)
        assert r.schema_version == cs.COORDINATION_SCHEMA_VERSION


def probe_g(monkeypatch) -> Guarantee:
    return cs.probe(Path("/tmp/x/journal.jsonl")).guarantee


class TestItNeverRaises:
    def test_a_nonexistent_path_walks_up_to_a_real_ancestor(self):
        """The journal may not exist yet, and a path that does not exist has no
        mount of its own."""
        r = cs.probe(Path("/tmp/does/not/exist/anywhere/journal.jsonl"))
        assert isinstance(r.guarantee, Guarantee)

    def test_psutil_exploding_degrades_to_unverified(self, monkeypatch):
        import psutil
        monkeypatch.setattr(psutil, "disk_partitions",
                            lambda **kw: (_ for _ in ()).throw(OSError("nope")))
        assert cs.probe(Path("/tmp/x")).guarantee is Guarantee.UNVERIFIED

    def test_a_hostile_probe_still_yields_a_reading(self, monkeypatch):
        monkeypatch.setattr(cs, "_mount_for",
                            lambda p: (_ for _ in ()).throw(RuntimeError()))
        assert cs.probe(Path("/tmp/x")).guarantee is Guarantee.UNVERIFIED

    def test_a_broken_backend_check_does_not_claim_cross_host(self, monkeypatch):
        """Failing to prove a distributed backend must never READ as one."""
        _mount(monkeypatch, "nfs")
        monkeypatch.setattr(cs, "_distributed_reachable",
                            lambda: (_ for _ in ()).throw(RuntimeError()))
        assert cs.probe(Path("/tmp/x")).guarantee is Guarantee.UNVERIFIED


class TestConfigurability:
    def test_the_shared_set_is_extended_never_replaced(self, monkeypatch):
        """A site with an exotic clustered filesystem should not need a code
        change — and must not be able to blank the built-ins by setting it."""
        monkeypatch.setenv("JARVIS_COORDINATION_SHARED_FSTYPES", "myclusterfs")
        types = cs.shared_fstypes()
        assert "myclusterfs" in types
        assert "nfs4" in types, "extending the set dropped the built-ins"

    def test_a_malformed_env_falls_back(self, monkeypatch):
        monkeypatch.setenv("JARVIS_COORDINATION_SHARED_FSTYPES", ",,,  ,")
        assert "nfs" in cs.shared_fstypes()

    def test_a_custom_type_then_reads_as_unsafe(self, monkeypatch):
        monkeypatch.setenv("JARVIS_COORDINATION_SHARED_FSTYPES", "weirdfs")
        _mount(monkeypatch, "weirdfs")
        monkeypatch.setattr(cs, "_distributed_reachable", lambda: False)
        assert probe_g(monkeypatch) is Guarantee.UNSAFE


class TestTheWarningIsScarce:
    def test_unsafe_warns_exactly_once_per_path(self, monkeypatch, caplog):
        import logging
        _mount(monkeypatch, "nfs")
        monkeypatch.setattr(cs, "_distributed_reachable", lambda: False)
        with caplog.at_level(logging.WARNING):
            for _ in range(5):
                cs.cached_probe(Path("/mnt/share/journal.jsonl"))
        warns = [r for r in caplog.records if "host-local lock" in r.getMessage()]
        assert len(warns) == 1, f"warned {len(warns)}x — that is how a signal dies"

    def test_the_ordinary_cases_are_silent(self, monkeypatch, caplog):
        import logging
        with caplog.at_level(logging.WARNING):
            _mount(monkeypatch, "apfs")
            cs.cached_probe(Path("/a/journal.jsonl"))
            monkeypatch.setattr(cs, "_mount_for", lambda p: ("", ""))
            cs.cached_probe(Path("/b/journal.jsonl"))
        assert not [r for r in caplog.records if "host-local lock" in r.getMessage()]

    def test_the_probe_is_cached(self, monkeypatch):
        calls = {"n": 0}

        def _counting(p):
            calls["n"] += 1
            return ("/", "apfs")
        monkeypatch.setattr(cs, "_mount_for", _counting)
        for _ in range(4):
            cs.cached_probe(Path("/same/journal.jsonl"))
        assert calls["n"] == 1, "disk_partitions() on every critical section"


class TestProvenanceTravelsWithTheDecision:
    def test_the_reading_is_splattable_evidence(self, monkeypatch):
        _mount(monkeypatch, "nfs")
        monkeypatch.setattr(cs, "_distributed_reachable", lambda: False)
        ev = cs.probe(Path("/tmp/x")).as_evidence()
        assert ev["coordination"] == "unsafe"
        assert ev["coordination_fstype"] == "nfs"
        assert ev["coordination_verified"] is True

    def test_unverified_says_so_rather_than_claiming_local(self, monkeypatch):
        """A duplicate envelope produced under a shared mount must be
        explicable after the fact, not a mystery."""
        monkeypatch.setattr(cs, "_mount_for", lambda p: ("", ""))
        ev = cs.probe(Path("/tmp/x")).as_evidence()
        assert ev["coordination"] == "unverified"
        assert ev["coordination_verified"] is False

    @pytest.mark.asyncio
    async def test_exclusive_yields_the_reading_even_when_not_acquired(
            self, monkeypatch):
        """Host-local paths do not take a distributed lock — the caller's own
        flock stays authoritative — but the reading still travels."""
        _mount(monkeypatch, "apfs")
        async with cs.exclusive(Path("/tmp/x"), "k") as (acquired, reading):
            assert acquired is False
            assert reading.guarantee is Guarantee.HOST_LOCAL

    @pytest.mark.asyncio
    async def test_exclusive_never_raises_on_a_broken_backend(self, monkeypatch):
        _mount(monkeypatch, "nfs4")
        monkeypatch.setattr(cs, "_distributed_reachable", lambda: True)
        async with cs.exclusive(Path("/tmp/x"), "k") as (acquired, reading):
            assert acquired in (True, False)
            assert isinstance(reading.guarantee, Guarantee)


class TestTheSensorStampsIt:
    @pytest.mark.asyncio
    async def test_an_envelope_carries_the_coordination_provenance(
            self, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_CU_JOURNAL_PATH", str(tmp_path / "j.jsonl"))
        from backend.core.ouroboros.governance.intake.sensors import (
            cu_execution_sensor as ces,
        )
        ces.CUExecutionSensor._instance = None

        class _R:
            def __init__(self):
                self.got = []

            async def ingest(self, e):
                self.got.append(e)
                return "ok"

        s = ces.CUExecutionSensor(router=_R())
        for _ in range(ces._GRADUATION_THRESHOLD):
            await s.record(ces.CUExecutionRecord(
                goal="message alice saying hi", success=False,
                steps_completed=1, steps_total=3, elapsed_s=1.0,
                error="target not found", is_messaging=True,
                contact="alice", app="Messages"))
        ces.CUExecutionSensor._instance = None
        assert s._router.got, "no envelope emitted"
        ev = s._router.got[0]
        assert "coordination" in ev.evidence
        assert ev.evidence["coordination"] in {g.value for g in Guarantee}
