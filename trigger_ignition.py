#!/usr/bin/env python3
"""
Ouroboros Go/No-Go Ignition Test
=================================
Boots the governance stack in ISOLATION (no full supervisor) and executes
one deterministic operation against a docs/ file (GOVERNED tier — auto-proceeds,
no voice approval required).

6-Point Checklist:
  1. Governance mode + resolved repo roots printed
  2. Provider route logged (GCP_PRIME_SPOT or fallback)
  3. Full lifecycle transitions to COMPLETE
  4. Voice/TUI narration emitted at transitions
  5. Ledger contains op with TelemetryContext, route, terminal outcome
  6. Terminal outcome confirmed

Usage:
    cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent
    source venv/bin/activate
    python3 trigger_ignition.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Path / env bootstrap
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "backend"))

# Load .env manually (no python-dotenv dependency required)
_env_file = REPO_ROOT / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _key, _, _val = _line.partition("=")
            os.environ.setdefault(_key.strip(), _val.strip())

# Force governed mode for this test
os.environ["JARVIS_GOVERNANCE_MODE"] = "governed"
os.environ["JARVIS_PROJECT_ROOT"] = str(REPO_ROOT)

# ---------------------------------------------------------------------------
# Logging — coloured, verbose enough to see narration + provider route
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
# Quieten noisy sub-loggers
for _noisy in ("httpx", "httpcore", "anthropic._base_client"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

log = logging.getLogger("ignition")

# ---------------------------------------------------------------------------
# Target: docs/ouroboros_production_readiness.md  (docs/ = GOVERNED tier)
# A comment appended at the end — safe, reversible, zero risk.
# ---------------------------------------------------------------------------

TARGET_FILE = "docs/ouroboros_production_readiness.md"
TASK_DESCRIPTION = (
    "Append a single markdown comment line at the very end of "
    f"`{TARGET_FILE}` with the text: "
    "`<!-- monitored by Ouroboros -->` "
    "Do not modify any other content. Add exactly one line."
)

# ---------------------------------------------------------------------------
# Checklist tracking
# ---------------------------------------------------------------------------

checklist = {
    1: ("Governance mode + repo roots printed", False),
    2: ("Provider route logged", False),
    3: ("Full lifecycle reached COMPLETE", False),
    4: ("Voice/TUI narration emitted", False),
    5: ("Ledger contains op record", False),
    6: ("Terminal outcome confirmed", False),
}


def check(n: int) -> None:
    label = checklist[n][0]
    checklist[n] = (label, True)
    log.info("  ✅  [%d] %s", n, label)


def print_checklist() -> None:
    print("\n" + "=" * 60)
    print("  OUROBOROS GO/NO-GO CHECKLIST")
    print("=" * 60)
    all_pass = True
    for n, (label, passed) in sorted(checklist.items()):
        icon = "✅" if passed else "❌"
        print(f"  {icon}  [{n}] {label}")
        if not passed:
            all_pass = False
    print("=" * 60)
    if all_pass:
        print("  🟢  IGNITION: GO — organism's first heartbeat confirmed.")
    else:
        print("  🔴  IGNITION: NO-GO — see failed items above.")
    print("=" * 60 + "\n")


# ---------------------------------------------------------------------------
# Patch VoiceNarrator to intercept narration calls (safe_say may not be live)
# ---------------------------------------------------------------------------

_narration_log: list[str] = []


async def _mock_safe_say(text: str, source: str = "ouroboros") -> bool:
    _narration_log.append(text)
    log.info("  🔊  NARRATION [%s]: %s", source, text)
    return True


# ---------------------------------------------------------------------------
# Main ignition coroutine
# ---------------------------------------------------------------------------


async def ignite() -> None:
    from backend.core.ouroboros.governance.integration import (
        GovernanceConfig,
        create_governance_stack,
    )
    from backend.core.ouroboros.governance.governed_loop_service import (
        GovernedLoopConfig,
        GovernedLoopService,
    )
    from backend.core.ouroboros.governance.op_context import OperationContext

    # ------------------------------------------------------------------ #
    # 1. Boot governance stack
    # ------------------------------------------------------------------ #
    log.info("--- Booting standalone governance stack ---")
    gov_config = GovernanceConfig.from_env_and_args(args=None)
    log.info("  mode        : %s", gov_config.initial_mode.value)
    log.info("  ledger_dir  : %s", gov_config.ledger_dir)

    stack = await create_governance_stack(gov_config)
    await stack.start()
    log.info("  stack health: %s", stack.health())

    # ------------------------------------------------------------------ #
    # 2. Boot GovernedLoopService
    # ------------------------------------------------------------------ #
    log.info("--- Booting GovernedLoopService ---")
    loop_config = GovernedLoopConfig.from_env(project_root=REPO_ROOT)
    log.info("  project_root: %s", loop_config.project_root)
    log.info("  claude_model: %s", loop_config.claude_model)

    # Patch VoiceNarrator's say_fn so narration works without live audio
    try:
        from backend.core.ouroboros.governance.comms.voice_narrator import VoiceNarrator
        for transport in stack.comm._transports:
            if isinstance(transport, VoiceNarrator):
                transport._say_fn = _mock_safe_say
                log.info("  VoiceNarrator patched with mock safe_say ✓")
                break
    except Exception as exc:
        log.warning("  Could not patch VoiceNarrator: %s", exc)

    gls = GovernedLoopService(stack=stack, prime_client=None, config=loop_config)
    await gls.start()
    log.info("  GLS state   : %s", gls.state.name)
    log.info("  GLS health  : %s", gls.health())

    # ------------------------------------------------------------------ #
    # Checklist item 1 — mode + repo roots
    # ------------------------------------------------------------------ #
    try:
        repo_reg = getattr(gls, "_repo_registry", None)
        repo_roots = (
            {r.name: str(r.path) for r in repo_reg.list_enabled()}
            if repo_reg is not None else {}
        )
        log.info("  *** OUROBOROS IGNITION *** mode=%s repos=%s",
                 gov_config.initial_mode.value, repo_roots)
        check(1)
    except Exception as exc:
        log.warning("  Could not resolve repo roots: %s", exc)

    # ------------------------------------------------------------------ #
    # 3. Create and submit operation
    # ------------------------------------------------------------------ #
    log.info("--- Submitting ignition operation ---")
    log.info("  target : %s", TARGET_FILE)
    log.info("  task   : %s", TASK_DESCRIPTION)

    ctx = OperationContext.create(
        target_files=(TARGET_FILE,),
        description=TASK_DESCRIPTION,
        primary_repo="jarvis",
        repo_scope=("jarvis",),
    )
    log.info("  op_id  : %s", ctx.op_id)

    t_start = time.monotonic()
    result = await gls.submit(ctx, trigger_source="ignition_test")
    duration = time.monotonic() - t_start

    log.info("--- Operation result ---")
    log.info("  terminal_phase : %s", result.terminal_phase.name)
    log.info("  reason_code    : %s", result.reason_code)
    log.info("  provider_used  : %s", result.provider_used)
    log.info("  duration       : %.2fs", duration)

    # ------------------------------------------------------------------ #
    # Checklist item 2 — provider route
    # ------------------------------------------------------------------ #
    if result.provider_used:
        log.info("  provider route : %s", result.provider_used)
        check(2)
    else:
        log.warning("  provider_used is None — route not logged")

    # ------------------------------------------------------------------ #
    # Checklist item 3 — lifecycle complete
    # ------------------------------------------------------------------ #
    from backend.core.ouroboros.governance.op_context import OperationPhase
    if result.terminal_phase == OperationPhase.COMPLETE:
        check(3)
    else:
        log.error("  Terminal phase is %s, not COMPLETE", result.terminal_phase.name)

    # ------------------------------------------------------------------ #
    # Checklist item 4 — narration
    # ------------------------------------------------------------------ #
    if _narration_log:
        log.info("  Narrations captured: %d", len(_narration_log))
        for n, msg in enumerate(_narration_log, 1):
            log.info("    [%d] %s", n, msg)
        check(4)
    else:
        log.warning("  No narrations captured (debounce may have suppressed or VoiceNarrator not reached)")

    # ------------------------------------------------------------------ #
    # Checklist item 5 — ledger entry
    # ------------------------------------------------------------------ #
    try:
        ledger_dir = gov_config.ledger_dir
        op_files = list(ledger_dir.glob(f"{ctx.op_id}*.json")) if ledger_dir.exists() else []
        if not op_files:
            # Try scanning all ledger files for this op_id
            op_files = [f for f in ledger_dir.glob("*.json") if ctx.op_id in f.read_text()]
        if op_files:
            log.info("  Ledger entry found: %s", op_files[0].name)
            check(5)
        else:
            log.warning("  No ledger entry found for op_id=%s in %s", ctx.op_id, ledger_dir)
            # Check if ledger dir exists at all
            if ledger_dir.exists():
                all_files = list(ledger_dir.iterdir())
                log.info("  Ledger dir has %d files", len(all_files))
            else:
                log.warning("  Ledger dir does not exist yet: %s", ledger_dir)
    except Exception as exc:
        log.warning("  Ledger check failed: %s", exc)

    # ------------------------------------------------------------------ #
    # Checklist item 6 — terminal outcome
    # ------------------------------------------------------------------ #
    if result.terminal_phase is not None:
        log.info("  Terminal outcome: %s (%s)", result.terminal_phase.name, result.reason_code)
        check(6)

    # ------------------------------------------------------------------ #
    # Verify the file was actually modified
    # ------------------------------------------------------------------ #
    target_path = REPO_ROOT / TARGET_FILE
    if target_path.exists():
        last_line = target_path.read_text().rstrip().splitlines()[-1]
        log.info("  Last line of %s: %r", TARGET_FILE, last_line)
        if "Ouroboros" in last_line or "ouroboros" in last_line.lower():
            log.info("  ✅  File modification confirmed on disk")
        else:
            log.warning("  ⚠️  File not modified as expected (last line: %r)", last_line)

    # ------------------------------------------------------------------ #
    # Teardown
    # ------------------------------------------------------------------ #
    await gls.stop()
    await stack.stop()
    log.info("--- Stack torn down cleanly ---")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("  OUROBOROS IGNITION SEQUENCE — FIRST HEARTBEAT")
    print("=" * 60 + "\n")

    try:
        asyncio.run(ignite())
    except KeyboardInterrupt:
        log.info("Interrupted by user")
    except Exception as exc:
        log.exception("Ignition sequence failed: %s", exc)

    print_checklist()
