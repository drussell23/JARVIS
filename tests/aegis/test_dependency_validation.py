"""Aegis boot-time dependency validation — the silent-degradation gate.

The failure class this closes, observed live on 2026-07-24: `sentence-transformers`
was declared in `backend/requirements.txt:49` but absent from the interpreter, so
`EmbeddingService` logged ONE warning and ran on the LITE tier for a 15-hour
session. Nothing failed; the organism just quietly got worse at its job.

These pin the honest-reporting contract. Reconciliation (the pip call) is
default-disarmed, so the default-path tests never touch the network.
"""

from __future__ import annotations

import pytest

from backend.core.ouroboros.aegis.preflight import (
    DependencyStatus,
    DependencyValidationStep,
    PreflightOutcome,
    validate_dependencies,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Every knob starts unset so tests bind to documented defaults."""
    for var in (
        "JARVIS_AEGIS_DEP_VALIDATION_ENABLED",
        "JARVIS_AEGIS_DEP_RECONCILE_ENABLED",
        "JARVIS_AEGIS_DEP_STRICT",
        "JARVIS_AEGIS_DEP_CRITICAL",
    ):
        monkeypatch.delenv(var, raising=False)


def test_intact_when_declared_package_is_importable(monkeypatch):
    """A package that resolves → INTACT, nothing missing."""
    monkeypatch.setenv("JARVIS_AEGIS_DEP_CRITICAL", "json=json")

    step = validate_dependencies()

    assert step.status is DependencyStatus.INTACT
    assert step.checked == ("json",)
    assert step.missing == ()
    assert step.unresolved == ()


def test_drift_detected_and_reported_when_reconcile_disarmed(monkeypatch):
    """The core assertion: an absent package is REPORTED, not swallowed.

    Reconciliation is off by default, so the step must still name the drift
    rather than returning INTACT — silence is the bug being fixed."""
    monkeypatch.setenv(
        "JARVIS_AEGIS_DEP_CRITICAL",
        "definitely_not_a_real_module_xyz=definitely-not-real-xyz==1.0",
    )

    step = validate_dependencies()

    assert step.status is DependencyStatus.DRIFT
    assert step.missing == ("definitely_not_a_real_module_xyz",)
    assert step.unresolved == ("definitely_not_a_real_module_xyz",)
    assert step.reconciled == ()
    assert "disarmed" in (step.detail or "")


def test_reconcile_requires_import_to_actually_work(monkeypatch):
    """pip exiting 0 is necessary but NOT sufficient — the step re-probes the
    real import path, so a package that 'installs' but cannot be imported is
    still reported unresolved rather than laundered into RECONCILED."""
    import subprocess

    monkeypatch.setenv("JARVIS_AEGIS_DEP_RECONCILE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_AEGIS_DEP_CRITICAL", "still_missing_after_pip=whatever==1.0")

    class _FakeCompleted:
        returncode = 0
        stdout = ""
        stderr = ""

    # Mirrors the real contract: same call shape, same return attributes.
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _FakeCompleted(),
    )

    step = validate_dependencies()

    assert step.status is DependencyStatus.DRIFT
    assert step.reconciled == ()
    assert step.unresolved == ("still_missing_after_pip",)


def test_reconcile_success_path_reports_reconciled(monkeypatch):
    """When pip succeeds AND the module becomes importable → RECONCILED."""
    import subprocess

    monkeypatch.setenv("JARVIS_AEGIS_DEP_RECONCILE_ENABLED", "true")
    # `json` is already importable; force it to look missing on the first probe
    # so we exercise the missing → reconcile → re-probe → success sequence.
    monkeypatch.setenv("JARVIS_AEGIS_DEP_CRITICAL", "json=json")

    import backend.core.ouroboros.aegis.preflight as pf

    calls = {"n": 0}
    real_importable = pf._importable

    def _flaky(name):
        calls["n"] += 1
        if calls["n"] == 1:
            return False          # first probe: "missing"
        return real_importable(name)   # post-reconcile probe: real answer

    monkeypatch.setattr(pf, "_importable", _flaky)

    class _Ok:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Ok())

    step = validate_dependencies()

    assert step.status is DependencyStatus.RECONCILED
    assert step.reconciled == ("json",)
    assert step.unresolved == ()


def test_reconcile_survives_pip_blowing_up(monkeypatch):
    """A pip subprocess that raises must not take boot down with it."""
    import subprocess

    monkeypatch.setenv("JARVIS_AEGIS_DEP_RECONCILE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_AEGIS_DEP_CRITICAL", "nope_not_here=nope-not-here==1.0")

    def _boom(*a, **k):
        raise OSError("no pip for you")

    monkeypatch.setattr(subprocess, "run", _boom)

    step = validate_dependencies()   # must not raise

    assert step.status is DependencyStatus.DRIFT
    assert step.unresolved == ("nope_not_here",)


def test_step_can_be_disabled_entirely(monkeypatch):
    monkeypatch.setenv("JARVIS_AEGIS_DEP_VALIDATION_ENABLED", "false")

    step = validate_dependencies()

    assert step.status is DependencyStatus.SKIPPED
    assert step.checked == ()


def test_malformed_critical_entry_does_not_brick_boot(monkeypatch):
    """A typo in the env list is skipped, never raised."""
    monkeypatch.setenv("JARVIS_AEGIS_DEP_CRITICAL", ",,  ,json=json,")

    step = validate_dependencies()

    assert step.status is DependencyStatus.INTACT
    assert step.checked == ("json",)


def test_default_critical_list_names_sentence_transformers():
    """Regression pin on the actual incident: the package whose absence caused
    the silent LITE-tier degradation must be in the default watch list."""
    from backend.core.ouroboros.aegis.preflight import _critical_deps

    names = [n for n, _ in _critical_deps()]
    assert "sentence_transformers" in names


def test_result_serialisation_is_lossless_and_additive():
    """`dependencies` rides on AegisPreflightResult.to_dict without disturbing
    the existing schema version."""
    from backend.core.ouroboros.aegis.preflight import (
        AegisPreflightResult,
        PREFLIGHT_SCHEMA_VERSION,
    )

    step = DependencyValidationStep(
        status=DependencyStatus.DRIFT, checked=("a",), missing=("a",), unresolved=("a",),
    )
    result = AegisPreflightResult(
        outcome=PreflightOutcome.READY, dependencies=step,
    )
    d = result.to_dict()

    assert d["schema_version"] == PREFLIGHT_SCHEMA_VERSION
    assert d["dependencies"]["status"] == "drift"
    assert d["dependencies"]["missing"] == ["a"]
    # The credential must never appear, dependency field or not.
    assert "bootstrap_psk" not in d


def test_result_without_dependencies_serialises_none():
    """Back-compat: paths that ran before the step still serialise cleanly."""
    from backend.core.ouroboros.aegis.preflight import AegisPreflightResult

    d = AegisPreflightResult(outcome=PreflightOutcome.READY).to_dict()
    assert d["dependencies"] is None


def test_strict_mode_outcome_exists_in_taxonomy():
    """The strict-mode terminal is a real member of the closed taxonomy."""
    assert PreflightOutcome.FAILED_DEPENDENCY_DRIFT.value == "failed_dependency_drift"
