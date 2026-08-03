"""
Root conftest — dynamic resolution of the legacy root-level ``test_*.py`` files.

The CI blind spot
-----------------
``pytest.ini`` pinned ``testpaths = tests``, so the 89 ``test_*.py`` files sitting
at the repository root were never collected. 65 of them define real test
functions; that coverage has been invisible to CI for the life of the repo.

Why this is a gate and not a plain glob
---------------------------------------
Those files are scripts as much as they are tests. A static AST census of all 89
(no imports executed) found:

  * 65 define test functions  -> real, collectible coverage
  * 24 define none            -> pure scripts; collecting them imports them for
                                 zero test yield
  * 78 carry module-level executable statements, which run at *collection* time

In the 65 test-bearing files the module-level code is benign — ``sys.path.insert``,
``logging.basicConfig``, ``Path``, ``os.getcwd``. The hazardous code lives almost
entirely in the 24 script-only files: ``test_pyautogui_direct.py`` (24 top-level
statements), ``test_direct_click.py``, ``test_tv_detection.py`` (30) drive the
mouse, synthesize clicks and probe displays on import. Enabling a blanket glob
would fire all of that during ``--collect-only``.

So collection is resolved dynamically: a file is collected only if it actually
declares tests. The rule is computed, not a hand-maintained denylist, so it stays
correct as files are added or gutted.

``_UNSAFE_AT_IMPORT`` is the one explicit carve-out — files that declare tests but
still cannot be safely imported.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_HERE = Path(__file__).parent

#: Test-bearing files that nevertheless cannot be safely imported.
#:
#: A static AST census cannot detect a *hang* — only executing the import does.
#: A per-file collection probe (45s ceiling, 64 admitted files) found four that
#: never return: they load speech/vision models or probe hardware at module
#: scope. A hang is strictly worse than a failure in CI, because it burns the
#: whole job's wall clock and reports nothing, so these are gated by name.
#:
#: Deliberately NOT listed here: the six modules whose collection *errors*
#: (missing ``pyautogui`` / ``pytesseract``, and one genuine stale import of
#: ``get_advanced_autonomous_engine``). Those are true findings — the first yield
#: of closing this blind spot — and hiding them would recreate the blind spot in
#: a new place. They should be fixed, not silenced.
_UNSAFE_AT_IMPORT = frozenset(
    {
        # asyncio.run(...) at module scope — starts a live handler on import.
        "test_live_handler.py",
        # Hang on import (>45s, no return) — model load / hardware probe.
        "test_god_mode.py",
        "test_voice_god_mode.py",
        "test_ferrari_integration_simple.py",
        "test_stereo_vision.py",
    }
)


def _declares_tests(path: Path) -> bool:
    """True if ``path`` declares a pytest-visible test, judged without importing.

    Mirrors the ``python_functions``/``python_classes`` patterns in pytest.ini:
    a module-level ``test_*`` function, or a ``Test*`` class holding one.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, OSError):
        # Unparseable: let pytest surface the real error rather than hide it.
        return True

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            return True
        if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for member in node.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)) and member.name.startswith("test_"):
                    return True
    return False


def _resolve_root_ignores() -> list[str]:
    """Root-level ``test_*.py`` files that must not be collected."""
    ignored: list[str] = []
    for path in sorted(_HERE.glob("test_*.py")):
        if path.name in _UNSAFE_AT_IMPORT or not _declares_tests(path):
            ignored.append(path.name)
    return ignored


#: Consumed by pytest during root-directory collection.
collect_ignore = _resolve_root_ignores()

logger.debug(
    "[root-conftest] collecting %d root test modules, ignoring %d script-only/unsafe",
    len(list(_HERE.glob("test_*.py"))) - len(collect_ignore),
    len(collect_ignore),
)
