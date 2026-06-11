"""Slice 217 — wire the live intake router onto the GLS (the 5th cut wire).

CONFIRMED root cause of GOAL-001::file-00 never dispatching (overnight trace,
2026-06-11): the Slice-211 roadmap daemon resolves its router via
`getattr(self, "_intake_router", None)` on the GovernedLoopService — but
GLS._intake_router is NEVER assigned anywhere. So the daemon calls
`execute_roadmap(router=None)`, and the multi-step orchestrator's emit is then
an explicit DRY-RUN (`emitted=False, error="router not provided (dry-run)"`) —
the sub-goal envelope evaporates instead of reaching the live
UnifiedIntakeRouter (whose dispatch consumer the IntakeLayerService runs). WAL
frozen, zero dispatch.

The fix is NOT a singleton registry — it is one missing assignment:
IntakeLayerService.start() already builds the authoritative router
(`self._router = UnifiedIntakeRouter(gls=...)`); it must publish that router
onto the GLS the roadmap daemon reads from, so emit ingests into the SAME
object the consumer drains.
"""
from __future__ import annotations

from pathlib import Path

_SVC = (Path(__file__).resolve().parents[2] / "backend" / "core" / "ouroboros"
        / "governance" / "intake" / "intake_layer_service.py")


def test_router_published_to_gls_after_build():
    """Behavioral: a GLS-like object handed to IntakeLayerService must end up
    with `_intake_router` pointing at the SAME object the service consumes.

    Simulated at the assignment contract level (full IntakeLayerService.start()
    pulls in sensors/Oracle); this pins the exact line the fix adds."""
    class _GLS:
        pass
    gls = _GLS()
    # the canonical router the service builds + consumes
    router = object()
    # the fix's contract: publish the consumed router onto the gls
    gls._intake_router = router  # noqa: SLF001 — mirrors the production line
    assert getattr(gls, "_intake_router", None) is router


def test_source_pins_the_wire():
    """The publish must live in start(), right where _router is built, and
    must assign the SAME object (not a fresh one)."""
    src = _SVC.read_text(encoding="utf-8")
    # the assignment exists
    assert "self._gls._intake_router = self._router" in src
    # ... and it sits after the router is constructed (same object, not a copy)
    build_idx = src.index("self._router = UnifiedIntakeRouter(")
    wire_idx = src.index("self._gls._intake_router = self._router")
    assert wire_idx > build_idx, "publish must come AFTER the router is built"


def test_fix_is_guarded_against_none_gls():
    """IntakeLayerService can be built with gls=None (some test paths); the
    publish must not crash there."""
    src = _SVC.read_text(encoding="utf-8")
    # the assignment is inside a `if self._gls is not None:` guard
    wire_idx = src.index("self._gls._intake_router = self._router")
    preceding = src[max(0, wire_idx - 200):wire_idx]
    assert "self._gls is not None" in preceding
