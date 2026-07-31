"""The superseded infrastructure orchestrator must not arm itself.

`infrastructure_orchestrator.py` provisions and tears down GCP resources —
`terraform destroy -target=<what we created>` on shutdown, with
`--auto-approve`. All three of its master flags defaulted to **true**.

That was harmless for one reason only: nothing imports the module. It has had
zero production importers since 2025-12-24, and `gcp_vm_manager` (34
production files) plus `failover_lifecycle` (12) own GCP lifecycle now. The
moment anyone wired this module back up — which is exactly what "bring the
dark flags to the light" would mean — shutdown would run a destroy with
auto-approve, on by default, with no operator ever having asked for it.

A dormant module with destructive defaults is a loaded gun with the safety
off, sitting in a drawer. Deleting it is a separate decision that needs
feature-parity review against `gcp_vm_manager`; taking the safety off the
table costs nothing today and is worth doing first.
"""
from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

_SRC = pathlib.Path("backend/core/infrastructure_orchestrator.py")

DESTRUCTIVE = (
    "JARVIS_INFRA_AUTO_DESTROY",
    "JARVIS_TERRAFORM_AUTO_APPROVE",
    "JARVIS_INFRA_ON_DEMAND",
)


def _default_for(flag: str) -> str:
    """The literal default in the module's own env read."""
    tree = ast.parse(_SRC.read_text())
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and getattr(node.func, "attr", "") == "getenv"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == flag):
            return str(node.args[1].value) if len(node.args) > 1 else ""
    raise AssertionError(f"{flag} not read in {_SRC}")


@pytest.mark.parametrize("flag", DESTRUCTIVE)
def test_the_default_is_off(flag):
    """Not 'true'. An ON default on an unimported module is a hazard that
    arms itself the day someone adopts it."""
    assert _default_for(flag).lower() in ("false", "0", "no", "off"), flag


def test_the_config_object_agrees_with_the_literal(monkeypatch):
    """The literal and the resolved value must not drift — a source-grep pin
    alone would pass while a factory overrode it."""
    import dataclasses

    for flag in DESTRUCTIVE:
        monkeypatch.delenv(flag, raising=False)
    import backend.core.infrastructure_orchestrator as M

    cfgs = [c for c in vars(M).values()
            if dataclasses.is_dataclass(c) and isinstance(c, type)
            and "auto_destroy_on_shutdown" in {f.name for f in dataclasses.fields(c)}]
    assert cfgs, "config dataclass not found"
    cfg = cfgs[0]()
    assert cfg.auto_destroy_on_shutdown is False


def test_an_operator_can_still_turn_it_on():
    """Default-off, not removed. The capability is intact for anyone who
    deliberately wants it — this only stops it arming itself."""
    src = _SRC.read_text()
    for flag in DESTRUCTIVE:
        assert flag in src


def test_the_reason_is_recorded_where_the_next_reader_will_look():
    """A bare `false` invites someone to 'fix' it back to true."""
    src = _SRC.read_text()
    assert "superseded" in src.lower()
    assert "gcp_vm_manager" in src
