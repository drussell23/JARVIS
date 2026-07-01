#!/usr/bin/env python3
"""LOCAL PROOF (no cloud): cooperative stream cancellation -> GSI propagation ->
signed checkpoint written to the REAL repo .ouroboros/checkpoints.

Exercises the production modules end-to-end (no pytest, no mocks of our own code):
  1. Real LocalPrimeClient streaming a BLOCKED read; cooperative_shutdown.request()
     fires mid-stream from a sibling task.
  2. The Preemptive Async Race drops the blocked I/O and raises the BaseException
     GracefulStreamInterruption carrying the exact partial buffer -- zero-latency.
  3. We prove GSI PIERCES a `try/except Exception` (the Venom tool-loop mimic).
  4. The real fsm_checkpoint writes a HMAC-signed checkpoint (with the partial) to the
     actual <repo>/.ouroboros/checkpoints, and list_pending reads it back verified.

Run:  python3 scripts/prove_cooperative_checkpoint_local.py
"""
from __future__ import annotations

import asyncio
import dataclasses
import os
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
_CKPT_DIR = _REPO / ".ouroboros" / "checkpoints"
os.environ.setdefault("JARVIS_FAILOVER_USE_ADC", "false")
os.environ["JARVIS_CHECKPOINT_DIR"] = str(_CKPT_DIR)
os.environ.setdefault("JARVIS_CHECKPOINT_HMAC_SECRET", "local-proof-secret")

import backend.core.ouroboros.governance.local_inference_director as lid
import backend.core.ouroboros.governance.cooperative_shutdown as coop
from backend.core.ouroboros.governance import fsm_checkpoint as ckpt


def _ok(msg: str) -> None:
    print(f"  \033[32m[OK]\033[0m {msg}")


def _hdr(msg: str) -> None:
    print(f"\n\033[1m{msg}\033[0m")


class _BlockingReader:
    async def readline(self):
        await asyncio.sleep(9999)   # simulates the 32B mid-generation (slow chunk)
        return b""


class _Resp:
    def __init__(self, reader):
        self.content = reader
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False


class _Sess:
    def __init__(self, reader):
        self._r = reader
    def post(self, url, **kw):
        return _Resp(self._r)
    async def close(self):
        pass


def _cfg(**over):
    return dataclasses.replace(lid.LocalConfig.from_env(), **over)


async def _race_and_checkpoint():
    coop.reset()
    _hdr("1. Exception hierarchy (the Earmuff Bypass)")
    assert issubclass(lid.GracefulStreamInterruption, BaseException)
    assert not issubclass(lid.GracefulStreamInterruption, Exception)
    _ok("GracefulStreamInterruption is a BaseException, NOT an Exception")

    _hdr("2. Preemptive async race (real LocalPrimeClient, blocked read)")
    client = lid.LocalPrimeClient(_cfg(num_ctx=8192), session=_Sess(_BlockingReader()))

    async def _fire():
        await asyncio.sleep(0.2)
        coop.request("wall_clock_cap")

    asyncio.ensure_future(_fire())
    t0 = time.monotonic()
    partial = None
    try:
        await client.complete(system="s", user="u", prompt_tokens=10, stream=True,
                              prefill="def solve(x):\n    # begin real 32B thought")
    except lid.GracefulStreamInterruption as gsi:
        partial = gsi.partial
    elapsed = time.monotonic() - t0
    assert partial is not None, "stream was NOT interrupted"
    assert elapsed < 2.0, f"interrupt took {elapsed:.2f}s (should be ~0.2s)"
    _ok(f"blocked read dropped + GSI raised in {elapsed*1000:.0f}ms (buffer preserved)")

    _hdr("3. GSI pierces `try/except Exception` (Venom tool-loop mimic)")
    pierced = False
    try:
        try:
            raise lid.GracefulStreamInterruption("freeze", partial=partial)
        except Exception:  # noqa: BLE001 -- the tool loop's per-round guard
            print("  [!!] swallowed by except Exception -- BUG")
    except lid.GracefulStreamInterruption:
        pierced = True
    assert pierced, "GSI was swallowed by except Exception"
    _ok("GSI propagated straight through the tool-loop's except Exception")

    _hdr("4. Signed checkpoint written to the REAL .ouroboros/checkpoints")
    from types import SimpleNamespace
    op_id = f"op-local-proof-{int(time.time())}"
    ckpt.stash_partial(op_id, partial)
    ctx = SimpleNamespace(op_id=op_id, phase="GENERATE", description="local proof",
                          target_files=("solve.py",), intake_evidence_json="",
                          provider_route="standard")
    cp = ckpt.capture_from_context(ctx, phase="GENERATE",
                                   resume_reason="graceful_stream_interruption")
    assert cp is not None
    path = ckpt.write_checkpoint(cp)
    coop.reset()

    _hdr("5. Atomic Hydration Handshake (exactly what Window 2 prints)")
    # list_pending re-verifies HMAC -> emits the crypto handshake to stdout below.
    pend = ckpt.list_pending(base_dir=None)
    mine = [c for c in pend if c.op_id == op_id]
    assert mine, "checkpoint not found on disk / failed HMAC verification"
    got = mine[0]
    assert got.partial_completion == partial, "partial mismatch after roundtrip"
    # The prefill-inject handshake fires when the resume dispatch feeds the partial to
    # the 32B; emit it here to show the operator the exact bytes + snippet.
    ckpt.emit_handshake(ckpt.format_prefill_handshake(op_id, got.partial_completion))
    _ok("both handshake lines emitted above (HMAC-SHA256 VERIFIED + PREFILL-INJECT)")
    _ok(f"on-disk checkpoint: {path}")
    return path


def main() -> int:
    print("\033[1m=== LOCAL PROOF: cooperative cancellation -> checkpoint (no cloud) ===\033[0m")
    print(f"checkpoint dir: {_CKPT_DIR}")
    path = asyncio.run(_race_and_checkpoint())
    print("\n\033[1;32mALL LOCAL PROOFS PASSED.\033[0m")
    print(f"Inspect the on-disk checkpoint: cat {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
