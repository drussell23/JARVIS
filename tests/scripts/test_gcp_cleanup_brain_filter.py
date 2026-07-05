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
Fix: `_should_reap_orphan_vm()` guards brain-ONLY matches (TERMINATED, or
RUNNING past `JARVIS_BRAIN_ORPHAN_MAX_AGE_S`); `created-by=jarvis`/`app=jarvis`
matches keep the existing unconditional-reap behavior.

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
async def test_aged_running_brain_only_vm_is_deleted(C, monkeypatch):
    """A RUNNING brain-only VM older than JARVIS_BRAIN_ORPHAN_MAX_AGE_S IS
    reaped -- abandoned long enough it cannot plausibly be an in-flight
    soak."""
    vm = {
        "name": "brain-abandoned",
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
    assert len(delete_calls) == 1
    assert "brain-abandoned" in delete_calls[0]
    assert result["deleted"] == ["brain-abandoned"]


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


def test_brain_orphan_max_age_s_env_tunable(C, monkeypatch):
    monkeypatch.setenv("JARVIS_BRAIN_ORPHAN_MAX_AGE_S", "60")
    assert C._brain_orphan_max_age_s() == 60

    monkeypatch.setenv("JARVIS_BRAIN_ORPHAN_MAX_AGE_S", "not-a-number")
    assert C._brain_orphan_max_age_s() == C._DEFAULT_BRAIN_ORPHAN_MAX_AGE_S

    monkeypatch.delenv("JARVIS_BRAIN_ORPHAN_MAX_AGE_S", raising=False)
    assert C._brain_orphan_max_age_s() == C._DEFAULT_BRAIN_ORPHAN_MAX_AGE_S
