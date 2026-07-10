#!/usr/bin/env python3
"""Phase 0 — LongCat Anthropic-dialect live endpoint probe (gate for Path A).

Spec: docs/superpowers/backlog/2026-07-10-longcat-resilience-stub.md

Decides, against the LIVE wire (Mandate 1 — no mock harness, no simulated
responses), whether LongCat's Anthropic-compatible surface is faithful enough
to fill the unfunded Claude Tier-1 slot via the existing ``ClaudeProvider``
seam (Path A), or whether the build must structurally pivot to Path B
(OpenRouter / OpenAI dialect on the DW seam).

Mandate mapping
---------------
1. **Root-Cause Only** — every sub-probe hits the live endpoint through the
   real Anthropic SDK transport. Missing credentials / unreachable host is an
   explicit BLOCKED verdict (exit 3), never a simulated PASS.
2. **Architectural Purity** — the client is constructed EXCLUSIVELY through
   ``aegis_provider_bridge.make_async_anthropic_client()`` (the single
   canonical AsyncAnthropic factory); ``base_url`` + the credential env NAME
   are resolved from ``brain_selection_policy.yaml``
   (``hosted_provider_candidates.longcat``). No endpoint or env-var string is
   hardcoded in this file.
3. **DRY** — attachment blocks come from the REAL
   ``providers._serialize_attachments`` (I7's only sanctioned egress path)
   fed a real ``op_context.Attachment``; no formatting arrays are duplicated
   here. NOTE — deliberate scope line: the probe does NOT instantiate the
   full ``ClaudeProvider``. Its only external transport seam (the Aegis env
   path) replaces ``api_key`` with a placeholder for daemon-side injection,
   so pointing it at LongCat would require hot-patching the private
   ``_client`` — exactly the interception Mandate 1 bans. The probe instead
   consumes the same factory + serializer + ``messages.create/stream`` SDK
   surface the provider itself calls; giving ``ClaudeProvider`` a
   policy-resolved ``base_url`` parameter is the Phase 2 build change.
4. **Bulletproof** — P2 targets ``feedback_claude_prefill_incompat``
   directly (prefill echo / drop / reorder), P3 asserts the full streaming
   event grammar incl. trailing tokens after ``message_stop``. Any mandatory
   anomaly fails the gate EXPLICITLY (exit 2) with a structured delta report
   for the Path B pivot.

Exit codes: 0 = PATH_A_VERIFIED · 2 = PATH_A_REJECTED_PIVOT_PATH_B ·
3 = BLOCKED (credentials / reachability / Aegis active — no verdict).
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

# Script-mode import bootstrap (the isomorphic-driver lesson: pytest-green
# imports still ModuleNotFoundError as a script — pin the repo root).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_POLICY_PATH = (
    _REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "brain_selection_policy.yaml"
)
_DEFAULT_REPORT = _REPO_ROOT / ".superpowers" / "sdd" / "longcat-phase0-report.json"

# 1x1 transparent PNG — probe fixture bytes for the REAL Attachment (the
# image CONTENT is irrelevant; the serializer contract + live vision
# capability answer are what P5 measures).
_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)

MANDATORY = ("wire_dialect", "prefill", "stream_grammar")


def _load_candidate_cfg() -> Dict[str, Any]:
    import yaml  # lazy — same dependency posture as the policy's own readers

    policy = yaml.safe_load(_POLICY_PATH.read_text(encoding="utf-8"))
    try:
        return policy["hosted_provider_candidates"]["longcat"]
    except (KeyError, TypeError) as exc:
        raise SystemExit(
            f"[Phase0] policy missing hosted_provider_candidates.longcat "
            f"({_POLICY_PATH}): {exc}"
        )


def _resolve_api_key(cfg: Dict[str, Any]) -> Optional[str]:
    # Env NAME comes from policy (Mandate 2 — no env strings in code).
    return os.environ.get(cfg["api_key_env"], "").strip() or None


def _aegis_is_active() -> bool:
    """The factory transparently hijacks base_url+api_key when Aegis is
    enabled — a probe routed through the Aegis daemon would measure the
    daemon, not LongCat. Refuse to run rather than silently mismeasure.

    Same module object the factory itself imports (aegis_provider_bridge:72)
    — if this import breaks, _make_client breaks identically, so there is no
    silent-False window where Aegis is active but undetected."""
    from backend.core.ouroboros.aegis import client as aegis_client
    return bool(aegis_client.is_enabled())


def _make_client(cfg: Dict[str, Any], api_key: str) -> Any:
    """Mandate 2: the ONLY client construction path — the canonical factory,
    base_url injected at the instantiation boundary from policy."""
    from backend.core.ouroboros.governance.aegis_provider_bridge import (
        make_async_anthropic_client,
    )
    return make_async_anthropic_client(
        api_key=api_key,
        base_url=cfg["endpoint"].rstrip("/"),
        max_retries=0,  # the probe measures the wire, not retry luck
    )


def _result(name: str, status: str, evidence: Any = None,
            delta: Optional[str] = None) -> Dict[str, Any]:
    return {"probe": name, "status": status, "evidence": evidence,
            "delta_for_path_b_pivot": delta}


# ---------------------------------------------------------------------------
# Sub-probes (each: live wire, bounded by asyncio.wait_for — Python 3.9 rule)
# ---------------------------------------------------------------------------

async def probe_wire_dialect(client: Any, model: str, timeout_s: float) -> Dict[str, Any]:
    """P1 — endpoint routing + auth + minimal Messages-dialect response shape."""
    import anthropic
    try:
        resp = await asyncio.wait_for(
            client.messages.create(
                model=model, max_tokens=32,
                messages=[{"role": "user", "content": "Reply with exactly: PONG"}],
            ),
            timeout=timeout_s,
        )
    except anthropic.AuthenticationError as exc:
        return _result("wire_dialect", "BLOCKED", str(exc),
                       "credential rejected — provision a valid key before re-running")
    except anthropic.NotFoundError as exc:
        return _result(
            "wire_dialect", "FAIL", str(exc),
            "404 at {endpoint}/v1/messages — the policy `endpoint` host-root is "
            "wrong or LongCat does not serve the Anthropic dialect there; fix "
            "policy endpoint or pivot to path_b_fallback",
        )
    except (anthropic.APIConnectionError, asyncio.TimeoutError) as exc:
        return _result("wire_dialect", "BLOCKED", repr(exc),
                       "host unreachable — network egress/allowlist or endpoint DNS")
    except anthropic.APIStatusError as exc:
        return _result("wire_dialect", "FAIL",
                       {"status": exc.status_code, "body": str(exc)[:400]},
                       "non-Anthropic-shaped error surface — dialect mismatch")

    anomalies: List[str] = []
    content = getattr(resp, "content", None)
    if not isinstance(content, list) or not content:
        anomalies.append(f"content is {type(content).__name__}, expected non-empty list")
    else:
        b0 = content[0]
        if getattr(b0, "type", None) != "text":
            anomalies.append(f"first block type={getattr(b0, 'type', None)!r}, expected 'text'")
    if getattr(resp, "stop_reason", None) not in (
        "end_turn", "max_tokens", "stop_sequence", "tool_use",
    ):
        anomalies.append(f"unknown stop_reason={getattr(resp, 'stop_reason', None)!r}")
    if getattr(resp, "usage", None) is None:
        anomalies.append("usage block absent — cost ledger cannot meter this lane")
    if anomalies:
        return _result("wire_dialect", "FAIL", anomalies,
                       "response envelope diverges from Anthropic Messages shape")
    return _result("wire_dialect", "PASS", {
        "model_echo": getattr(resp, "model", None),
        "stop_reason": resp.stop_reason,
        "usage": {"in": resp.usage.input_tokens, "out": resp.usage.output_tokens},
    })


async def probe_prefill(client: Any, model: str, timeout_s: float) -> Dict[str, Any]:
    """P2 — feedback_claude_prefill_incompat: trailing-assistant prefill must
    CONTINUE (no echo, no drop, no reorder). A silent mismatch here corrupts
    the candidate JSON the 11-phase FSM parses downstream."""
    prefill = '{"status":'
    try:
        resp = await asyncio.wait_for(
            client.messages.create(
                model=model, max_tokens=64,
                messages=[
                    {"role": "user", "content":
                     'Return a JSON object with a single key "status" whose '
                     'value is "ok". Output ONLY the JSON object.'},
                    {"role": "assistant", "content": prefill},
                ],
            ),
            timeout=timeout_s,
        )
    except Exception as exc:  # noqa: BLE001 — any refusal of the trailing turn IS the finding
        return _result(
            "prefill", "FAIL", repr(exc)[:400],
            "endpoint rejects trailing-assistant prefill outright — "
            "feedback_claude_prefill_incompat class; GENERATE prompt assembly "
            "cannot rely on prefill on this lane (pivot or per-brain branch)",
        )

    text = "".join(
        getattr(b, "text", "") for b in getattr(resp, "content", []) or []
    )
    if text.lstrip().startswith(prefill) or '"status":' in text[:24]:
        return _result(
            "prefill", "FAIL", {"continuation_head": text[:80]},
            "prefill ECHOED back in the completion (double-prefix) — "
            "concatenation would produce corrupt JSON; the exact "
            "feedback_claude_prefill_incompat failure",
        )
    try:
        parsed = json.loads(prefill + text)
    except ValueError:
        return _result(
            "prefill", "FAIL", {"prefill": prefill, "continuation": text[:120]},
            "prefill+continuation is not valid JSON — prefill buffer dropped "
            "or reordered during generation",
        )
    return _result("prefill", "PASS",
                   {"continuation": text[:80], "parsed": parsed})


async def probe_stream_grammar(client: Any, model: str, timeout_s: float) -> Dict[str, Any]:
    """P3 — streaming block boundaries + trailing-token discipline. The
    stream_renderer and the tool-loop both parse this grammar; malformed
    boundaries are the 'fracture the downstream state machine' class."""
    events: List[str] = []
    stream_text: List[str] = []
    anomalies: List[str] = []

    async def _consume() -> Any:
        async with client.messages.stream(
            model=model, max_tokens=128,
            messages=[{"role": "user",
                       "content": "Count from 1 to 10 as plain text."}],
        ) as stream:
            async for ev in stream:
                etype = getattr(ev, "type", "?")
                events.append(etype)
                if etype == "content_block_delta":
                    delta = getattr(ev, "delta", None)
                    stream_text.append(getattr(delta, "text", "") or "")
            return await stream.get_final_message()

    try:
        final = await asyncio.wait_for(_consume(), timeout=timeout_s)
    except Exception as exc:  # noqa: BLE001 — grammar break mid-stream IS the finding
        return _result("stream_grammar", "FAIL",
                       {"events_so_far": events[-12:], "error": repr(exc)[:400]},
                       "stream aborted / unparseable event mid-flight")

    if not events or events[0] != "message_start":
        anomalies.append(f"first event {events[:1]!r}, expected ['message_start']")
    if "message_stop" not in events:
        anomalies.append("no terminal message_stop")
    else:
        tail = events[events.index("message_stop") + 1:]
        if tail:
            anomalies.append(f"TRAILING events after message_stop: {tail!r}")
    if "content_block_start" not in events or "content_block_stop" not in events:
        anomalies.append("content block not bracketed by start/stop")
    else:
        cbs, cbe = events.index("content_block_start"), events.index("content_block_stop")
        stray = [i for i, e in enumerate(events)
                 if e == "content_block_delta" and not (cbs < i < cbe)]
        if stray:
            anomalies.append(f"deltas outside block bracket at indices {stray}")
    if "message_delta" not in events:
        anomalies.append("no message_delta (stop_reason never delivered on-stream)")
    joined = "".join(stream_text)
    final_text = "".join(getattr(b, "text", "") for b in final.content or [])
    if joined != final_text:
        anomalies.append(
            f"stream/final divergence: streamed {len(joined)}B != final "
            f"{len(final_text)}B — trailing tokens dropped or duplicated"
        )
    if anomalies:
        return _result("stream_grammar", "FAIL",
                       {"event_seq": events, "anomalies": anomalies},
                       "streaming grammar diverges from Anthropic SSE contract")
    return _result("stream_grammar", "PASS",
                   {"event_seq_len": len(events), "bytes": len(joined),
                    "stop_reason": final.stop_reason})


async def probe_stop_sequence(client: Any, model: str, timeout_s: float) -> Dict[str, Any]:
    """P4 (advisory) — stop_sequences honored with correct stop_reason."""
    try:
        resp = await asyncio.wait_for(
            client.messages.create(
                model=model, max_tokens=64, stop_sequences=["###END"],
                messages=[{"role": "user", "content":
                           "Write the word alpha, then ###END, then beta."}],
            ),
            timeout=timeout_s,
        )
    except Exception as exc:  # noqa: BLE001
        return _result("stop_sequence", "FAIL", repr(exc)[:200],
                       "stop_sequences parameter rejected")
    text = "".join(getattr(b, "text", "") for b in resp.content or [])
    if resp.stop_reason == "stop_sequence" and "###END" not in text:
        return _result("stop_sequence", "PASS",
                       {"stop_reason": resp.stop_reason, "text": text[:60]})
    if "###END" in text:
        return _result("stop_sequence", "FAIL",
                       {"stop_reason": resp.stop_reason, "text": text[:120]},
                       "stop marker leaked into completion — cut not applied")
    return _result("stop_sequence", "INCONCLUSIVE",
                   {"stop_reason": resp.stop_reason, "text": text[:60]},
                   "model never emitted the marker — re-run for signal")


async def probe_attachments(client: Any, model: str, timeout_s: float,
                            scratch: Path) -> Dict[str, Any]:
    """P5 (capability, non-fatal) — the REAL _serialize_attachments contract
    on this channel: real Attachment through the I7 egress path, then the
    produced Claude-native block on the live wire."""
    import hashlib

    from backend.core.ouroboros.governance.op_context import Attachment
    from backend.core.ouroboros.governance.providers import _serialize_attachments

    png = base64.b64decode(_TINY_PNG_B64)
    img = scratch / "phase0_probe.png"
    img.write_bytes(png)
    att = Attachment(
        kind="sensor_frame", image_path=str(img), mime_type="image/png",
        hash8=hashlib.sha256(png).hexdigest()[:8], ts=time.monotonic(),
    )
    # Duck ctx exercising the real purpose + route gates (visual_verify is a
    # sanctioned purpose; a BG route here would prove the strip gate instead).
    # The serializer duck-types ctx via getattr (attachments/provider_route);
    # a full OperationContext would drag op-ledger state into a wire probe.
    ctx = SimpleNamespace(attachments=[att], provider_route="immediate")
    blocks = _serialize_attachments(ctx, provider_kind="claude",  # type: ignore[arg-type]
                                    purpose="visual_verify")
    if not blocks or blocks[0].get("type") != "image" \
            or blocks[0].get("source", {}).get("type") != "base64":
        return _result("attachments", "FAIL", blocks,
                       "_serialize_attachments contract drift — fix locally "
                       "before any wire conclusion")
    try:
        resp = await asyncio.wait_for(
            client.messages.create(
                model=model, max_tokens=48,
                messages=[{"role": "user", "content":
                           blocks + [{"type": "text",
                                      "text": "One word: what color is this image?"}]}],
            ),
            timeout=timeout_s,
        )
        return _result("attachments", "PASS",
                       {"vision_supported": True,
                        "reply": "".join(getattr(b, "text", "")
                                         for b in resp.content or [])[:60]})
    except Exception as exc:  # noqa: BLE001 — capability answer, not gate failure
        return _result("attachments", "INCONCLUSIVE",
                       {"vision_supported": False, "error": repr(exc)[:300]},
                       "local serializer contract HELD; endpoint likely "
                       "text-only (fine — BG/SPEC strip attachments anyway)")


# ---------------------------------------------------------------------------

async def run(args: argparse.Namespace) -> int:
    cfg = _load_candidate_cfg()
    model = args.model or cfg["models"][0]["model_name"]
    report: Dict[str, Any] = {
        "phase": "longcat-phase0", "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "dialect": cfg["dialect"], "endpoint": cfg["endpoint"], "model": model,
        "probes": [], "verdict": None,
    }

    def finish(verdict: str, code: int) -> int:
        report["verdict"] = verdict
        args.report_path.parent.mkdir(parents=True, exist_ok=True)
        args.report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\n[Phase0] VERDICT: {verdict}")
        for p in report["probes"]:
            line = f"  {p['probe']:<14} {p['status']}"
            if p.get("delta_for_path_b_pivot"):
                line += f"  Δ {p['delta_for_path_b_pivot']}"
            print(line)
        print(f"[Phase0] report → {args.report_path}")
        return code

    if _aegis_is_active():
        report["probes"].append(_result(
            "preflight", "BLOCKED", "Aegis enabled",
            "the canonical factory would hijack base_url+key toward the Aegis "
            "daemon — disable Aegis for the probe run"))
        return finish("BLOCKED_AEGIS_ACTIVE", 3)

    api_key = _resolve_api_key(cfg)
    if not api_key:
        report["probes"].append(_result(
            "preflight", "BLOCKED",
            f"env {cfg['api_key_env']} unset/empty",
            "provision a key at the OFFICIAL vendor surface (see policy "
            "`vendor` comment), export it, re-run — the gate does NOT pass "
            "on simulation"))
        return finish("BLOCKED_NO_CREDENTIALS", 3)

    client = _make_client(cfg, api_key)
    try:
        report["probes"].append(await probe_wire_dialect(client, model, args.timeout_s))
        if report["probes"][-1]["status"] in ("BLOCKED",):
            return finish("BLOCKED_UNREACHABLE_OR_UNAUTHED", 3)
        if report["probes"][-1]["status"] == "FAIL":
            return finish("PATH_A_REJECTED_PIVOT_PATH_B", 2)
        report["probes"].append(await probe_prefill(client, model, args.timeout_s))
        report["probes"].append(await probe_stream_grammar(client, model, args.timeout_s))
        report["probes"].append(await probe_stop_sequence(client, model, args.timeout_s))
        if args.attachments:
            report["probes"].append(await probe_attachments(
                client, model, args.timeout_s, args.scratch))
    finally:
        close = getattr(client, "close", None)
        if close is not None:
            try:
                await close()
            except Exception:  # noqa: BLE001 — teardown is best-effort
                pass

    hard_fail = any(p["status"] == "FAIL" and p["probe"] in MANDATORY
                    for p in report["probes"])
    if hard_fail:
        return finish("PATH_A_REJECTED_PIVOT_PATH_B", 2)
    return finish("PATH_A_VERIFIED", 0)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Phase 0 — LongCat Anthropic-dialect live endpoint probe "
                    "(gate for Path A)")
    ap.add_argument("--model", default=None,
                    help="override policy model_name (default: first policy model)")
    ap.add_argument("--timeout-s", type=float, default=45.0)
    ap.add_argument("--report-path", type=Path, default=_DEFAULT_REPORT)
    ap.add_argument("--attachments", action="store_true",
                    help="include the P5 _serialize_attachments live capability probe")
    ap.add_argument("--scratch", type=Path,
                    default=Path(os.environ.get("TMPDIR", "/tmp")))
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
