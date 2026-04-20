"""Minimal O+V smoke harness — real infra, zero ceremony.

Verifies Ouroboros + Venom slices without the 6-layer battle-test boot.
Where ``harness.py`` spins up 7 CommProtocol transports + SerpentFlow TUI
+ Controller + sensors, this entry point builds **only** the
GovernedOrchestrator + SubagentOrchestrator assembly and exercises a
specific hook against real subagent infrastructure.

Design principle: every slice verification should reuse this smoke
harness and add a `check_*()` function here. We pay the 6-layer boot
cost when we want to see the live organism; we pay ~0 seconds when we
want to prove a mechanical invariant.

Checks shipped:

  * review_shadow — post-VALIDATE REVIEW shadow hook (Slice 1a).
    Runs ``_run_review_shadow`` against the real ``SubagentOrchestrator``
    + real ``AgenticReviewSubagent``, verifies the ``[REVIEW-SHADOW]``
    telemetry line fires with the expected structure.

Usage:

  python3 -m backend.core.ouroboros.battle_test.ov_smoke --check review_shadow
  python3 -m backend.core.ouroboros.battle_test.ov_smoke --check all

Exit code 0 on all checks passing, 1 on any failure.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Dict, List, Tuple

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ===========================================================================
# Test-infra helpers
# ===========================================================================

class _LineCaptureHandler(logging.Handler):
    """Capture every emitted log record (formatted) for assertion."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: List[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(record.getMessage())
        except Exception:
            pass

    def matching(self, needle: str) -> List[str]:
        return [ln for ln in self.lines if needle in ln]


def _attach_capture() -> _LineCaptureHandler:
    handler = _LineCaptureHandler()
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    return handler


def _detach_capture(handler: _LineCaptureHandler) -> None:
    logging.getLogger().removeHandler(handler)


def _build_orchestrator() -> Any:
    """Construct a GovernedOrchestrator with the minimum fields the
    REVIEW shadow hook needs. Bypasses ``__init__`` because the real
    constructor wires half the governance stack — we only need
    ``_config.project_root`` and ``_subagent_orchestrator``.
    """
    from backend.core.ouroboros.governance.orchestrator import GovernedOrchestrator

    inst = object.__new__(GovernedOrchestrator)
    inst._config = SimpleNamespace(project_root=REPO_ROOT)
    inst._subagent_orchestrator = None
    return inst


def _build_subagent_orchestrator() -> Any:
    """Build a real ``SubagentOrchestrator`` with all four Phase 1/B factories.

    Uses the default comm + ledger sinks (LoggerCommSink +
    InMemoryLedgerSink) so telemetry flows through the standard Python
    logging pipeline. No TUI transports, no Langfuse, no VoiceNarrator.
    """
    from backend.core.ouroboros.governance.subagent_orchestrator import (
        SubagentOrchestrator,
    )
    from backend.core.ouroboros.governance.agentic_subagent import (
        build_default_explore_factory,
    )
    from backend.core.ouroboros.governance.agentic_review_subagent import (
        build_default_review_factory,
    )
    from backend.core.ouroboros.governance.agentic_plan_subagent import (
        build_default_plan_factory,
    )
    from backend.core.ouroboros.governance.agentic_general_subagent import (
        build_default_general_factory,
    )

    return SubagentOrchestrator(
        explore_factory=build_default_explore_factory(REPO_ROOT),
        review_factory=build_default_review_factory(REPO_ROOT),
        plan_factory=build_default_plan_factory(REPO_ROOT),
        general_factory=build_default_general_factory(REPO_ROOT),
    )


# ===========================================================================
# CHECK: review_shadow
# ===========================================================================

async def check_review_shadow() -> Tuple[bool, str]:
    """Exercise ``_run_review_shadow`` against real subagent infra.

    Builds a synthetic candidate against a real tempfile, fires the hook
    under ``JARVIS_REVIEW_SUBAGENT_SHADOW=true``, asserts the
    ``[REVIEW-SHADOW]`` telemetry line fires with the expected fields.
    Returns (pass: bool, details: str).
    """
    prev_flag = os.environ.get("JARVIS_REVIEW_SUBAGENT_SHADOW")
    os.environ["JARVIS_REVIEW_SUBAGENT_SHADOW"] = "true"
    handler = _attach_capture()

    try:
        scratch_root = Path(tempfile.mkdtemp(prefix="ov_smoke_review_"))
        file_path = scratch_root / "hello.py"
        file_path.write_text("def greet():\n    return 'old'\n")

        orch = _build_orchestrator()
        orch._config = SimpleNamespace(project_root=scratch_root)
        orch.set_subagent_orchestrator(_build_subagent_orchestrator())

        candidate = {
            "file_path": "hello.py",
            "full_content": "def greet():\n    return 'new'\n",
        }
        ctx = SimpleNamespace(
            op_id="op-smoke-review-001",
            description="ov_smoke: review_shadow check",
        )

        result = await orch._run_review_shadow(ctx, candidate)
        if result is not None:
            return False, f"hook returned non-None ({result!r}); observer must return None"

        shadow_lines = handler.matching("[REVIEW-SHADOW]")
        if len(shadow_lines) != 1:
            return False, (
                f"expected exactly one [REVIEW-SHADOW] line, got "
                f"{len(shadow_lines)}: {shadow_lines!r}"
            )

        line = shadow_lines[0]
        expected_tokens = [
            "op=op-smoke-review-001",
            "files_reviewed=1",
            "observer",
            "FSM proceeds regardless",
        ]
        missing = [tok for tok in expected_tokens if tok not in line]
        if missing:
            return False, (
                f"[REVIEW-SHADOW] line missing expected tokens {missing!r}: {line!r}"
            )

        aggregate = ""
        for chunk in line.split():
            if chunk.startswith("aggregate="):
                aggregate = chunk[len("aggregate="):].rstrip()
        if aggregate not in {"APPROVE", "APPROVE_WITH_RESERVATIONS", "REJECT"}:
            return False, (
                f"[REVIEW-SHADOW] aggregate={aggregate!r} not in known verdict set"
            )

        return True, f"PASS — aggregate={aggregate}"
    finally:
        _detach_capture(handler)
        if prev_flag is None:
            os.environ.pop("JARVIS_REVIEW_SUBAGENT_SHADOW", None)
        else:
            os.environ["JARVIS_REVIEW_SUBAGENT_SHADOW"] = prev_flag


# ===========================================================================
# CHECK: webhook_routing
# ===========================================================================

async def check_webhook_routing() -> Tuple[bool, str]:
    """Exercise GitHubIssueSensor.ingest_webhook against a real sensor +
    real ``make_envelope``, routed through a spy router. Proves the Slice
    1 mechanism (gap #4 migration) without starting the HTTP server:
    a webhook-shaped payload produces the same envelope shape as the
    poll path.

    Returns (pass, detail).
    """
    prev_flag = os.environ.get("JARVIS_GITHUB_WEBHOOK_ENABLED")
    os.environ["JARVIS_GITHUB_WEBHOOK_ENABLED"] = "true"

    try:
        from backend.core.ouroboros.governance.intake.sensors.github_issue_sensor import (
            GitHubIssueSensor,
        )

        class _SpyRouter:
            def __init__(self) -> None:
                self.envelopes: List[Any] = []

            async def ingest(self, envelope: Any) -> str:
                self.envelopes.append(envelope)
                return "enqueued"

        router = _SpyRouter()
        sensor = GitHubIssueSensor(
            repo="jarvis",
            router=router,
            poll_interval_s=3600.0,
            repos=(
                ("jarvis", "drussell23/JARVIS-AI-Agent", "backend/"),
            ),
        )

        if not sensor._webhook_mode:
            return False, (
                "webhook_mode did not activate under "
                "JARVIS_GITHUB_WEBHOOK_ENABLED=true"
            )

        payload = {
            "action": "opened",
            "issue": {
                "number": 9001,
                "title": "Bug: ov_smoke integration test",
                "body": "Traceback: simulated for smoke test.",
                "labels": [{"name": "bug"}],
                "created_at": "2026-04-19T22:30:00Z",
                "html_url": "https://github.com/drussell23/JARVIS-AI-Agent/issues/9001",
            },
            "repository": {"full_name": "drussell23/JARVIS-AI-Agent"},
        }
        emitted = await sensor.ingest_webhook(payload)

        if emitted is not True:
            return False, f"expected True return, got {emitted!r}"
        if len(router.envelopes) != 1:
            return False, (
                f"expected 1 envelope on router, got {len(router.envelopes)}"
            )
        env = router.envelopes[0]

        if env.source != "github_issue":
            return False, (
                f"envelope.source must be 'github_issue' (poll-path parity), "
                f"got {env.source!r}"
            )
        if env.evidence.get("via") != "webhook":
            return False, (
                f"envelope.evidence.via must be 'webhook', "
                f"got {env.evidence.get('via')!r}"
            )
        if env.evidence.get("issue_number") != 9001:
            return False, (
                f"envelope.evidence.issue_number mismatch: "
                f"got {env.evidence.get('issue_number')!r}"
            )
        if env.urgency != "high":  # 'bug' label classifies to 'high'
            return False, (
                f"envelope.urgency expected 'high' (bug label), "
                f"got {env.urgency!r}"
            )

        return True, (
            f"PASS — source={env.source} urgency={env.urgency} "
            f"issue_number={env.evidence.get('issue_number')} "
            f"webhook_mode={sensor._webhook_mode}"
        )
    finally:
        if prev_flag is None:
            os.environ.pop("JARVIS_GITHUB_WEBHOOK_ENABLED", None)
        else:
            os.environ["JARVIS_GITHUB_WEBHOOK_ENABLED"] = prev_flag


# ===========================================================================
# CHECK: webhook_http_activation
# ===========================================================================

async def check_webhook_http_activation() -> Tuple[bool, str]:
    """Full gap #4 proof: real HTTP POST on ephemeral port -> sensor -> router.

    Slice 2 activation proof. Spins up a real ``EventChannelServer`` on
    an ephemeral port, POSTs a genuine GitHub ``issues`` webhook payload,
    and verifies the envelope lands on the router with
    ``source='github_issue'`` — the same shape the poll path produces.

    Zero polling, zero ceremony, pure push.
    """
    import json
    try:
        import aiohttp
    except ImportError:
        return False, "aiohttp not available"

    prev_flag = os.environ.get("JARVIS_GITHUB_WEBHOOK_ENABLED")
    prev_ch_flag = os.environ.get("JARVIS_EVENT_CHANNELS_ENABLED")
    os.environ["JARVIS_GITHUB_WEBHOOK_ENABLED"] = "true"
    os.environ["JARVIS_EVENT_CHANNELS_ENABLED"] = "true"

    try:
        from backend.core.ouroboros.governance.event_channel import EventChannelServer
        from backend.core.ouroboros.governance.intake.sensors.github_issue_sensor import (
            GitHubIssueSensor,
        )

        class _SpyRouter:
            def __init__(self) -> None:
                self.envelopes: List[Any] = []

            async def ingest(self, envelope: Any) -> str:
                self.envelopes.append(envelope)
                return "enqueued"

        router = _SpyRouter()
        sensor = GitHubIssueSensor(
            repo="jarvis",
            router=router,
            poll_interval_s=3600.0,
            repos=(("jarvis", "drussell23/JARVIS-AI-Agent", "backend/"),),
        )
        server = EventChannelServer(
            router=router,
            port=0,
            host="127.0.0.1",
            github_issue_sensor=sensor,
        )

        await server.start()
        try:
            # Read the OS-assigned port from the bound socket.
            site = server._site
            if site is None:
                return False, "server failed to start (site is None)"
            sock = site._server.sockets[0]  # type: ignore[attr-defined]
            port = int(sock.getsockname()[1])

            payload = {
                "action": "opened",
                "issue": {
                    "number": 99001,
                    "title": "Bug: ov_smoke http activation",
                    "body": "Traceback: integration probe.",
                    "labels": [{"name": "bug"}],
                    "created_at": "2026-04-19T22:00:00Z",
                    "html_url": "https://github.com/drussell23/JARVIS-AI-Agent/issues/99001",
                },
                "repository": {"full_name": "drussell23/JARVIS-AI-Agent"},
            }
            headers = {
                "Content-Type": "application/json",
                "X-GitHub-Event": "issues",
            }

            async with aiohttp.ClientSession() as client:
                async with client.post(
                    f"http://127.0.0.1:{port}/webhook/github",
                    data=json.dumps(payload),
                    headers=headers,
                ) as resp:
                    if resp.status != 200:
                        return False, f"HTTP POST returned {resp.status}"
                # Also probe /channel/health for observability sanity.
                async with client.get(
                    f"http://127.0.0.1:{port}/channel/health",
                ) as hresp:
                    if hresp.status != 200:
                        return False, f"health endpoint returned {hresp.status}"
                    health = await hresp.json()

            if len(router.envelopes) != 1:
                return False, (
                    f"expected 1 envelope after HTTP POST, got "
                    f"{len(router.envelopes)}"
                )
            env = router.envelopes[0]
            if env.source != "github_issue":
                return False, (
                    f"envelope.source must be 'github_issue', got {env.source!r}"
                )
            if env.evidence.get("via") != "webhook":
                return False, (
                    f"envelope.evidence.via must be 'webhook', got "
                    f"{env.evidence.get('via')!r}"
                )

            ghis = health.get("github_issue_sensor", {})
            if not ghis.get("webhook_mode"):
                return False, (
                    "health.github_issue_sensor.webhook_mode must be True"
                )
            if ghis.get("webhooks_emitted") != 1:
                return False, (
                    f"health.webhooks_emitted expected 1, "
                    f"got {ghis.get('webhooks_emitted')}"
                )

            return True, (
                f"PASS — HTTP round-trip source={env.source} port={port} "
                f"webhooks_emitted={ghis.get('webhooks_emitted')}"
            )
        finally:
            await server.stop()
    finally:
        for key, prev in (
            ("JARVIS_GITHUB_WEBHOOK_ENABLED", prev_flag),
            ("JARVIS_EVENT_CHANNELS_ENABLED", prev_ch_flag),
        ):
            if prev is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev


# ===========================================================================
# Registry
# ===========================================================================

CHECKS: Dict[str, Callable[[], Awaitable[Tuple[bool, str]]]] = {
    "review_shadow": check_review_shadow,
    "webhook_routing": check_webhook_routing,
    "webhook_http_activation": check_webhook_http_activation,
}


# ===========================================================================
# CLI
# ===========================================================================

async def _run(selected: List[str]) -> int:
    results: List[Tuple[str, bool, str]] = []
    for name in selected:
        fn = CHECKS.get(name)
        if fn is None:
            results.append((name, False, "unknown check"))
            continue
        try:
            ok, detail = await fn()
        except Exception as exc:
            ok, detail = False, f"exception: {type(exc).__name__}: {exc}"
        results.append((name, ok, detail))

    print()
    print("=" * 64)
    print("O+V smoke results")
    print("=" * 64)
    all_pass = True
    for name, ok, detail in results:
        flag = "PASS" if ok else "FAIL"
        print(f"  [{flag}] {name:20s} — {detail}")
        if not ok:
            all_pass = False
    print("=" * 64)
    return 0 if all_pass else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="ov_smoke",
        description=(
            "Minimal O+V smoke harness. Real subagent infra, zero "
            "6-layer ceremony. Runs in seconds, no provider calls, "
            "no disk mutation outside a scratch tempdir."
        ),
    )
    parser.add_argument(
        "--check",
        default="all",
        help=(
            "Name of a check to run (e.g. review_shadow), or 'all' "
            "for every registered check. Default: all."
        ),
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List registered checks and exit.",
    )
    args = parser.parse_args()

    if args.list:
        print("Registered O+V smoke checks:")
        for name in sorted(CHECKS):
            print(f"  {name}")
        return 0

    selected = list(CHECKS.keys()) if args.check == "all" else [args.check]
    return asyncio.run(_run(selected))


if __name__ == "__main__":
    sys.exit(main())
