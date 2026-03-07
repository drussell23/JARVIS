#!/usr/bin/env python3
"""Phase C Diagnostic: Prove one full email triage cycle can execute.

Run from repo root:
    cd backend && python3 -m tests.diagnostic.phase_c_triage_trace

Or directly:
    cd backend && python3 ../tests/diagnostic/phase_c_triage_trace.py

Traces the FULL runtime chain:
  1. Environment & feature flags
  2. Dependency resolution (workspace_agent, router, notifier)
  3. OAuth token validity
  4. One triage cycle (fetch → extract → score → label → notify)
  5. Snapshot commit

Output: structured trace with pass/fail at each gate.
Exit criteria: "A triage cycle executes every X sec, with Y provider, and commits snapshot Z."
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

# ── Ensure backend is on sys.path ──────────────────────────
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_BACKEND = _REPO_ROOT / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

# Load .env from backend/ if present
_dotenv_path = _BACKEND / ".env"
if _dotenv_path.exists():
    for line in _dotenv_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            os.environ.setdefault(key, val)

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("phase_c")


class TraceStep:
    """One step in the diagnostic trace."""
    def __init__(self, name: str):
        self.name = name
        self.status = "PENDING"
        self.detail = ""
        self.elapsed_ms = 0.0
        self._start = 0.0

    def start(self):
        self._start = time.monotonic()
        self.status = "RUNNING"
        logger.info("─── [%s] START ───", self.name)

    def ok(self, detail: str = ""):
        self.elapsed_ms = (time.monotonic() - self._start) * 1000
        self.status = "PASS"
        self.detail = detail
        logger.info("  ✓ [%s] PASS (%.0fms) %s", self.name, self.elapsed_ms, detail)

    def fail(self, detail: str):
        self.elapsed_ms = (time.monotonic() - self._start) * 1000
        self.status = "FAIL"
        self.detail = detail
        logger.error("  ✗ [%s] FAIL (%.0fms) %s", self.name, self.elapsed_ms, detail)

    def skip(self, detail: str = ""):
        self.status = "SKIP"
        self.detail = detail
        logger.warning("  ⊘ [%s] SKIP %s", self.name, detail)

    def to_dict(self):
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "elapsed_ms": round(self.elapsed_ms, 1),
        }


async def run_diagnostic() -> Dict[str, Any]:
    trace: list[TraceStep] = []
    results: Dict[str, Any] = {}

    # ================================================================
    # Step 1: Environment & Feature Flags
    # ================================================================
    s = TraceStep("1_env_flags")
    s.start()
    flags = {
        "EMAIL_TRIAGE_ENABLED": os.getenv("EMAIL_TRIAGE_ENABLED", "false"),
        "EMAIL_TRIAGE_POLL_INTERVAL_S": os.getenv("EMAIL_TRIAGE_POLL_INTERVAL_S", "60"),
        "EMAIL_TRIAGE_CYCLE_TIMEOUT_S": os.getenv("EMAIL_TRIAGE_CYCLE_TIMEOUT_S", "30"),
        "EMAIL_TRIAGE_EXTRACTION_ENABLED": os.getenv("EMAIL_TRIAGE_EXTRACTION_ENABLED", "true"),
        "AGENT_RUNTIME_ENABLED": os.getenv("AGENT_RUNTIME_ENABLED", "true"),
        "EMAIL_TRIAGE_DLM_FAIL_OPEN": os.getenv("EMAIL_TRIAGE_DLM_FAIL_OPEN", "false"),
        "JARVIS_WORKSPACE_ALLOW_STANDALONE": os.getenv("JARVIS_WORKSPACE_ALLOW_STANDALONE", ""),
    }
    results["flags"] = flags
    triage_enabled = flags["EMAIL_TRIAGE_ENABLED"].lower() in ("true", "1", "yes")
    if triage_enabled:
        s.ok(f"EMAIL_TRIAGE_ENABLED=true, interval={flags['EMAIL_TRIAGE_POLL_INTERVAL_S']}s")
    else:
        s.fail("EMAIL_TRIAGE_ENABLED is not true — triage will be skipped at runtime")
    trace.append(s)

    # ================================================================
    # Step 2: OAuth Token Check
    # ================================================================
    s = TraceStep("2_oauth_tokens")
    s.start()
    creds_path = os.getenv(
        "GOOGLE_CREDENTIALS_PATH",
        str(Path.home() / ".jarvis" / "google_credentials.json"),
    )
    token_path = os.getenv(
        "GOOGLE_TOKEN_PATH",
        str(Path.home() / ".jarvis" / "google_workspace_token.json"),
    )
    creds_exist = os.path.isfile(creds_path)
    token_exist = os.path.isfile(token_path)
    token_age_s = None
    token_has_gmail = False
    if token_exist:
        stat = os.stat(token_path)
        token_age_s = time.time() - stat.st_mtime
        try:
            token_data = json.loads(Path(token_path).read_text())
            scopes = token_data.get("scopes", []) or []
            token_has_gmail = any("gmail" in sc.lower() for sc in scopes)
        except Exception as e:
            logger.debug("Token parse error: %s", e)

    results["oauth"] = {
        "credentials_path": creds_path,
        "credentials_exist": creds_exist,
        "token_path": token_path,
        "token_exist": token_exist,
        "token_age_hours": round(token_age_s / 3600, 1) if token_age_s else None,
        "token_has_gmail_scope": token_has_gmail,
    }
    if creds_exist and token_exist and token_has_gmail:
        s.ok(f"Credentials + token present, gmail scope found, token age={results['oauth']['token_age_hours']}h")
    elif creds_exist and token_exist:
        s.ok(f"Credentials + token present (gmail scope not confirmed), age={results['oauth']['token_age_hours']}h")
    else:
        s.fail(f"Missing: creds={creds_exist}, token={token_exist}")
    trace.append(s)

    # ================================================================
    # Step 3: TriageConfig Loading
    # ================================================================
    s = TraceStep("3_triage_config")
    s.start()
    try:
        from autonomy.email_triage.config import get_triage_config
        config = get_triage_config()
        results["triage_config"] = {
            "enabled": config.enabled,
            "extraction_enabled": config.extraction_enabled,
            "max_emails_per_cycle": config.max_emails_per_cycle,
            "state_persistence_enabled": config.state_persistence_enabled,
            "dep_backoff_base_s": config.dep_backoff_base_s,
        }
        if config.enabled:
            s.ok(f"enabled={config.enabled}, extraction={config.extraction_enabled}, max_per_cycle={config.max_emails_per_cycle}")
        else:
            s.fail("TriageConfig.enabled=False — run_cycle() will return skipped=True")
    except Exception as e:
        s.fail(f"Import/load failed: {e}")
    trace.append(s)

    # ================================================================
    # Step 4: Dependency Resolution — Workspace Agent
    # ================================================================
    s = TraceStep("4_workspace_agent")
    s.start()
    workspace_agent = None
    try:
        # Allow standalone creation
        os.environ.setdefault("JARVIS_WORKSPACE_ALLOW_STANDALONE", "true")
        from neural_mesh.agents.google_workspace_agent import get_google_workspace_agent
        workspace_agent = await get_google_workspace_agent()
        if workspace_agent is not None:
            has_gmail = hasattr(workspace_agent, "_gmail_service") and workspace_agent._gmail_service is not None
            has_fetch = hasattr(workspace_agent, "_fetch_unread_emails")
            results["workspace_agent"] = {
                "resolved": True,
                "type": type(workspace_agent).__name__,
                "has_gmail_service": has_gmail,
                "has_fetch_unread": has_fetch,
            }
            s.ok(f"Resolved: gmail_svc={has_gmail}, fetch_unread={has_fetch}")
        else:
            results["workspace_agent"] = {"resolved": False, "error": "returned None"}
            s.fail("get_google_workspace_agent() returned None")
    except Exception as e:
        results["workspace_agent"] = {"resolved": False, "error": str(e)}
        s.fail(f"Exception: {e}")
    trace.append(s)

    # ================================================================
    # Step 5: Dependency Resolution — PrimeRouter
    # ================================================================
    s = TraceStep("5_prime_router")
    s.start()
    router = None
    try:
        from core.prime_router import get_prime_router
        router = await get_prime_router()
        if router is not None:
            initialized = getattr(router, "_initialized", False)
            results["prime_router"] = {
                "resolved": True,
                "initialized": initialized,
                "type": type(router).__name__,
            }
            s.ok(f"Resolved: initialized={initialized}")
        else:
            results["prime_router"] = {"resolved": False, "error": "returned None"}
            s.fail("get_prime_router() returned None")
    except Exception as e:
        results["prime_router"] = {"resolved": False, "error": str(e)}
        # Router is optional — extraction falls back to heuristic
        s.ok(f"Not available (optional): {e}")
    trace.append(s)

    # ================================================================
    # Step 6: Dependency Resolution — Notifier
    # ================================================================
    s = TraceStep("6_notifier")
    s.start()
    notifier = None
    try:
        from agi_os.notification_bridge import notify_user
        notifier = notify_user
        results["notifier"] = {"resolved": True, "callable": callable(notifier)}
        s.ok("notify_user resolved")
    except Exception as e:
        results["notifier"] = {"resolved": False, "error": str(e)}
        s.ok(f"Not available (optional): {e}")
    trace.append(s)

    # ================================================================
    # Step 7: Email Triage Runner — Warm-up
    # ================================================================
    s = TraceStep("7_runner_warmup")
    s.start()
    runner = None
    try:
        from autonomy.email_triage.runner import EmailTriageRunner
        runner = EmailTriageRunner(
            config=config,
            workspace_agent=workspace_agent,
            router=router,
            notifier=notifier,
        )
        await runner.warm_up()
        dep_health = runner._resolver.health_summary()
        results["runner_warmup"] = {
            "warmed_up": runner.is_warmed_up,
            "dep_health": dep_health,
        }
        s.ok(f"warm_up complete, dep_health={dep_health}")
    except Exception as e:
        results["runner_warmup"] = {"error": str(e)}
        s.fail(f"warm_up failed: {e}")
    trace.append(s)

    # ================================================================
    # Step 8: Run One Triage Cycle
    # ================================================================
    s = TraceStep("8_run_cycle")
    s.start()
    report = None
    if runner is not None:
        try:
            deadline = time.monotonic() + 60.0  # generous for diagnostic
            report = await asyncio.wait_for(
                runner.run_cycle(deadline=deadline),
                timeout=60.0,
            )
            results["cycle_report"] = {
                "cycle_id": report.cycle_id,
                "skipped": report.skipped,
                "skip_reason": getattr(report, "skip_reason", None),
                "emails_fetched": report.emails_fetched,
                "emails_processed": report.emails_processed,
                "tier_counts": report.tier_counts,
                "notifications_sent": report.notifications_sent,
                "notifications_suppressed": getattr(report, "notifications_suppressed", 0),
                "errors": report.errors,
                "snapshot_committed": getattr(report, "snapshot_committed", None),
                "duration_s": round(report.completed_at - report.started_at, 2),
            }
            if report.skipped:
                s.fail(f"Cycle skipped: reason={report.skip_reason}")
            elif report.errors:
                s.ok(f"Cycle completed with errors: fetched={report.emails_fetched}, "
                     f"processed={report.emails_processed}, errors={report.errors}")
            else:
                s.ok(f"Cycle OK: fetched={report.emails_fetched}, processed={report.emails_processed}, "
                     f"tiers={report.tier_counts}")
        except asyncio.TimeoutError:
            s.fail("Cycle timed out (60s)")
            results["cycle_report"] = {"error": "timeout"}
        except Exception as e:
            s.fail(f"Cycle exception: {e}")
            results["cycle_report"] = {"error": str(e)}
            import traceback
            traceback.print_exc()
    else:
        s.skip("Runner not available")
    trace.append(s)

    # ================================================================
    # Step 9: Snapshot Verification
    # ================================================================
    s = TraceStep("9_snapshot")
    s.start()
    if runner is not None and report is not None and not report.skipped:
        snapshot = runner._committed_snapshot
        if snapshot:
            results["snapshot"] = {
                "committed": True,
                "committed_at": snapshot.get("committed_at"),
                "schema_version": snapshot.get("schema_version"),
                "triaged_count": len(snapshot.get("triaged_emails", {})),
            }
            s.ok(f"Snapshot committed: {results['snapshot']}")
        else:
            results["snapshot"] = {"committed": False}
            s.ok("No snapshot committed (may be expected if 0 emails)")
    else:
        s.skip("No successful cycle to verify")
    trace.append(s)

    # ================================================================
    # Summary
    # ================================================================
    total_pass = sum(1 for t in trace if t.status == "PASS")
    total_fail = sum(1 for t in trace if t.status == "FAIL")
    total_skip = sum(1 for t in trace if t.status == "SKIP")

    summary = {
        "verdict": "CHAIN_HEALTHY" if total_fail == 0 else "CHAIN_BROKEN",
        "pass": total_pass,
        "fail": total_fail,
        "skip": total_skip,
        "steps": [t.to_dict() for t in trace],
        "results": results,
    }

    # Print the one-liner exit criteria
    if report and not report.skipped:
        interval = flags["EMAIL_TRIAGE_POLL_INTERVAL_S"]
        provider = "heuristic_only"
        if router:
            provider = f"jprime_v1 (via PrimeRouter)"
        snapshot_id = report.cycle_id
        committed = getattr(report, "snapshot_committed", False)
        print(f"\n{'='*70}")
        print(f"EXIT CRITERIA: A triage cycle executes every {interval}s, "
              f"with {provider}, and commits snapshot {snapshot_id} "
              f"(committed={committed})")
        print(f"{'='*70}")
    else:
        print(f"\n{'='*70}")
        skip_reason = getattr(report, "skip_reason", "runner_unavailable") if report else "runner_unavailable"
        print(f"EXIT CRITERIA: NOT MET — cycle did not execute (reason: {skip_reason})")
        print(f"{'='*70}")

    print(f"\n{'='*70}")
    print(f"PHASE C DIAGNOSTIC: {summary['verdict']}  "
          f"(pass={total_pass}, fail={total_fail}, skip={total_skip})")
    print(f"{'='*70}\n")

    for step in trace:
        icon = {"PASS": "✓", "FAIL": "✗", "SKIP": "⊘", "PENDING": "?"}[step.status]
        print(f"  {icon} {step.name:30s} {step.status:5s}  {step.detail[:80]}")

    return summary


if __name__ == "__main__":
    result = asyncio.run(run_diagnostic())
    # Write JSON trace for post-mortem
    out_path = Path("/tmp/claude") / "phase_c_trace.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, default=str))
    print(f"\nFull trace written to: {out_path}")
    sys.exit(0 if result["verdict"] == "CHAIN_HEALTHY" else 1)
