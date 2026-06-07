"""Slice 133 — The Sovereign Episodic Memory Core.

A continuously-aware organism must passively recall its immediate past without a
manual MEMORY_SEARCH. This builds an append-only ``EpisodicLedger`` composing the
existing tamper-evident ``BlueEvidenceLedger`` (hash-chain receipt per episode) +
``SemanticIndex`` (long-term embedding as episodes age out of the short-term
window), plus passive injection of the recent window into the generation prompt's
VOLATILE tail (never the P2a cached prefix).

Gated ``JARVIS_EPISODIC_CORE_ENABLED`` default-FALSE; embedder + ledger injectable
→ unit-tested without fastembed / disk.
"""
from __future__ import annotations

import asyncio
import os
import unittest

from backend.core.ouroboros.governance import episodic_core as EC


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class _Emb:
    """Deterministic fake embedder: summary→distinct unit vector by first char."""
    def embed(self, texts):
        out = []
        for t in texts:
            c = (t or "x")[0].lower()
            out.append([1.0, 0.0, 0.0] if c in "ar" else [0.0, 1.0, 0.0])
        return out


class _BlueSpy:
    def __init__(self):
        self.records = []
    def record(self, *, attack_class, payload, verdict, blocked, blocked_by=""):
        self.records.append((attack_class, payload, verdict))
        return object()


class TestGate(unittest.TestCase):
    def setUp(self):
        os.environ.pop("JARVIS_EPISODIC_CORE_ENABLED", None)

    def test_default_false(self):
        self.assertFalse(EC.episodic_core_enabled())


class TestLedger(unittest.TestCase):
    def setUp(self):
        os.environ["JARVIS_EPISODIC_CORE_ENABLED"] = "1"

    def tearDown(self):
        os.environ.pop("JARVIS_EPISODIC_CORE_ENABLED", None)

    def _led(self, window=8, blue=None, emb=None):
        return EC.EpisodicLedger(window=window, blue_ledger=blue, embedder=emb or _Emb())

    def test_record_and_recent(self):
        led = self._led()
        for i in range(3):
            _run(led.record(kind="transition", op_id=f"op{i}", summary=f"did thing {i}"))
        recent = led.recent(2)
        self.assertEqual(len(recent), 2)
        self.assertEqual(recent[-1].summary, "did thing 2")

    def test_window_evicts_oldest(self):
        led = self._led(window=2)
        for i in range(3):
            _run(led.record(kind="transition", op_id=f"op{i}", summary=f"s{i}"))
        recent = led.recent(10)
        self.assertEqual(len(recent), 2)
        self.assertEqual(recent[0].summary, "s1")  # s0 evicted

    def test_durable_receipt_per_episode(self):
        blue = _BlueSpy()
        led = self._led(blue=blue)
        _run(led.record(kind="error", op_id="opE", summary="boom"))
        self.assertEqual(len(blue.records), 1)
        self.assertEqual(blue.records[0][0], "error")      # attack_class = kind
        self.assertEqual(blue.records[0][2], "recorded")   # verdict

    def test_writethrough_on_eviction_enables_recall(self):
        led = self._led(window=1)
        _run(led.record(kind="route", op_id="o1", summary="apple routing decision"))
        _run(led.record(kind="route", op_id="o2", summary="banana something else"))
        # o1 ('apple' → [1,0,0]) evicted → embedded → recallable
        hits = _run(led.recall("avocado query", k=1))  # 'a' → [1,0,0] matches apple
        self.assertTrue(hits)
        self.assertEqual(hits[0].op_id, "o1")

    def test_writethrough_fail_soft(self):
        class _Boom:
            def embed(self, texts):
                raise RuntimeError("embedder down")
        led = self._led(window=1, emb=_Boom())
        _run(led.record(kind="t", op_id="a", summary="x"))
        _run(led.record(kind="t", op_id="b", summary="y"))  # eviction embed fails soft
        self.assertEqual(led.recent(10)[-1].op_id, "b")  # ledger still works

    def test_render_recent_block(self):
        led = self._led()
        _run(led.record(kind="complete", op_id="opX", summary="finished the refactor"))
        block = led.render_recent(5)
        self.assertIn("finished the refactor", block)
        self.assertIn("opX", block)


class TestRenderHelperGate(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("JARVIS_EPISODIC_CORE_ENABLED", None)
        EC.reset_episodic_ledger()

    def test_render_empty_when_disabled(self):
        os.environ.pop("JARVIS_EPISODIC_CORE_ENABLED", None)
        self.assertEqual(EC.render_episodic_context(5), "")

    def test_render_reflects_singleton_when_enabled(self):
        os.environ["JARVIS_EPISODIC_CORE_ENABLED"] = "1"
        EC.reset_episodic_ledger()
        _run(EC.get_episodic_ledger().record(
            kind="route", op_id="opR", summary="chose DW cheap tier"))
        block = EC.render_episodic_context()
        self.assertIn("chose DW cheap tier", block)
        self.assertIn("opR", block)

    def test_record_transition_convenience_gated(self):
        os.environ.pop("JARVIS_EPISODIC_CORE_ENABLED", None)
        self.assertIsNone(_run(EC.record_transition(
            op_id="o", phase_from="GENERATE", phase_to="VALIDATE")))
        os.environ["JARVIS_EPISODIC_CORE_ENABLED"] = "1"
        EC.reset_episodic_ledger()
        ep = _run(EC.record_transition(
            op_id="o", phase_from="GENERATE", phase_to="VALIDATE"))
        self.assertIsNotNone(ep)
        self.assertEqual(ep.context["phase_to"], "VALIDATE")


if __name__ == "__main__":
    unittest.main()
