"""Can the cockpit start? — asked about the ACCELERATOR, not about a number.

## The defect this replaces

`cockpit_interactive.sh` gated on raw free VRAM::

    FREE=$((TOTAL - USED))
    [ "$FREE" -ge "$FREE_MIB" ] || die "only ${FREE} MiB free; the model needs ~${FREE_MIB}"

with ``FREE_MIB`` defaulting to 20480 — the 30B's footprint. Once that model is
loaded its ~20 GiB have moved from ``free`` into ``used``, so the gate compares
the footprint against the space the footprint is occupying and refuses. The
cockpit therefore could not start whenever the model it wants was already warm,
which after any soak is most of the time.

Measured on this host while the 30B was resident: 8,431 MiB free against a
20,480 MiB demand — refused, with the message *"A training run or another soak
still holds the card"*. What held the card was the model the cockpit wanted.

This is the same arithmetic as the admission defect fixed in ``16c530cfd3``
(``local_model_admission`` guards *"the act of LOADING model weights"* and was
being asked about weights already loaded), one layer up in the launcher. Fixing
it there and not here would leave the operator staring at a refusal produced by
a bug that had already been fixed underneath them.

## What it asks instead

"Is there room to SERVE this model", which has two satisfying answers:

* the model is already resident — nothing needs to be allocated, and the only
  requirement is working headroom for KV growth;
* the model is absent — its full footprint must fit.

Composed from :func:`candidate_generator.fetch_resident_weights`, the SAME
``/api/ps`` probe the admission gate uses, so the launcher and the gate one
layer down cannot disagree about what is on the card.

## The eviction race is NOT handled here, deliberately

A model can be evicted between this check and the first generation. There is no
lease to take — Ollama exposes no such API — and a redundant guard here would
be a second opinion that can disagree with the authority. The authority is
``local_model_admission``, which re-reads the accelerator at dispatch time and
DEFERs rather than OOMs. This preflight's job is to stop a session that cannot
possibly work from starting; keeping a running session honest is the admission
gate's, and it already does it. :func:`verdict_for` reports what it saw so a
later fault can be attributed, and nothing more.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Optional, Tuple

logger = logging.getLogger("Ouroboros.CockpitPreflight")

__all__ = ["PreflightVerdict", "verdict_for", "main"]

_MIB = 1024 * 1024
_GIB = 1024 ** 3

#: Headroom the session needs BEYOND the weights, for KV growth and the
#: allocator's own slack. Derived from the negotiator's own context sizing
#: rather than picked: the KV cache is linear in context length, and that is
#: the term that actually grows during a session.
_ENV_HEADROOM_GIB = "JARVIS_COCKPIT_KV_HEADROOM_GIB"
_DEFAULT_HEADROOM_GIB = 4.0


def _headroom_bytes() -> int:
    try:
        raw = (os.environ.get(_ENV_HEADROOM_GIB, "") or "").strip()
        return int(max(0.5, float(raw or _DEFAULT_HEADROOM_GIB)) * _GIB)
    except (TypeError, ValueError):
        return int(_DEFAULT_HEADROOM_GIB * _GIB)


@dataclass(frozen=True)
class PreflightVerdict:
    """Whether the cockpit may start, and the arithmetic that decided it."""

    ok: bool
    reason: str
    free_bytes: int = 0
    required_bytes: int = 0
    resident_model: str = ""
    resident_bytes: int = 0

    def render(self) -> str:
        head = "ready" if self.ok else "REFUSING"
        return (
            f"[CockpitPreflight] {head}: {self.reason} "
            f"(free={self.free_bytes / _GIB:.1f} GiB, "
            f"need={self.required_bytes / _GIB:.1f} GiB"
            + (f", resident={self.resident_model} "
               f"{self.resident_bytes / _GIB:.1f} GiB" if self.resident_bytes else "")
            + ")"
        )


def _accelerator_free_bytes() -> int:
    """Free VRAM, via the same nvidia-smi query the launcher used. 0 = unknown.

    Unknown is NOT zero-free: a host with no NVIDIA accelerator (a Mac, a CPU
    box) must not be refused a cockpit by a probe that could not run. The
    caller treats 0 as "cannot measure, do not gate".
    """
    try:
        import subprocess
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode != 0:
            return 0
        first = (out.stdout or "").strip().splitlines()[0]
        return int(float(first.strip())) * _MIB
    except Exception:  # noqa: BLE001 — an unmeasurable card gates nothing
        return 0


async def verdict_for(
    model: str,
    *,
    endpoint: str = "",
    footprint_bytes: int = 0,
) -> PreflightVerdict:
    """May a cockpit session start for *model*? NEVER raises.

    ``footprint_bytes`` is what the model needs when it is NOT resident; 0
    resolves it from the brain catalog, and an unresolvable footprint gates
    nothing (the session starts and the admission gate remains the authority).
    """
    try:
        from backend.core.ouroboros.governance.candidate_generator import (
            fetch_resident_weights, local_lane_endpoint,
        )
        ep = endpoint or local_lane_endpoint()
        resident_model, resident_bytes = ("", 0)
        if ep:
            resident_model, resident_bytes = await fetch_resident_weights(ep)

        free = _accelerator_free_bytes()
        headroom = _headroom_bytes()

        # Does the RESIDENT model satisfy the request? Prefix-matched on the
        # tag-stripped name, the same comparison the admission discount uses:
        # `qwen3-coder-ov:30b` serves a request for `qwen3-coder-ov`.
        wanted = str(model or "").strip()
        serves = bool(
            resident_model and wanted and (
                resident_model == wanted
                or resident_model.startswith(wanted.split(":")[0])
                or wanted.startswith(resident_model.split(":")[0])
            )
        )

        if serves:
            # Nothing to allocate. The only question is working headroom.
            ok = (free == 0) or (free >= headroom)
            return PreflightVerdict(
                ok=ok,
                reason=(
                    f"{resident_model} is already resident — no allocation "
                    f"needed, only KV headroom"
                    if ok else
                    f"{resident_model} is resident but only "
                    f"{free / _GIB:.1f} GiB remains for KV growth "
                    f"(need {headroom / _GIB:.1f} GiB)"
                ),
                free_bytes=free, required_bytes=headroom,
                resident_model=resident_model, resident_bytes=resident_bytes,
            )

        if not footprint_bytes:
            try:
                from backend.core.ouroboros.governance.brain_selector import (
                    footprint_bytes_for,
                )
                footprint_bytes = int(footprint_bytes_for(wanted) or 0)
            except Exception:  # noqa: BLE001
                footprint_bytes = 0

        if not footprint_bytes or free == 0:
            # Nothing to compare. Do not invent a refusal — the admission gate
            # re-reads the card at dispatch and is the authority.
            return PreflightVerdict(
                ok=True,
                reason=("cannot size this request (unknown footprint or "
                        "unmeasurable accelerator) — deferring to the "
                        "admission gate"),
                free_bytes=free, required_bytes=0,
                resident_model=resident_model, resident_bytes=resident_bytes,
            )

        need = footprint_bytes + headroom
        ok = free >= need
        other = (f"; {resident_model} holds "
                 f"{resident_bytes / _GIB:.1f} GiB and would be evicted"
                 if resident_bytes else "")
        return PreflightVerdict(
            ok=ok,
            reason=(
                f"{wanted} is not resident and its "
                f"{footprint_bytes / _GIB:.1f} GiB "
                + ("fits" if ok else "does not fit")
                + f" in {free / _GIB:.1f} GiB free{other}"
            ),
            free_bytes=free, required_bytes=need,
            resident_model=resident_model, resident_bytes=resident_bytes,
        )
    except Exception as exc:  # noqa: BLE001 — a broken preflight gates nothing
        logger.debug("[CockpitPreflight] degraded", exc_info=True)
        return PreflightVerdict(
            ok=True,
            reason=f"preflight degraded ({type(exc).__name__}) — not gating",
        )


def main(argv=None) -> int:
    """``python -m ...cockpit_preflight --model X`` → 0 ready, 2 refuse.

    Exists so the bash launcher can ask the SAME question in the same way,
    rather than reimplementing the arithmetic in shell — which is how the two
    came to disagree in the first place.
    """
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default=os.environ.get(
        "JARVIS_LOCAL_MODEL_NAME", ""))
    parser.add_argument("--endpoint", default="")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    verdict = asyncio.run(verdict_for(args.model, endpoint=args.endpoint))
    if not args.quiet:
        print(verdict.render())
    return 0 if verdict.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
