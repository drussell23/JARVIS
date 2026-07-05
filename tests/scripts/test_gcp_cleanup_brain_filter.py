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

Zero real gcloud calls -- subprocess.run is monkeypatched.
"""

import importlib.util
import pathlib
import sys
from typing import Any, List

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
