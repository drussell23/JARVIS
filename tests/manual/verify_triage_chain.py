#!/usr/bin/env python3
"""End-to-end verification of the email triage chain.

Tests each component in isolation, then runs a full cycle.
Run from repo root:  python3 tests/manual/verify_triage_chain.py
"""

import asyncio
import json
import os
import sys
import time

# Ensure backend is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

# Load .env files — force-override for API keys so the correct
# project key is used (not Claude Code's session key).
for _env_path in [
    os.path.join(os.path.dirname(__file__), "..", "..", ".env"),
    os.path.join(os.path.dirname(__file__), "..", "..", "backend", ".env"),
]:
    if os.path.exists(_env_path):
        with open(_env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip()
                    # Force-set API keys, setdefault for everything else
                    if "API_KEY" in k or "TOKEN" in k or "PASSWORD" in k:
                        os.environ[k] = v
                    else:
                        os.environ.setdefault(k, v)


def _status(label, ok, detail=""):
    icon = "PASS" if ok else "FAIL"
    msg = f"  [{icon}] {label}"
    if detail:
        msg += f" -- {detail}"
    print(msg)
    return ok


async def main():
    print("\n" + "=" * 60)
    print("  JARVIS Email Triage Chain Verification")
    print("=" * 60 + "\n")

    all_ok = True

    # -- 1. Anthropic API key --
    print("[1/7] Anthropic API Key")
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    key_ok = api_key.startswith("sk-ant-api03-") and len(api_key) > 30
    if key_ok:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
            resp = client.messages.create(
                model="claude-sonnet-4-5-20250929",
                max_tokens=5,
                messages=[{"role": "user", "content": "Say OK"}],
            )
            key_ok = "ok" in resp.content[0].text.lower()
            _status("API key valid", key_ok, resp.content[0].text.strip())
        except Exception as e:
            key_ok = False
            _status("API key", False, str(e)[:100])
    else:
        _status("API key", False, "Missing or malformed")
    all_ok &= key_ok

    # -- 2. Gmail OAuth --
    print("\n[2/7] Gmail OAuth Token")
    token_path = os.path.expanduser("~/.jarvis/google_workspace_token.json")
    token_ok = os.path.exists(token_path)
    if token_ok:
        with open(token_path) as f:
            tok = json.load(f)
        has_refresh = bool(tok.get("refresh_token"))
        _status("Token file exists", True, token_path)
        _status("Has refresh_token", has_refresh)
        token_ok = has_refresh
    else:
        _status("Token file", False, f"Not found at {token_path}")
    all_ok &= token_ok

    # -- 3. Redis --
    print("\n[3/7] Redis")
    try:
        import redis
        r = redis.Redis(host="localhost", port=6379, socket_timeout=2)
        pong = r.ping()
        _status("Redis", pong, "PONG")
    except Exception as e:
        _status("Redis", False, str(e)[:100])
        all_ok = False

    # -- 4. GCP VM --
    print("\n[4/7] GCP VM (jarvis-prime-node)")
    try:
        proc = await asyncio.create_subprocess_exec(
            "gcloud", "compute", "instances", "describe",
            "jarvis-prime-node", "--zone=us-central1-a",
            "--project=jarvis-473803",
            "--format=value(status,networkInterfaces[0].accessConfigs[0].natIP)",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0:
            parts = stdout.decode().strip().split("\t")
            status = parts[0] if parts else "UNKNOWN"
            ip = parts[1] if len(parts) > 1 else "N/A"
            vm_ok = status == "RUNNING"
            _status("VM status", vm_ok, f"{status} @ {ip}")
        else:
            vm_ok = False
            _status("VM", False, stderr.decode().strip()[:100])
    except FileNotFoundError:
        vm_ok = False
        _status("VM", False, "gcloud CLI not found")
    all_ok &= vm_ok

    # -- 5. PrimeRouter --
    print("\n[5/7] PrimeRouter")
    try:
        from core.prime_router import PrimeRouter
        router = PrimeRouter.get_instance()
        _status("PrimeRouter singleton", True)

        route = router._decide_route("test prompt")
        _status("Routing decision", True, f"{route}")
    except Exception as e:
        _status("PrimeRouter", False, str(e)[:100])
        all_ok = False

    # -- 6. Email Triage Runner --
    print("\n[6/7] EmailTriageRunner")
    try:
        from autonomy.email_triage.runner import EmailTriageRunner
        from autonomy.email_triage.config import get_triage_config

        config = get_triage_config()
        _status("Config loaded", True,
                f"enabled={config.enabled}, extraction={config.extraction_enabled}, "
                f"max_per_cycle={config.max_emails_per_cycle}")

        runner = EmailTriageRunner.get_instance()
        _status("Runner singleton", True, f"id={runner._runner_id}")

        _status("EnvelopeFactory", runner._envelope_factory is not None)
        _status("HealthMonitor", runner._health_monitor is not None)
        _status("PolicyGate", runner._policy_gate is not None)

        print("  ... warming up (resolving deps, init ledger)...")
        await asyncio.wait_for(runner.warm_up(), timeout=30.0)
        _status("Warm-up complete", runner.is_warmed_up)
        _status("Commit ledger", runner._commit_ledger is not None,
                "ready" if runner._commit_ledger else "UNAVAILABLE")
    except Exception as e:
        _status("Runner", False, str(e)[:150])
        all_ok = False

    # -- 7. Full Cycle --
    print("\n[7/7] Full Triage Cycle")
    try:
        runner = EmailTriageRunner.get_instance()
        deadline = time.monotonic() + 120.0
        t0 = time.time()
        report = await asyncio.wait_for(
            runner.run_cycle(deadline=deadline),
            timeout=120.0,
        )
        elapsed = time.time() - t0

        cycle_ok = not report.skipped
        _status("Cycle completed", cycle_ok,
                f"skipped={report.skipped}, reason={report.skip_reason}")
        _status("Emails fetched", True, str(report.emails_fetched))
        _status("Emails processed", True, str(report.emails_processed))
        _status("Tier counts", True, str(report.tier_counts))
        _status("Notifications", True,
                f"sent={report.notifications_sent}, "
                f"suppressed={report.notifications_suppressed}")
        _status("Errors", len(report.errors) == 0,
                str(report.errors) if report.errors else "none")
        _status("Health", getattr(report, "health_healthy", True),
                getattr(report, "health_recommendation", "OK"))
        _status("Elapsed", True, f"{elapsed:.1f}s")

        if report.emails_fetched == 0:
            print("\n  NOTE: No unread emails found. Send yourself a test email")
            print("        and re-run this script to see full processing.")
    except asyncio.TimeoutError:
        _status("Cycle", False, "Timed out after 120s")
        all_ok = False
    except Exception as e:
        _status("Cycle", False, f"{type(e).__name__}: {str(e)[:150]}")
        all_ok = False

    # -- Summary --
    print("\n" + "=" * 60)
    if all_ok:
        print("  SUCCESS -- triage chain is operational")
    else:
        print("  WARNING -- some checks failed, see above for details")
    print("=" * 60 + "\n")

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
