from __future__ import annotations

"""Tests for scripts/gcp_cleanup.py -- brain-VM orphan-filter fix (A1 mandate 1).

Bug: `_find_orphaned_resources` filtered GCE instances with
`labels.created-by=jarvis OR labels.app=jarvis`. GCP Brain VMs are labelled
`jarvis-role=brain` ONLY (see `gcp_compute_rest.py::_BRAIN_ROLE_LABEL_KEY/_VALUE`),
so an orphaned Brain VM (e.g. left behind by a SIGKILL'd ignition driver) was
INVISIBLE to `gcp_cleanup.py` -- a real $0-teardown gap.

Fix: extract the filter string into a pure `_orphan_instance_filter()` helper
that also matches `labels.jarvis-role=brain`, and use it in
`_find_orphaned_resources` instead of the inline literal.

Second bug (whole-branch review, destructive collateral): once brain VMs
became visible to this filter, `cleanup_orphaned_vms()` deleted EVERY match
unconditionally -- including a LIVE, RUNNING Stage-2/3/4 Brain organism.
Fix: `_should_reap_orphan_vm()` guards brain matches to `status ==
"TERMINATED"` only; `created-by=jarvis`/`app=jarvis`-only matches keep the
existing unconditional-reap behavior.

Re-review fix #1 (safety-critical): the guard's original age-based branch
(RUNNING past `JARVIS_BRAIN_ORPHAN_MAX_AGE_S`, default 2h) reaped a LIVE
production Brain -- a persistent Stage-2/3/4 organism is always older than
2h. That branch has been removed entirely: a brain-labelled VM is reaped
ONLY when already `TERMINATED`, regardless of age.

Re-review fix #2 (safety-critical): `_vm_label_match_class` used to check
`created-by=jarvis`/`app=jarvis` BEFORE `jarvis-role=brain`, so a VM
carrying BOTH labels classified as unconditional-reap `"jarvis"`, bypassing
the brain guard entirely. The brain label now wins any such ambiguity.

Zero real gcloud calls -- subprocess.run is monkeypatched.
"""

import importlib.util
import json
import pathlib
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest

# --------------------------------------------------------------------------- #
# Load the module under test via importlib so this works even though
# scripts/ is not a package (mirrors tests/scripts/test_a1_orphan_reaper.py).
# --------------------------------------------------------------------------- #
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_CLEANUP_PATH = _REPO_ROOT / "scripts" / "gcp_cleanup.py"


def _load_cleanup():
    spec = importlib.util.spec_from_file_location("gcp_cleanup", str(_CLEANUP_PATH))
    assert spec and spec.loader, f"could not load spec from {_CLEANUP_PATH}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gcp_cleanup"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def C():
    """Module-scoped fixture: load gcp_cleanup.py once per test session."""
    return _load_cleanup()


def test_orphan_instance_filter_contains_all_three_predicates(C):
    """The filter helper must OR-join all three orphan label predicates,
    including the brain-VM one that was previously missing."""
    filt = C._orphan_instance_filter()

    assert isinstance(filt, str)
    assert "labels.created-by=jarvis" in filt
    assert "labels.app=jarvis" in filt
    assert "labels.jarvis-role=brain" in filt

    # Well-formed OR-joined gcloud filter: exactly 3 predicates, 2 " OR " joins.
    parts = filt.split(" OR ")
    assert len(parts) == 3
    assert set(parts) == {
        "labels.created-by=jarvis",
        "labels.app=jarvis",
        "labels.jarvis-role=brain",
    }


@pytest.mark.asyncio
async def test_find_orphaned_resources_passes_brain_role_filter(C, monkeypatch):
    """`_find_orphaned_resources` must build its --filter= argv from the
    canonical helper (so a brain-labelled orphan VM is no longer invisible)."""
    captured: List[List[str]] = []

    class _FakeCompletedProcess:
        def __init__(self) -> None:
            self.returncode = 0
            self.stdout = ""
            self.stderr = ""

    def _fake_run(cmd: List[str], **kwargs: Any) -> _FakeCompletedProcess:
        captured.append(cmd)
        return _FakeCompletedProcess()

    monkeypatch.setattr(C.subprocess, "run", _fake_run)

    cleanup = C.GCPCleanup(config=C.GCPConfig(project_id="fake-project"))
    result = await cleanup._find_orphaned_resources()

    assert result == []
    assert len(captured) == 1

    argv = captured[0]
    filter_args = [a for a in argv if a.startswith("--filter=")]
    assert len(filter_args) == 1
    assert filter_args[0] == f"--filter={C._orphan_instance_filter()}"
    assert "labels.jarvis-role=brain" in filter_args[0]

    # Labels must be requested too, so the reap guard can classify the match.
    format_args = [a for a in argv if a.startswith("--format=")]
    assert len(format_args) == 1
    assert "labels" in format_args[0]


# ===========================================================================
# Brain-VM reap guard (destructive-collateral fix): a brain-labelled VM must
# be reaped ONLY when genuinely orphaned (TERMINATED, or RUNNING past the
# max-age threshold). `created-by=jarvis`/`app=jarvis` VMs are unconditional,
# unchanged.
# ===========================================================================


class _FakeCompletedProcess:
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _recent_timestamp() -> str:
    return _iso(datetime.now(timezone.utc) - timedelta(seconds=30))


def _aged_timestamp(age_s: int) -> str:
    return _iso(datetime.now(timezone.utc) - timedelta(seconds=age_s))


@pytest.mark.asyncio
async def test_running_brain_only_vm_is_not_deleted(C, monkeypatch):
    """A RUNNING brain-only VM (no created-by/app label) must NOT be
    selected for deletion -- it may be a live Brain organism."""
    vm = {
        "name": "brain-live",
        "zone": "projects/p/zones/us-central1-a",
        "status": "RUNNING",
        "creationTimestamp": _recent_timestamp(),
        "labels": {"jarvis-role": "brain"},
    }
    calls: List[List[str]] = []

    def _fake_run(cmd: List[str], **kwargs: Any) -> _FakeCompletedProcess:
        calls.append(cmd)
        if "list" in cmd:
            return _FakeCompletedProcess(stdout=json.dumps([vm]))
        return _FakeCompletedProcess()

    monkeypatch.setattr(C.subprocess, "run", _fake_run)
    cleanup = C.GCPCleanup(config=C.GCPConfig(project_id="fake-project"))

    result = await cleanup.cleanup_orphaned_vms()

    delete_calls = [c for c in calls if "delete" in c]
    assert delete_calls == []
    assert result["deleted"] == []
    assert "brain-live" in result["skipped"]


@pytest.mark.asyncio
async def test_terminated_brain_only_vm_is_deleted(C, monkeypatch):
    """A TERMINATED brain-only VM IS reaped -- this is exactly the
    composition with Piece D's dead-man (`shutdown -h now` -> TERMINATED)
    finishing the job."""
    vm = {
        "name": "brain-halted",
        "zone": "projects/p/zones/us-central1-a",
        "status": "TERMINATED",
        "creationTimestamp": _recent_timestamp(),
        "labels": {"jarvis-role": "brain"},
    }
    calls: List[List[str]] = []

    def _fake_run(cmd: List[str], **kwargs: Any) -> _FakeCompletedProcess:
        calls.append(cmd)
        if "list" in cmd:
            return _FakeCompletedProcess(stdout=json.dumps([vm]))
        return _FakeCompletedProcess()

    monkeypatch.setattr(C.subprocess, "run", _fake_run)
    cleanup = C.GCPCleanup(config=C.GCPConfig(project_id="fake-project"))

    result = await cleanup.cleanup_orphaned_vms()

    delete_calls = [c for c in calls if "delete" in c]
    assert len(delete_calls) == 1
    assert "brain-halted" in delete_calls[0]
    assert result["deleted"] == ["brain-halted"]
    assert result["skipped"] == []


@pytest.mark.asyncio
async def test_aged_running_brain_only_vm_is_not_deleted(C, monkeypatch):
    """SAFETY-CRITICAL (re-review): a RUNNING brain-only VM must NOT be
    reaped no matter how old it is -- a real production Brain is a
    persistent organism that runs for days/weeks, so any age-based RUNNING
    reap eventually deletes a LIVE production Brain. The age branch has
    been removed entirely; only ``status == "TERMINATED"`` triggers reap."""
    vm = {
        "name": "brain-old-but-alive",
        "zone": "projects/p/zones/us-central1-a",
        "status": "RUNNING",
        "creationTimestamp": _aged_timestamp(20000),
        "labels": {"jarvis-role": "brain"},
    }
    calls: List[List[str]] = []

    def _fake_run(cmd: List[str], **kwargs: Any) -> _FakeCompletedProcess:
        calls.append(cmd)
        if "list" in cmd:
            return _FakeCompletedProcess(stdout=json.dumps([vm]))
        return _FakeCompletedProcess()

    monkeypatch.setattr(C.subprocess, "run", _fake_run)
    cleanup = C.GCPCleanup(config=C.GCPConfig(project_id="fake-project"))

    result = await cleanup.cleanup_orphaned_vms()

    delete_calls = [c for c in calls if "delete" in c]
    assert delete_calls == []
    assert result["deleted"] == []
    assert "brain-old-but-alive" in result["skipped"]


@pytest.mark.asyncio
async def test_brain_label_wins_when_both_labels_present(C, monkeypatch):
    """SAFETY-CRITICAL (re-review): a VM carrying BOTH
    ``created-by=jarvis`` AND ``jarvis-role=brain`` must classify as
    brain-guarded (NOT unconditional-reap `"jarvis"`) -- the brain label
    wins any ambiguity. RUNNING is skipped; TERMINATED is reaped."""
    running_vm = {
        "name": "brain-and-jarvis-running",
        "zone": "projects/p/zones/us-central1-a",
        "status": "RUNNING",
        "creationTimestamp": _recent_timestamp(),
        "labels": {"created-by": "jarvis", "jarvis-role": "brain"},
    }
    calls: List[List[str]] = []

    def _fake_run_running(cmd: List[str], **kwargs: Any) -> _FakeCompletedProcess:
        calls.append(cmd)
        if "list" in cmd:
            return _FakeCompletedProcess(stdout=json.dumps([running_vm]))
        return _FakeCompletedProcess()

    monkeypatch.setattr(C.subprocess, "run", _fake_run_running)
    cleanup = C.GCPCleanup(config=C.GCPConfig(project_id="fake-project"))

    result = await cleanup.cleanup_orphaned_vms()

    delete_calls = [c for c in calls if "delete" in c]
    assert delete_calls == []
    assert result["deleted"] == []
    assert "brain-and-jarvis-running" in result["skipped"]

    # Same VM, TERMINATED -- now reaped (brain-guard rule, not jarvis-rule).
    terminated_vm = {**running_vm, "status": "TERMINATED", "name": "brain-and-jarvis-halted"}
    calls2: List[List[str]] = []

    def _fake_run_terminated(cmd: List[str], **kwargs: Any) -> _FakeCompletedProcess:
        calls2.append(cmd)
        if "list" in cmd:
            return _FakeCompletedProcess(stdout=json.dumps([terminated_vm]))
        return _FakeCompletedProcess()

    monkeypatch.setattr(C.subprocess, "run", _fake_run_terminated)
    result2 = await cleanup.cleanup_orphaned_vms()

    delete_calls2 = [c for c in calls2 if "delete" in c]
    assert len(delete_calls2) == 1
    assert "brain-and-jarvis-halted" in delete_calls2[0]
    assert result2["deleted"] == ["brain-and-jarvis-halted"]


def test_vm_label_match_class_brain_wins_over_jarvis(C):
    """Direct unit check on the classifier: brain label wins regardless of
    label dict ordering."""
    assert C._vm_label_match_class({"created-by": "jarvis", "jarvis-role": "brain"}) == "brain"
    assert C._vm_label_match_class({"jarvis-role": "brain", "app": "jarvis"}) == "brain"
    assert C._vm_label_match_class({"jarvis-role": "brain"}) == "brain"
    assert C._vm_label_match_class({"created-by": "jarvis"}) == "jarvis"
    assert C._vm_label_match_class({"app": "jarvis"}) == "jarvis"
    assert C._vm_label_match_class({"some-other-label": "x"}) == "unknown"
    assert C._vm_label_match_class(None) == "unknown"


@pytest.mark.asyncio
async def test_created_by_jarvis_vm_deleted_regardless_of_status(C, monkeypatch):
    """`created-by=jarvis` (or `app=jarvis`) ephemeral nodes keep the
    EXISTING unconditional-reap behavior -- unaffected by the brain guard."""
    vm = {
        "name": "ephemeral-jarvis",
        "zone": "projects/p/zones/us-central1-a",
        "status": "RUNNING",
        "creationTimestamp": _recent_timestamp(),
        "labels": {"created-by": "jarvis"},
    }
    calls: List[List[str]] = []

    def _fake_run(cmd: List[str], **kwargs: Any) -> _FakeCompletedProcess:
        calls.append(cmd)
        if "list" in cmd:
            return _FakeCompletedProcess(stdout=json.dumps([vm]))
        return _FakeCompletedProcess()

    monkeypatch.setattr(C.subprocess, "run", _fake_run)
    cleanup = C.GCPCleanup(config=C.GCPConfig(project_id="fake-project"))

    result = await cleanup.cleanup_orphaned_vms()

    delete_calls = [c for c in calls if "delete" in c]
    assert len(delete_calls) == 1
    assert "ephemeral-jarvis" in delete_calls[0]
    assert result["deleted"] == ["ephemeral-jarvis"]
    assert result["skipped"] == []


def test_should_reap_orphan_vm_fails_closed_on_unknown_label_class(C):
    """A VM whose labels match neither predicate class (should not happen
    given the OR-filter) must fail CLOSED -- not reaped."""
    vm = {
        "name": "weird",
        "status": "RUNNING",
        "created": _recent_timestamp(),
        "labels": {"some-other-label": "x"},
    }
    assert C._should_reap_orphan_vm(vm) is False


def test_should_reap_orphan_vm_brain_ignores_age_entirely(C):
    """Re-review regression pin: the age-based RUNNING-reap branch (and its
    supporting helpers/env var) must be gone. A brain-only VM's reap
    decision depends ONLY on ``status``, never on age -- verified directly
    via ``_should_reap_orphan_vm`` with an arbitrarily old creation time."""
    ancient_running = {
        "name": "brain-ancient",
        "status": "RUNNING",
        "created": _aged_timestamp(365 * 24 * 3600),  # 1 year old
        "labels": {"jarvis-role": "brain"},
    }
    assert C._should_reap_orphan_vm(ancient_running) is False

    ancient_terminated = {**ancient_running, "status": "TERMINATED"}
    assert C._should_reap_orphan_vm(ancient_terminated) is True

    # The age-threshold config surface must no longer exist on the module.
    assert not hasattr(C, "_brain_orphan_max_age_s")
    assert not hasattr(C, "_vm_age_seconds")
    assert not hasattr(C, "_DEFAULT_BRAIN_ORPHAN_MAX_AGE_S")
