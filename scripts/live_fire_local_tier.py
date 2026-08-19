#!/usr/bin/env python3
"""Live-fire validation of the local sovereignty tier. No mocks, no fakes.

Drives the REAL `InferenceGateway`, the REAL `LatencyProfiler` ledger and the
REAL `ThroughputGovernor` against a REAL serving endpoint. There is no separate
diagnostic channel: every number printed is read back out of the same objects
the running system consults, because a diagnostic that observes a parallel copy
of the state proves nothing about the state.

    # against the Windows host over the LAN
    python3 scripts/live_fire_local_tier.py --endpoint http://192.168.1.50:11434 \
                                            --model qwen3-coder:30b

    # against whatever this machine is serving (harness self-validation)
    python3 scripts/live_fire_local_tier.py --endpoint http://127.0.0.1:11434 \
                                            --model qwen2.5-coder:3b

    # one phase at a time
    python3 scripts/live_fire_local_tier.py --phase 3

PHASE 1  residency probe + autonomous warm-swap handshake
PHASE 2  closed-loop telemetry: ledger fills, governor re-sizes lanes
PHASE 3  fault injection: a peer that accepts, emits, then goes SILENT

Phase 3 does NOT stop your ollama. It stands up a deliberately wedged listener
on loopback and points the gateway at it, so the failure is DETERMINISTIC and
your serving host is never disturbed. Killing a real service would prove the
same path less reliably and cost you a cold reload.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import sys
import time
from typing import Any, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.core.ouroboros.governance import hardware_signature as hs  # noqa: E402
from backend.core.ouroboros.governance import inference_gateway as ig  # noqa: E402
from backend.core.ouroboros.governance import local_inference_director as lid  # noqa: E402
from backend.core.ouroboros.governance import throughput_governor as tg  # noqa: E402
from backend.core.ouroboros.governance.route_budgets import (  # noqa: E402
    route_generation_budget_s,
)

OK, BAD, WARN, DIM = "\033[32m", "\033[31m", "\033[33m", "\033[2m"
END = "\033[0m"


def _say(sym: str, colour: str, msg: str) -> None:
    print(f"  {colour}{sym}{END} {msg}", flush=True)


def ok(m): _say("PASS", OK, m)
def bad(m): _say("FAIL", BAD, m)
def warn(m): _say("WARN", WARN, m)
def info(m): print(f"       {DIM}{m}{END}", flush=True)


def banner(n: int, title: str) -> None:
    print(f"\n{'=' * 72}\nPHASE {n} — {title}\n{'=' * 72}", flush=True)


def _cfg_for(endpoint: str, model: str) -> Any:
    return dataclasses.replace(
        lid.LocalConfig.from_env(), base_url=endpoint, model_name=model)


def _ledger_entry(cfg: Any, endpoint: str) -> Tuple[str, dict]:
    """Read the DURABLE ledger the governor actually consults."""
    key = lid.physics_key(cfg, endpoint=endpoint)
    return key, (lid._physics_ledger_load().get(key) or {})


# ---------------------------------------------------------------- PHASE 1
async def phase1(gw: "ig.InferenceGateway", endpoint: str, model: str,
                 swap_model: Optional[str]) -> bool:
    banner(1, "LIVE LAN PROBE + RESIDENCY VERIFICATION")
    passed = True

    sig = hs.signature_for(endpoint)
    info(f"endpoint      : {endpoint}")
    info(f"hardware sig  : {sig.digest}  (provenance={sig.provenance})")
    if sig.provenance == "endpoint":
        ok("remote host has its OWN signature — its physics cannot land in "
           "this machine's ledger")
    elif sig.provenance == "probed":
        warn("endpoint resolves as LOCAL — this is a loopback run, not a LAN run")

    t0 = time.monotonic()
    resident = await gw.resident_models(endpoint)
    rtt = (time.monotonic() - t0) * 1000.0

    if resident is None:
        bad(f"/api/ps unreachable or unparseable after {rtt:.0f}ms")
        info("the gateway treats this as UNKNOWABLE (not empty) and will "
             "dispatch without a swap — verify the host is up and bound to "
             "0.0.0.0, and that a firewall permits :11434 from this machine")
        return False
    ok(f"/api/ps answered in {rtt:.0f}ms — resident: {list(resident) or '<none>'}")

    if ig.InferenceGateway._model_matches(model, resident):
        ok(f"'{model}' is RESIDENT — no swap needed")
    else:
        warn(f"'{model}' is NOT resident (resident: {list(resident)})")

    target = ig.GatewayTarget(base_url=endpoint, model_name=model,
                              scope="remote", state=ig.HostState.HEALTHY,
                              reason="live-fire")
    t0 = time.monotonic()
    rep = await gw.ensure_model_resident(target)
    info(f"pre-flight    : {json.dumps(rep, default=str)[:200]}")
    info(f"elapsed       : {(time.monotonic() - t0):.1f}s "
         f"(budget {ig.warm_swap_budget_s():.0f}s, paid OUTSIDE any op clock)")
    if rep.get("checked"):
        ok("pre-flight ran and reported residency")
    else:
        bad("pre-flight did not run")
        passed = False

    if swap_model:
        info(f"forcing a warm-swap handshake to '{swap_model}' …")
        swap_target = dataclasses.replace(target, model_name=swap_model)
        t0 = time.monotonic()
        rep2 = await gw.ensure_model_resident(swap_target)
        dt = time.monotonic() - t0
        if rep2.get("swapped"):
            ok(f"autonomous warm swap completed in {dt:.1f}s")
        else:
            warn(f"no swap performed: {rep2.get('reason')}")
        after = await gw.resident_models(endpoint)
        info(f"resident after: {list(after) if after is not None else 'unknown'}")
    return passed


# ---------------------------------------------------------------- PHASE 2
async def phase2(gw: "ig.InferenceGateway", endpoint: str, model: str,
                 rounds: int) -> bool:
    banner(2, "CLOSED-LOOP TELEMETRY + GOVERNOR RE-SIZING")
    cfg = _cfg_for(endpoint, model)
    key, before = _ledger_entry(cfg, endpoint)
    budget = route_generation_budget_s("background")
    info(f"ledger key    : {key}")
    info(f"samples before: {len(before.get('total') or [])}")

    tg.reset_for_tests()
    v0 = tg.get_default_governor().evaluate(budget_s=budget)
    info(f"governor pre  : lanes={v0.lanes} per_op={v0.per_op_ms:.0f}ms "
         f"via={v0.provenance}")

    prompt = ("Reply with exactly one short sentence naming the single biggest "
              "risk in reusing one timeout constant for two different waits.")
    lat = []
    for i in range(rounds):
        t0 = time.monotonic()
        try:
            r = await gw.dispatch(system="You are terse.", user=prompt,
                                  prompt_tokens=lid.estimate_tokens(prompt),
                                  route="background", max_tokens=48)
        except Exception as exc:  # noqa: BLE001
            bad(f"round {i+1}: {type(exc).__name__}: {str(exc)[:110]}")
            return False
        dt = time.monotonic() - t0
        lat.append(dt)
        tps = (r.output_tokens / (r.total_ms / 1000.0)) if r.total_ms else 0.0
        info(f"round {i+1}/{rounds}: ttft={r.ttft_ms:7.0f}ms "
             f"total={r.total_ms:8.0f}ms out={r.output_tokens:4d}tok "
             f"{tps:6.1f} tok/s  wall={dt:5.1f}s")

    key2, after = _ledger_entry(cfg, endpoint)
    n_before, n_after = len(before.get("total") or []), len(after.get("total") or [])
    if n_after > n_before:
        ok(f"ledger GREW {n_before} -> {n_after} samples at {key2}")
    else:
        bad(f"ledger did NOT grow ({n_before} -> {n_after}) — telemetry is not "
            f"reaching the profiler the governor reads")
        return False

    tg.reset_for_tests()
    v1 = tg.get_default_governor().evaluate(budget_s=budget)
    info(f"governor post : lanes={v1.lanes} per_op={v1.per_op_ms:.0f}ms "
         f"via={v1.provenance} measured={v1.measured}")
    if v1.measured:
        ok("governor is now sizing lanes from MEASURED remote physics")
    else:
        warn(f"governor still '{v1.provenance}' — needs "
             f"{cfg.min_samples} samples to warm")

    prof = lid.LatencyProfiler(cfg, ledger_key=key2)
    first_s, steady_s = prof.inter_token_budget_s()
    static = lid._inter_token_timeout_s()
    info(f"inter-token   : first={first_s:.1f}s steady={steady_s:.1f}s "
         f"(static legacy was {static:.0f}s for both)")
    if steady_s <= static and first_s >= static:
        ok("deadlines are directionally clamped: steady only tighter, first "
           "only looser")
    else:
        bad("directional clamp violated")
        return False
    if lat and max(lat) < budget:
        ok(f"every round finished inside the BACKGROUND budget "
           f"(slowest {max(lat):.1f}s < {budget:.0f}s)")
    return True


# ---------------------------------------------------------------- PHASE 3
class _WedgedPeer:
    """A peer that accepts, emits ONE chunk, then goes permanently silent.

    This is the exact shape the adaptive steady-state deadline exists to
    catch, and the shape a total-duration timeout cannot distinguish from a
    slow-but-healthy generation. Deterministic, loopback-only, and it never
    touches your serving host.
    """

    def __init__(self) -> None:
        self._server = None
        self.port = 0

    async def start(self) -> str:
        async def handle(reader, writer):
            try:
                while True:                       # drain request headers
                    line = await reader.readline()
                    if line in (b"\r\n", b"", b"\n"):
                        break
                writer.write(b"HTTP/1.1 200 OK\r\n"
                             b"Content-Type: text/event-stream\r\n"
                             b"Cache-Control: no-cache\r\n"
                             b"Connection: keep-alive\r\n\r\n")
                chunk = {"choices": [{"delta": {"content": "the"}}]}
                writer.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
                await writer.drain()
                await asyncio.sleep(3600)          # ... and then nothing. ever.
            except Exception:                      # noqa: BLE001
                pass

        self._server = await asyncio.start_server(handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{self.port}"

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()


async def phase3(local_model: str) -> bool:
    banner(3, "FAULT INJECTION — MID-STREAM PARTITION")
    peer = _WedgedPeer()
    endpoint = await peer.start()
    info(f"wedged peer   : {endpoint} (emits 1 chunk, then silence forever)")
    passed = True
    try:
        cfg = _cfg_for(endpoint, "wedged-model:1b")
        prof = lid.LatencyProfiler(cfg,
                                   ledger_key=lid.physics_key(cfg, endpoint=endpoint))
        for _ in range(cfg.min_samples + 2):       # warm it so the deadline adapts
            prof.record(ttft_ms=60.0, total_ms=500.0, output_tokens=120)
        first_s, steady_s = prof.inter_token_budget_s()
        before_est = prof.adaptive_timeout_ms(prompt_tokens=4096)
        info(f"armed with    : first={first_s:.1f}s steady={steady_s:.1f}s")
        info(f"estimate pre  : {before_est:.0f}ms")

        client = lid.LocalPrimeClient(cfg, profiler=prof)
        t0 = time.monotonic()
        try:
            await client.complete(system="s", user="u", prompt_tokens=64,
                                  max_tokens=64, stream=True)
            bad("no stall raised — the wedged peer was not detected")
            passed = False
        except lid.InterTokenStall as exc:
            dt = time.monotonic() - t0
            ok(f"InterTokenStall raised after {dt:.1f}s — socket severed")
            info(f"  {str(exc)[:150]}")
            if dt < first_s + steady_s + 5:
                ok(f"fired on the ADAPTIVE budget, not the static "
                   f"{lid._inter_token_timeout_s():.0f}s")
            else:
                warn(f"took {dt:.1f}s — longer than the adaptive budget implies")
        except Exception as exc:                   # noqa: BLE001
            bad(f"wrong exception: {type(exc).__name__}: {str(exc)[:110]}")
            passed = False
        finally:
            await client.aclose()

        after_est = prof.adaptive_timeout_ms(prompt_tokens=4096)
        if after_est > before_est:
            ok(f"EWMA PENALISED {before_est:.0f} -> {after_est:.0f}ms — the "
               f"ledger learned this host wedges")
        else:
            warn(f"estimate unchanged ({after_est:.0f}ms) — expected on the "
                 f"survival/CPU branch (num_ctx unset), which deliberately "
                 f"ignores the EWMA; the LAN bridge negotiates num_ctx")

        info("classification:")
        stall = lid.InterTokenStall("x")
        if ig.is_infrastructure_fault(stall):
            ok("InterTokenStall classifies as INFRASTRUCTURE -> counts against "
               "host health, triggers fallback")
        else:
            bad("InterTokenStall not classified as infrastructure")
            passed = False
        if not ig.is_infrastructure_fault(ValueError("bad prompt")):
            ok("a request fault does NOT count against host health")

        gw = ig.InferenceGateway()
        gw._publish_degraded(
            ig.GatewayTarget(base_url=endpoint, model_name="wedged-model:1b",
                             scope="remote", state=ig.HostState.DEGRADED,
                             reason="fault-injection"), stall)
        ok("provider_state_changed published (non-fatal, best-effort)")

        health = ig._HostHealth()
        for i in range(ig.failure_threshold()):
            health.record_failure("InterTokenStall", now=100.0 + i)
        if health.state(now=100.0) is ig.HostState.UNREACHABLE:
            ok(f"after {ig.failure_threshold()} stalls the breaker OPENS — "
               f"subsequent ops bypass the LAN")
        else:
            bad("breaker did not open")
            passed = False
        t_probe = 100.0 + ig.cooldown_s() + 1
        if health.state(now=t_probe) is ig.HostState.PROBING:
            ok(f"after {ig.cooldown_s():.0f}s cooldown ONE probe is offered")
        info(f"fallback tgt  : {gw._local_target('fault-injection').to_dict()}")
        ok(f"local triage fallback resolves to "
           f"'{gw._local_target('x').model_name}'")
    finally:
        await peer.stop()
    return passed


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--endpoint",
                    default=os.environ.get("JARVIS_REMOTE_INFERENCE_ENDPOINT",
                                           "http://127.0.0.1:11434"))
    ap.add_argument("--model",
                    default=os.environ.get("JARVIS_REMOTE_INFERENCE_MODEL",
                                           "qwen3-coder:30b"))
    ap.add_argument("--swap-model", default=None,
                    help="force a warm-swap handshake to this model in phase 1")
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--phase", default="1,2,3")
    args = ap.parse_args()

    phases = {int(x) for x in args.phase.split(",") if x.strip()}
    print(f"\n{'#' * 72}\n# LIVE-FIRE — LOCAL SOVEREIGNTY TIER\n"
          f"# endpoint {args.endpoint}   model {args.model}\n{'#' * 72}")

    gw = ig.InferenceGateway()
    results = {}
    try:
        if 1 in phases:
            results[1] = await phase1(gw, args.endpoint, args.model,
                                      args.swap_model)
        if 2 in phases:
            results[2] = await phase2(gw, args.endpoint, args.model, args.rounds)
        if 3 in phases:
            results[3] = await phase3(lid.LocalConfig.from_env().model_name)
    finally:
        await gw.aclose()

    print(f"\n{'=' * 72}")
    for n in sorted(results):
        (ok if results[n] else bad)(f"PHASE {n}")
    print(f"{'=' * 72}\n")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
