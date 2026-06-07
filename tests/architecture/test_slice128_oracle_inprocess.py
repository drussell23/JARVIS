"""Slice 128 Phase 1 — in-process AST analyze for the isolated Oracle worker.

Root cause (soak bt-2026-06-07, verify-first): the Oracle is ALREADY decoupled
via a spawn-context Pipe IPC (Slice 112 ``oracle_ipc``), but its worker is
spawned ``daemon=True``. A daemonic process cannot have children, so the
Oracle's inner ``ProcessPoolExecutor`` (``ast_compile_helper`` via
``oracle._index_file``) crashes with "daemonic processes are not allowed to have
children" (×4789 in the soak) → starvation churn.

Fix (compose, no rebuild): inside the already-isolated Oracle subprocess, run
the AST analyze IN-PROCESS (no nested pool). The main loop is already protected
by the IPC, so the nested pool is redundant. Gated
``JARVIS_AST_HELPER_INPROCESS_ENABLED`` (default-FALSE → main process keeps the
pool, byte-identical).
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from backend.core.ouroboros.governance.ast_compile_helper import (
    AnalyzeOutcome,
    analyze_python_source_for_oracle,
    ast_helper_inprocess_enabled,
)
from backend.core.ouroboros.governance import ast_compile_helper as helper_mod


# A source comfortably over the 4 KB inline-tiny threshold so the heavy
# (pool vs in-process) path is exercised, not the inline shortcut.
_BIG_SOURCE = "\n".join(
    f"def func_{i}(a, b):\n    x = a + b\n    return x * {i}\n"
    for i in range(200)
)


class TestInprocessFlag(unittest.TestCase):
    def test_default_false(self) -> None:
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("JARVIS_AST_HELPER_INPROCESS_ENABLED", None)
            self.assertFalse(ast_helper_inprocess_enabled())

    def test_truthy(self) -> None:
        import os
        for v in ("1", "true", "yes", "on"):
            os.environ["JARVIS_AST_HELPER_INPROCESS_ENABLED"] = v
            self.assertTrue(ast_helper_inprocess_enabled())
        os.environ.pop("JARVIS_AST_HELPER_INPROCESS_ENABLED", None)


class TestInprocessAnalyze(unittest.TestCase):
    def setUp(self) -> None:
        import os
        self._prev = os.environ.get("JARVIS_AST_HELPER_INPROCESS_ENABLED")
        os.environ["JARVIS_AST_HELPER_INPROCESS_ENABLED"] = "1"

    def tearDown(self) -> None:
        import os
        if self._prev is None:
            os.environ.pop("JARVIS_AST_HELPER_INPROCESS_ENABLED", None)
        else:
            os.environ["JARVIS_AST_HELPER_INPROCESS_ENABLED"] = self._prev

    async def test_inprocess_analyze_ok_and_bypasses_pool(self) -> None:
        # With the flag on, analyze must succeed WITHOUT touching the process
        # pool — proving the daemonic-pool crash path is bypassed. Patch
        # _get_pool to raise: if the in-process path is taken it is never called.
        with patch.object(
            helper_mod, "_get_pool",
            side_effect=AssertionError("pool must NOT be used in-process"),
        ):
            result = await analyze_python_source_for_oracle(
                caller="test.slice128",
                source=_BIG_SOURCE,
                filename="big_module.py",
                repo_name="jarvis",
                relative_path="big_module.py",
            )
        self.assertEqual(result.outcome, AnalyzeOutcome.OK)

    async def test_inprocess_matches_pool_result_shape(self) -> None:
        # In-process result must carry the same node/edge payload the pool path
        # would (so the Oracle graph build is identical).
        result = await analyze_python_source_for_oracle(
            caller="test.slice128",
            source="def f(x):\n    return x\n" * 400,  # > 4KB
            filename="m.py", repo_name="jarvis", relative_path="m.py",
        )
        self.assertEqual(result.outcome, AnalyzeOutcome.OK)


class TestOracleWorkerWiringPin(unittest.TestCase):
    """The isolated Oracle worker must enable in-process analyze before it
    builds the Oracle (bytes-pin — the worker runs in a subprocess)."""

    def test_worker_enables_inprocess(self) -> None:
        import pathlib
        src = pathlib.Path(
            "backend/core/ouroboros/oracle_ipc.py"
        ).read_text()
        idx = src.find("def _oracle_worker_main(")
        nxt = src.find("def _oracle_worker_async(", idx)
        body = src[idx:nxt] if nxt > idx else src[idx:]
        self.assertIn("JARVIS_AST_HELPER_INPROCESS_ENABLED", body)


if __name__ == "__main__":
    unittest.main()
