"""The backend's /health "degraded" was two defects, not a real degradation.

Boot of 2026-10-14 09:20: JARVIS announced "JARVIS is degraded" -- truthfully,
/health said so -- because of
- supervisor_progress: the progress bridge was attached after the live
  lifespan's ``yield`` (its shutdown half), so only at shutdown;
- trinity_bridge: the adapter defined a class on watchdog's base even when
  watchdog was missing, so it failed to import (NameError).
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def _function(tree: ast.AST, name: str) -> ast.AsyncFunctionDef:
    return next(n for n in ast.walk(tree)
                if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == name)


def test_the_live_lifespan_attaches_the_progress_bridge_during_startup():
    tree = ast.parse((REPO / "backend" / "main.py").read_text(encoding="utf-8"))
    lifespan = _function(tree, "parallel_lifespan")
    yields = [n.lineno for n in ast.walk(lifespan) if isinstance(n, ast.Yield)]
    attaches = [n.lineno for n in ast.walk(lifespan)
                if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "attach_app"]
    assert yields and attaches
    assert max(attaches) < min(yields), "attach_app must run before the lifespan yields"


def test_the_trinity_bridge_adapter_loads_without_watchdog():
    probe = (
        "import sys; sys.modules['watchdog'] = None; "        # import watchdog -> ImportError
        "sys.modules['watchdog.observers'] = None; sys.modules['watchdog.events'] = None; "
        f"sys.path.insert(0, {str(REPO / 'backend')!r}); sys.path.insert(0, {str(REPO)!r}); "
        "import system.trinity_bridge_adapter as t; "
        "assert t.WATCHDOG_AVAILABLE is False; print('ok')"
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0 and "ok" in out.stdout, out.stderr[-2000:]
