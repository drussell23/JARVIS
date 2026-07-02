"""Atomic Hydration Handshake -- strict cross-window observability.

The window-1 (suspend) -> window-2 (resume) transition must be cryptographically
LEGIBLE, never silent. Proves:
  1. list_pending emits a POSITIVE "HMAC-SHA256 VERIFIED" handshake (op + digest) when
     a checkpoint's signature verifies -- symmetric with the existing REJECT log.
  2. The handshake is forced to BOTH the logger (debug.log) AND stdout (operator console).
  3. The prefill-injection handshake reports the EXACT byte-length + a repr snippet of
     the partial_completion being injected into the LLM as the assistant prefill.
  4. End-to-end: a resume dispatch (partial rides in intake evidence) emits the
     prefill-injection handshake with the right bytes + snippet.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging

import pytest

import backend.core.ouroboros.governance.fsm_checkpoint as ckpt


# --- 1+2. crypto verification handshake -> logger + stdout ------------------

def test_list_pending_emits_hmac_verified_handshake(tmp_path, monkeypatch, caplog, capsys):
    monkeypatch.setenv("JARVIS_CHECKPOINT_DIR", str(tmp_path / "cp"))
    monkeypatch.setenv("JARVIS_CHECKPOINT_HMAC_SECRET", "handshake-secret")
    cp = ckpt.FSMCheckpoint(op_id="op-hs-1", phase="GENERATE",
                            partial_completion="def f(): pass")
    assert ckpt.write_checkpoint(cp)

    with caplog.at_level(logging.INFO):
        pend = ckpt.list_pending(base_dir=None)
    assert any(c.op_id == "op-hs-1" for c in pend)

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "HMAC-SHA256 VERIFIED" in logged
    assert "op-hs-1" in logged
    assert "digest=" in logged                       # the verified signature prefix
    # forced to stdout too (operator console), not just the log file
    assert "HMAC-SHA256 VERIFIED" in capsys.readouterr().out


def test_rejected_checkpoint_emits_no_verified_handshake(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("JARVIS_CHECKPOINT_DIR", str(tmp_path / "cp"))
    monkeypatch.setenv("JARVIS_CHECKPOINT_HMAC_SECRET", "handshake-secret")
    cp = ckpt.FSMCheckpoint(op_id="op-hs-bad", phase="GENERATE", partial_completion="x")
    assert ckpt.write_checkpoint(cp)
    # Tamper: flip the secret so the HMAC no longer verifies.
    monkeypatch.setenv("JARVIS_CHECKPOINT_HMAC_SECRET", "different-secret")
    with caplog.at_level(logging.INFO):
        pend = ckpt.list_pending(base_dir=None)
    assert pend == []                                # fail-closed
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "HMAC-SHA256 VERIFIED" not in logged      # no false handshake on reject
    assert "REJECT" in logged


# --- 3. prefill-injection handshake: exact bytes + snippet ------------------

def test_format_prefill_handshake_reports_bytes_and_snippet():
    partial = "def solve(x):\n    return x"
    line = ckpt.format_prefill_handshake("op-pi", partial)
    assert "PREFILL-INJECT" in line
    assert "op-pi" in line
    assert "partial_bytes=%d" % len(partial.encode("utf-8")) in line
    assert "partial_chars=%d" % len(partial) in line
    assert repr(partial) in line                     # exact snippet (short -> full repr)


def test_format_prefill_handshake_truncates_long_partial_exactly():
    partial = "A" * 400
    line = ckpt.format_prefill_handshake("op-long", partial)
    assert "partial_bytes=400" in line
    assert " … " in line                             # head … tail elision
    assert repr("A" * 48) in line                    # exact head preserved


def test_multibyte_partial_bytes_not_chars():
    partial = "café — 日本語"                         # multibyte: bytes != chars
    line = ckpt.format_prefill_handshake("op-mb", partial)
    b = len(partial.encode("utf-8"))
    assert "partial_bytes=%d" % b in line
    assert "partial_chars=%d" % len(partial) in line
    assert b != len(partial)                          # sanity: they genuinely differ


# --- 4. end-to-end: resume dispatch emits the prefill handshake -------------

def test_resume_dispatch_emits_prefill_handshake(tmp_path, monkeypatch, caplog, capsys):
    monkeypatch.setenv("JARVIS_JPRIME_DISPATCH_READY_ENABLED", "false")  # gate predates this test
    monkeypatch.setenv("JARVIS_CHECKPOINT_DIR", str(tmp_path / "cp"))
    monkeypatch.setenv("JARVIS_CHECKPOINT_HMAC_SECRET", "hs")
    import json
    import backend.core.ouroboros.governance.candidate_generator as cg
    import backend.core.ouroboros.governance.local_inference_director as lidmod
    import backend.core.ouroboros.governance.providers as provmod
    from types import SimpleNamespace
    import datetime as _dt

    partial = "def resumed():\n    # continue here"

    class _FakeClient:
        def __init__(self, cfg, session=None, profiler=None):
            self._resume_prefill = ""
        async def warmup(self, *, timeout_s):
            return True
        async def aclose(self):
            pass

    class _FakeProvider:
        def __init__(self, client, repo_root=None, **_kw):  # venom/dilation kwargs
            self._client = client
        async def generate(self, context, deadline):
            # Prove the prefill actually reached the client.
            assert self._client._resume_prefill == partial
            return SimpleNamespace(candidates=("ok",))

    monkeypatch.setattr(lidmod, "LocalPrimeClient", _FakeClient)
    monkeypatch.setattr(provmod, "PrimeProvider", _FakeProvider)

    class _Stub:
        _repo_root = None
        def _remaining_seconds(self, dl):
            return 60.0
        async def _resolve_dispatch_model_name(self, ep):
            return "qwen2.5-coder:32b"
        async def _negotiate_num_ctx(self, ep):
            return 8192
        def _failover_profiler_for(self, ep, cfg):
            return lidmod.LatencyProfiler(cfg)

    dl = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=60)
    ctx = SimpleNamespace(
        op_id="op-resume", phase="GENERATE", description="resume",
        target_files=("m.py",), provider_route="standard",
        intake_evidence_json=json.dumps({"partial_completion": partial}),
    )
    with caplog.at_level(logging.INFO):
        asyncio.run(cg.CandidateGenerator._failover_local_dispatch(
            _Stub(), ctx, dl, "http://n:11434"))

    out = caplog.text + capsys.readouterr().out
    assert "PREFILL-INJECT" in out
    assert "op-resume" in out
    assert "partial_bytes=%d" % len(partial.encode("utf-8")) in out
