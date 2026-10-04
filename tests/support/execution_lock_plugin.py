"""Pytest seam for the resource-aware test execution lock.

Registered from the ROOT ``conftest.py`` so every invocation from the repo --
``pytest tests/...``, a root-level ``test_*.py``, a scripted wrapper -- passes
through it; there is no un-gated way to start a suite from this checkout.

Lifecycle (decisions live in ``governance.test_execution_lock``):

* ``pytest_configure`` (tryfirst, before collection imports anything heavy):
  under the cross-process admission lock, size the grant against live soaks
  and sibling runs, refuse with exit 75 if it is below the floor, otherwise
  move this process into a ``MemoryMax``-capped scope. Beside a live soak, a
  run that cannot be contained is refused rather than run unprotected.
* ``pytest_collection_finish``: now that the item count is known, compare
  the grant with the peak this machine has measured for runs of that scale;
  refuse BEFORE the first test executes if it cannot fit.
* ``pytest_unconfigure``: record the scope's measured ``memory.peak`` and
  OOM kills to the ledger the next prediction learns from.
"""
from __future__ import annotations

import time
from typing import Any, Dict

import pytest

from backend.core.ouroboros.governance import test_execution_lock as tel

_STATE = pytest.StashKey[Dict[str, Any]]()


def _refuse(message: str) -> None:
    pytest.exit(f"ResourceExhaustion: {message}", returncode=tel.EXIT_RESOURCE_EXHAUSTION)


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    state: Dict[str, Any] = {"exempt": None, "grant": None, "scope": None,
                             "items": 0, "need": None, "exitstatus": None}
    config.stash[_STATE] = state
    reason = tel.exemption_reason()
    if reason:
        state["exempt"] = reason
        return

    from backend.core.ouroboros.governance.memory_pressure_gate import get_default_gate

    # Decide inside the lock, refuse after releasing it: the flock section
    # never raises by contract, so an exit raised inside it is swallowed.
    refusal = ""
    with tel.admission_section() as locked:
        if not locked:
            refusal = "admission lock unavailable -- concurrent grants cannot be serialized"
        else:
            probe = get_default_gate().probe()
            total = int(probe.total_bytes) if probe.ok else 0
            grant = tel.compute_grant(
                total_bytes=total,
                available_bytes=int(probe.available_bytes) if probe.ok else 0,
                cgroup_headroom=tel.cgroup_headroom_bytes(tel.own_cgroup()),
                tenants=tel.soak_tenants(total_bytes=total) if total else [],
                sibling_reserve=tel.sibling_scope_reserve(),
            )
            state["grant"], state["total"] = grant, total
            if not grant.admitted:
                refusal = grant.describe()
            else:
                state["scope"] = tel.adopt_into_scope(grant.granted_bytes)
                if state["scope"] is None and grant.tenants:
                    refusal = ("cannot contain this run beside a live soak "
                               "(systemd user manager unavailable): " + grant.describe())
    if refusal:
        _refuse(refusal)


def pytest_collection_finish(session: pytest.Session) -> None:
    state = session.config.stash.get(_STATE, None)
    if not state or state["grant"] is None:
        return
    state["items"] = len(session.items)
    need = tel.predict_need(state["items"], tel.read_ledger(), total_bytes=state["total"])
    state["need"] = need
    if need.need_bytes > state["grant"].granted_bytes:
        gib = 1024 ** 3
        _refuse(
            f"{state['items']} tests are predicted to need {need.need_bytes / gib:.1f}GiB "
            f"({need.basis}, {need.samples} comparable runs) but only "
            f"{state['grant'].granted_bytes / gib:.1f}GiB can be granted: "
            + state["grant"].describe()
        )


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    state = session.config.stash.get(_STATE, None)
    if state is not None:
        state["exitstatus"] = int(exitstatus)


def pytest_report_header(config: pytest.Config) -> str:
    state = config.stash.get(_STATE, None) or {}
    if state.get("exempt"):
        return f"test execution lock: exempt ({state['exempt']})"
    grant = state.get("grant")
    if grant is None:
        return "test execution lock: inactive"
    where = state.get("scope") or "UNCONTAINED (no live soak; systemd user manager unavailable)"
    return f"test execution lock: {grant.describe()} scope={where}"


def pytest_terminal_summary(terminalreporter: Any, config: pytest.Config) -> None:
    state = config.stash.get(_STATE, None) or {}
    if not state.get("scope"):
        return
    peak, oom_kills = tel.scope_stats()
    state["peak"], state["oom_kills"] = peak, oom_kills
    if oom_kills:
        terminalreporter.write_line(
            f"test execution lock: {oom_kills} process(es) OOM-killed inside "
            f"{state['scope']} at its {state['grant'].granted_bytes / 1024 ** 3:.1f}GiB "
            f"ceiling -- the guest and any live soak were protected",
            yellow=True,
        )


def pytest_unconfigure(config: pytest.Config) -> None:
    state = config.stash.get(_STATE, None) or {}
    if not state.get("scope") or not state.get("items") or config.option.collectonly:
        return
    peak, oom_kills = state.get("peak"), state.get("oom_kills")
    if peak is None:
        peak, oom_kills = tel.scope_stats()
    if peak is None:
        return
    tel.record_run(tel.RunRecord(
        ts=time.time(), items=state["items"], peak_bytes=int(peak),
        granted_bytes=state["grant"].granted_bytes, oom_kills=int(oom_kills or 0),
        exitstatus=state.get("exitstatus"),
    ))
