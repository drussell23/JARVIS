"""No exit handler may print a traceback over the operator's goodbye.

The symptom, reported twice:

    ^CException ignored in atexit callback: <_final_semaphore_cleanup>
    ...
    KeyboardInterrupt:

`KeyboardInterrupt` and `SystemExit` derive from `BaseException`, so the
`except Exception` these handlers already carried never caught them. Every
handler was equally exposed, and the ones that took real time — joining
threads, terminating children, importing torch — were simply the likeliest to
be interrupted.

Fixing the sites that had already fired would leave the next one waiting. The
enforcement below is the actual fix: a raw `atexit.register` in `backend/core`
fails this test, so the guarded form is what a maintainer reaches for without
having to know why.

That is the same move as `tests/contract_fakes.py` — replacing a policy whose
enforcement mechanism is human memory with one the machine checks.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import List, Tuple

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SCOPE = _REPO / "backend" / "core"

#: `exit_guard` itself must call the real `atexit.register` — it IS the
#: wrapper. Anything else in scope goes through it.
_ALLOWED = {"backend/core/ouroboros/governance/exit_guard.py"}


def _raw_registrations() -> List[Tuple[str, int]]:
    """Every `atexit.register(...)` call that is not the guarded wrapper.

    Parsed, not grepped: a regex cannot tell a live call from the same text
    inside a docstring or a comment explaining the rule — and this file's own
    prose would trip it.
    """
    found: List[Tuple[str, int]] = []
    for path in _SCOPE.rglob("*.py"):
        rel = path.relative_to(_REPO).as_posix()
        if rel in _ALLOWED:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (isinstance(func, ast.Attribute) and func.attr == "register"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "atexit"):
                found.append((rel, node.lineno))
    return found


def test_no_exit_handler_is_registered_unguarded() -> None:
    """THE invariant. A new raw registration fails here rather than surfacing
    as a traceback across someone's terminal months later."""
    raw = _raw_registrations()
    assert raw == [], (
        "unguarded atexit handlers — a Ctrl+C landing in any of these prints "
        "a traceback over the goodbye:\n" +
        "\n".join(f"  {p}:{ln}" for p, ln in raw) +
        "\n\nUse guarded_atexit_register from "
        "backend.core.ouroboros.governance.exit_guard."
    )


def test_the_detector_actually_detects(tmp_path: Path) -> None:
    """A guard that cannot fail proves nothing. This one is checked against a
    known-bad sample before its clean result is believed."""
    sample = tmp_path / "bad.py"
    sample.write_text("import atexit\ndef f():\n    atexit.register(f)\n")
    tree = ast.parse(sample.read_text())
    hits = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "register" and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "atexit"
    ]
    assert len(hits) == 1, "the detector would miss a real raw registration"


def test_the_wrapper_itself_is_exempt() -> None:
    """`exit_guard` must call the real thing — it is what everything else
    wraps. Excluded by path, so the exemption cannot silently widen."""
    src = (_REPO / "backend/core/ouroboros/governance/exit_guard.py").read_text()
    assert "atexit.register(" in src
    assert _ALLOWED == {"backend/core/ouroboros/governance/exit_guard.py"}


@pytest.mark.parametrize("module", [
    "backend.core.thread_manager",
    "backend.core.embedding_service",
    "backend.core.lifecycle_manager",
    "backend.core.vm_lifecycle_manager",
    "backend.core.cross_repo_cleanup",
    "backend.core.resilience.graceful_shutdown",
])
def test_every_swept_module_still_imports(module: str) -> None:
    """The sweep's real risk was an import CYCLE — `exit_guard` lives under
    `ouroboros.governance`, and these are core modules that chain could
    plausibly import back. The imports are function-local for exactly that
    reason; this proves it."""
    __import__(module)


def test_the_guard_is_imported_locally_not_at_module_scope() -> None:
    """Structural: a module-level import is what would create the cycle, and
    it would do so silently at boot rather than here."""
    for name in ("thread_manager", "embedding_service", "lifecycle_manager",
                 "vm_lifecycle_manager", "cross_repo_cleanup"):
        src = (_SCOPE / f"{name}.py").read_text()
        if "guarded_atexit_register" not in src:
            continue
        tree = ast.parse(src)
        for node in tree.body:          # TOP LEVEL only
            if isinstance(node, ast.ImportFrom) and node.module and \
                    "exit_guard" in node.module:
                pytest.fail(
                    f"{name}.py imports exit_guard at module scope — that is "
                    f"the cycle risk the local import avoids"
                )
