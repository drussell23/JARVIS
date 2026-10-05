"""The harness publishes an ABSOLUTE session dir (bt-2026-10-05-005023).

Children inherit ``JARVIS_OUROBOROS_SESSION_DIR`` from a different cwd -- a
VALIDATE sandbox runs in ``/tmp/jarvis_repair_sandbox_*``. The relative
``.ouroboros/sessions/<id>`` resolved to nothing there, so the test execution
lock could not recognise the soak's own validation runs and refused six of them.
"""
from __future__ import annotations

import ast
import os
import time
from pathlib import Path

from backend.core.ouroboros.governance import test_execution_lock as tel

_HARNESS = Path(__file__).resolve().parents[2] / "backend/core/ouroboros/battle_test/harness.py"


def test_harness_exports_the_session_dir_resolved():
    tree = ast.parse(_HARNESS.read_text(encoding="utf-8"))
    exports = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", "") == "setdefault"
        and node.args and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "JARVIS_OUROBOROS_SESSION_DIR"
    ]
    assert len(exports) == 1
    assert ".resolve()" in ast.unparse(exports[0].args[1])


def test_a_relative_export_is_invisible_from_a_sandbox_cwd(tmp_path, monkeypatch):
    """The failure mode, reproduced: the same session, seen from another cwd."""
    root = tmp_path / "repo"
    session = root / ".ouroboros" / "sessions" / "bt-x"
    session.mkdir(parents=True)
    (session / "heartbeat.tick").write_text(str(time.time()))
    proc = tmp_path / "proc" / "self"
    proc.mkdir(parents=True)
    (proc / "cgroup").write_text("0::/init.scope\n")
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.chdir(sandbox)
    relative = {"JARVIS_OUROBOROS_SESSION_DIR": os.path.relpath(session, root)}
    absolute = {"JARVIS_OUROBOROS_SESSION_DIR": str(session.resolve())}
    assert tel.exemption_reason(relative, proc_root=tmp_path / "proc") is None
    assert tel.exemption_reason(absolute, proc_root=tmp_path / "proc") == "soak_tenant_inside_soak_budget"
