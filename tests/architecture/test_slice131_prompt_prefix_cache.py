"""Slice 131 Phase 2a — Tool-Catalog Prompt Restructuring (prefix segregation).

The leak: the STABLE tool catalog + output schema are appended to the END of the
VOLATILE per-op lean prompt (after the task + source snapshot), so they are
re-billed every op instead of riding the ~90%-cheaper cached prefix. The fix is a
structural inversion — divert the stable tail into the cached SYSTEM prefix,
leaving only volatile content in the user message.

This suite pins the GATED MECHANISM (default-FALSE → OFF byte-identical):
  * ``_prompt_prefix_cache_enabled`` default-FALSE.
  * ``_route_stable_tail`` (one source of truth): OFF → stable tail stays in the
    user-prompt ``parts`` (byte-identical legacy); ON → diverted into the stable
    sink (→ caller folds it into the cached system block); no-sink → fail-safe to
    parts.
  * ``_build_cached_system_blocks`` wraps base+stable in ONE ephemeral
    cache_control breakpoint with the tool catalog inside it.

NOTE (honest): the end-to-end cross-function activation (threading the sink from
``_build_lean_codegen_prompt`` through ``_generate_raw`` to the system seam) +
the live "model still uses its tools flawlessly" verification require a funded
interactive Anthropic soak (the spec's Phase 3) — that is the graduation gate,
not a unit test. OFF is proven byte-identical here.
"""
from __future__ import annotations

import os
import unittest

from backend.core.ouroboros.governance import providers as P


class TestGate(unittest.TestCase):
    def setUp(self):
        os.environ.pop("JARVIS_PROMPT_PREFIX_CACHE_ENABLED", None)

    def test_default_false(self):
        self.assertFalse(P._prompt_prefix_cache_enabled())

    def test_explicit_on(self):
        os.environ["JARVIS_PROMPT_PREFIX_CACHE_ENABLED"] = "1"
        try:
            self.assertTrue(P._prompt_prefix_cache_enabled())
        finally:
            os.environ.pop("JARVIS_PROMPT_PREFIX_CACHE_ENABLED", None)


class TestRouteStableTail(unittest.TestCase):
    def test_off_keeps_tail_in_user_parts_byte_identical(self):
        parts, stable = [], []
        P._route_stable_tail(parts, stable, "TOOLS", "SCHEMA", enabled=False)
        self.assertEqual(parts, ["TOOLS", "SCHEMA"])  # legacy: in the user prompt
        self.assertEqual(stable, [])

    def test_on_diverts_tail_into_stable_prefix(self):
        parts, stable = [], []
        P._route_stable_tail(parts, stable, "TOOLS", "SCHEMA", enabled=True)
        self.assertEqual(stable, ["TOOLS", "SCHEMA"])  # → cached system prefix
        self.assertEqual(parts, [])                     # user prompt stays volatile

    def test_on_without_sink_falls_back_to_parts(self):
        parts = []
        P._route_stable_tail(parts, None, "TOOLS", "SCHEMA", enabled=True)
        self.assertEqual(parts, ["TOOLS", "SCHEMA"])  # fail-safe: never lose tools

    def test_empty_tool_section_only_schema_routed(self):
        # VENOM_SKIP routes emit an empty tool section → only the schema rides.
        parts, stable = [], []
        P._route_stable_tail(parts, stable, "", "SCHEMA", enabled=True)
        self.assertEqual(stable, ["SCHEMA"])
        self.assertEqual(parts, [])


class TestCachedSystemCarriesTools(unittest.TestCase):
    def test_stable_prefix_folds_into_one_cached_block(self):
        prov = P.ClaudeProvider("test-key")
        prov._prompt_cache_enabled = True
        prov._prompt_cache_min_chars = 0
        base = P._CODEGEN_SYSTEM_PROMPT
        stable = "\n\n".join(["**Available Tools** read_file ...", "## Output Schema ..."])
        blocks = prov._build_cached_system_blocks(base + "\n\n" + stable)
        self.assertIsInstance(blocks, list)
        self.assertEqual(blocks[-1]["cache_control"], {"type": "ephemeral"})
        self.assertIn("Available Tools", blocks[-1]["text"])  # tools now cached


if __name__ == "__main__":
    unittest.main()
