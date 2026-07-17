"""VM orchestration hold — sever spend, keep the failover logic."""
from __future__ import annotations
import pytest
import backend.core.ouroboros.governance.failover_lifecycle as fl


def test_hold_flag_default_false(monkeypatch):
    monkeypatch.delenv("JARVIS_FAILOVER_VM_ORCHESTRATION_HOLD", raising=False)
    assert fl.failover_vm_orchestration_held() is False   # prod byte-identical


def test_hold_flag_enables(monkeypatch):
    monkeypatch.setenv("JARVIS_FAILOVER_VM_ORCHESTRATION_HOLD", "true")
    assert fl.failover_vm_orchestration_held() is True


def test_hold_is_at_the_single_spend_chokepoint():
    """Structural: the guard sits in _do_awaken, the ONLY place _vm_awaken_fn
    is invoked — so it covers BOTH the sovereign REST path and the gcloud
    fallback, and cannot be bypassed by either."""
    import inspect
    src = inspect.getsource(fl.FailoverLifecycleController._do_awaken)
    # Strip comments — a mention of the symbol in prose is not an invocation
    # (the first draft of this test asserted against its own comment text).
    code = "\n".join(
        l for l in src.splitlines() if not l.strip().startswith("#")
    )
    assert "failover_vm_orchestration_held()" in code
    hold = code.index("failover_vm_orchestration_held()")
    invoke = code.index("self._vm_awaken_fn")          # the real spend trigger
    assert hold < invoke, "hold must precede the spend trigger"
    # and it must RETURN before reaching it
    assert "return" in code[hold:invoke]
    # upstream failover logic preserved (severed trigger, not dismantled logic)
    assert "_build_startup_script" in code and "RAM PRE-FLIGHT" in src
