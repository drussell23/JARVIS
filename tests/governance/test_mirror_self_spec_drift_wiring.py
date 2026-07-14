"""Task #2 — the spec-drift detector, wired live.

mirror_self_spec_drift was TRULY DEAD (0 non-test importers) — the module built
to detect drift between CLAUDE.md's stated flag defaults and the FlagRegistry's
actual defaults, i.e. the exact tool that would surface the "severed wire masked
by stale docs" class this whole audit kept finding by hand. These tests prove:
  1. the master graduated to default-TRUE (a read-only, advisory-only observer;
     OFF is blind, not safe);
  2. run_spec_drift_audit composes the module's own adapters into the EXISTING
     invariant-drift advisory bridge (no parallel machinery) and emits on drift;
  3. it is fail-soft everywhere (a drift audit must never perturb its caller);
  4. the post-commit scheduler self-gates and never raises into the commit path.
"""
from __future__ import annotations

from backend.core.ouroboros.governance import mirror_self_spec_drift as msd


# ── Default graduation ────────────────────────────────────────────────

def test_master_defaults_true(monkeypatch):
    monkeypatch.delenv("JARVIS_MIRROR_SELF_SPEC_DRIFT_ENABLED", raising=False)
    assert msd.master_enabled() is True


def test_kill_switch_intact(monkeypatch):
    monkeypatch.setenv("JARVIS_MIRROR_SELF_SPEC_DRIFT_ENABLED", "false")
    assert msd.master_enabled() is False
    # When off, the live entry point does nothing and returns None.
    assert msd.run_spec_drift_audit() is None


# ── Synthetic-drift fakes (real detector runs; only the I/O edges faked) ──

from backend.core.ouroboros.governance.flag_registry import FlagType


class _FakeSpec:
    """Mirrors the REAL FlagSpec contract that _lookup_spec/_spec_is_bool read:
    ``.type is FlagType.BOOL`` (the actual enum, NOT Python bool) + ``.default``
    + ``.source_file``. Using the real enum is the point — a fake that used
    Python ``bool`` would diverge from the collaborator and mask behavior."""
    def __init__(self, default: bool):
        self.type = FlagType.BOOL
        self.default = default
        self.source_file = "fake_registry.py"


class _FakeRegistry:
    def __init__(self, mapping):
        self._m = mapping

    # detect_spec_drift resolves via _lookup_spec → registry.get_spec(flag).
    def get_spec(self, flag):
        return self._m.get(flag)


class _CapturingBridge:
    def __init__(self):
        self.calls = []

    def emit(self, snapshot, records):
        self.calls.append((snapshot, records))


def _drifted_spec_text() -> str:
    # CLAUDE.md claims TRUE; registry (below) will say FALSE → drift.
    return "`JARVIS_FOO_ENABLED` default-TRUE — some documentation prose."


def test_run_emits_into_the_existing_bridge_on_drift(monkeypatch):
    """The end-to-end wire: real detect_spec_drift finds the mismatch, the
    module's own adapters convert it, and it flows into the EXISTING
    invariant-drift auto-action bridge via install_auto_action_bridge()."""
    monkeypatch.setenv("JARVIS_MIRROR_SELF_SPEC_DRIFT_ENABLED", "true")
    registry = _FakeRegistry({"JARVIS_FOO_ENABLED": _FakeSpec(default=False)})

    captured = _CapturingBridge()
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.invariant_drift_auto_action_bridge"
        ".install_auto_action_bridge",
        lambda *a, **k: captured,
    )

    report = msd.run_spec_drift_audit(
        spec_text=_drifted_spec_text(), registry=registry,
    )
    assert report is not None
    assert report.verdict is msd.SpecDriftVerdict.DRIFTED
    assert len(report.records) == 1
    assert report.records[0].flag == "JARVIS_FOO_ENABLED"
    assert report.records[0].claimed_default is True
    assert report.records[0].actual_default is False

    # …and it reached the existing bridge.
    assert len(captured.calls) == 1
    snapshot, records = captured.calls[0]
    assert snapshot is not None
    assert len(records) == 1


def test_no_emit_when_aligned(monkeypatch):
    """CLAUDE.md claim matches the registry → ALIGNED → nothing emitted."""
    monkeypatch.setenv("JARVIS_MIRROR_SELF_SPEC_DRIFT_ENABLED", "true")
    registry = _FakeRegistry({"JARVIS_FOO_ENABLED": _FakeSpec(default=True)})
    captured = _CapturingBridge()
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.invariant_drift_auto_action_bridge"
        ".install_auto_action_bridge",
        lambda *a, **k: captured,
    )
    report = msd.run_spec_drift_audit(
        spec_text="`JARVIS_FOO_ENABLED` default-TRUE", registry=registry,
    )
    assert report.verdict is msd.SpecDriftVerdict.ALIGNED
    assert captured.calls == []


def test_emit_false_detects_but_does_not_route(monkeypatch):
    """The boot one-shot uses emit=False semantics via the master; verify the
    detect-without-route path: emit=False finds drift, logs, no bridge call."""
    monkeypatch.setenv("JARVIS_MIRROR_SELF_SPEC_DRIFT_ENABLED", "true")
    registry = _FakeRegistry({"JARVIS_FOO_ENABLED": _FakeSpec(default=False)})
    captured = _CapturingBridge()
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.invariant_drift_auto_action_bridge"
        ".install_auto_action_bridge",
        lambda *a, **k: captured,
    )
    report = msd.run_spec_drift_audit(
        emit=False, spec_text=_drifted_spec_text(), registry=registry,
    )
    assert report.verdict is msd.SpecDriftVerdict.DRIFTED
    assert captured.calls == []


# ── Fail-soft: a drift audit must NEVER raise into its caller ─────────────

def test_fail_soft_on_exploding_bridge(monkeypatch):
    monkeypatch.setenv("JARVIS_MIRROR_SELF_SPEC_DRIFT_ENABLED", "true")
    registry = _FakeRegistry({"JARVIS_FOO_ENABLED": _FakeSpec(default=False)})

    def _boom(*a, **k):
        raise RuntimeError("bridge down")
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.invariant_drift_auto_action_bridge"
        ".install_auto_action_bridge",
        _boom,
    )
    # Must not raise — the drift is still detected and returned.
    report = msd.run_spec_drift_audit(
        spec_text=_drifted_spec_text(), registry=registry,
    )
    assert report.verdict is msd.SpecDriftVerdict.DRIFTED


def test_fail_soft_on_garbage_registry(monkeypatch):
    monkeypatch.setenv("JARVIS_MIRROR_SELF_SPEC_DRIFT_ENABLED", "true")

    class _Bad:
        def get(self, flag):
            raise ValueError("nope")
    # Must not raise.
    report = msd.run_spec_drift_audit(
        spec_text=_drifted_spec_text(), registry=_Bad(),
    )
    assert report is not None  # returns a report, no exception


# ── The post-commit scheduler: self-gates, never raises ───────────────────

def test_post_commit_scheduler_self_gates_when_off(monkeypatch):
    from backend.core.ouroboros.governance import auto_committer as ac
    monkeypatch.setenv("JARVIS_MIRROR_SELF_SPEC_DRIFT_ENABLED", "false")
    called = {"n": 0}
    monkeypatch.setattr(msd, "run_spec_drift_audit",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    # No running loop here; the scheduler must swallow that too (never raises).
    ac._schedule_post_commit_spec_drift("op-1", "deadbeef")
    assert called["n"] == 0  # gated off → not invoked


def test_post_commit_scheduler_never_raises_without_loop(monkeypatch):
    from backend.core.ouroboros.governance import auto_committer as ac
    monkeypatch.setenv("JARVIS_MIRROR_SELF_SPEC_DRIFT_ENABLED", "true")
    # No running event loop → run_in_executor(get_running_loop) raises inside;
    # the scheduler must swallow it and never propagate.
    ac._schedule_post_commit_spec_drift("op-1", "deadbeef")  # must not raise


# ── The live wiring exists (no silent inert regression) ───────────────────

def test_run_spec_drift_audit_is_exported():
    assert "run_spec_drift_audit" in msd.__all__
    assert callable(msd.run_spec_drift_audit)


def test_post_commit_seam_is_wired():
    """Guard against the wired-but-inert trap: the commit path must actually
    call the scheduler."""
    import inspect
    from backend.core.ouroboros.governance import auto_committer as ac
    src = inspect.getsource(ac)
    assert "_schedule_post_commit_spec_drift(op_id, commit_hash)" in src, (
        "post-commit spec-drift call site missing — the wire is inert"
    )
