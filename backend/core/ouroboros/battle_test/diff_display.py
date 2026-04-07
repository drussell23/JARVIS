"""BattleDiffDisplay — live colored diff output for the battle test CLI.

Shows Claude-Code-style colored diffs when Ouroboros applies changes.
Intercepts CommProtocol messages and prints formatted output to stdout.

Quick version — a proper TUI transport will be specced separately.
"""
from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ANSI color codes
_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RED = "\033[31m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_BLUE = "\033[34m"
_CYAN = "\033[36m"
_WHITE = "\033[37m"
_BG_RED = "\033[41m"
_BG_GREEN = "\033[42m"


def _colored_diff(diff_text: str) -> str:
    """Colorize a unified diff string."""
    lines = []
    for line in diff_text.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            lines.append(f"{_BOLD}{_WHITE}{line}{_RESET}")
        elif line.startswith("@@"):
            lines.append(f"{_CYAN}{line}{_RESET}")
        elif line.startswith("+"):
            lines.append(f"{_GREEN}{line}{_RESET}")
        elif line.startswith("-"):
            lines.append(f"{_RED}{line}{_RESET}")
        else:
            lines.append(line)
    return "\n".join(lines)


def print_operation_start(
    op_id: str,
    goal: str,
    target_files: List[str],
    risk_tier: str,
    sensor: str = "",
) -> None:
    """Print a header when an operation begins."""
    short_id = op_id.split("-")[1][:8] if "-" in op_id else op_id[:8]
    print(f"\n{_BOLD}{_CYAN}{'=' * 70}{_RESET}")
    print(f"{_BOLD}{_CYAN}  OUROBOROS  {_RESET}{_DIM}op:{short_id}{_RESET}  {_YELLOW}{risk_tier}{_RESET}")
    if sensor:
        print(f"  {_DIM}sensor: {sensor}{_RESET}")
    print(f"  {_WHITE}{goal[:100]}{_RESET}")
    if target_files:
        for f in target_files[:5]:
            print(f"  {_DIM}  -> {f}{_RESET}")
        if len(target_files) > 5:
            print(f"  {_DIM}  ... and {len(target_files) - 5} more{_RESET}")
    print(f"{_BOLD}{_CYAN}{'=' * 70}{_RESET}")


def print_operation_result(
    op_id: str,
    status: str,
    files_changed: List[str],
    composite_score: Optional[float] = None,
    duration_s: float = 0.0,
    repo_path: Optional[Path] = None,
) -> None:
    """Print the result of an operation with colored diff."""
    short_id = op_id.split("-")[1][:8] if "-" in op_id else op_id[:8]

    if status in ("completed", "applied"):
        icon = f"{_GREEN}OK{_RESET}"
    elif status == "failed":
        icon = f"{_RED}FAIL{_RESET}"
    elif status == "cancelled":
        icon = f"{_YELLOW}SKIP{_RESET}"
    else:
        icon = f"{_DIM}{status}{_RESET}"

    score_str = f"  score={composite_score:.3f}" if composite_score is not None else ""
    dur_str = f"  {duration_s:.1f}s" if duration_s > 0 else ""

    print(f"\n  {_BOLD}[{icon}]{_RESET} op:{short_id}{score_str}{dur_str}")

    # Show diff for each changed file
    if files_changed and repo_path and status in ("completed", "applied"):
        for fpath in files_changed[:10]:
            _show_file_diff(repo_path, fpath)

    print()


def _show_file_diff(repo_path: Path, file_path: str) -> None:
    """Show a colored git diff for a single file."""
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--", file_path],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=5,
        )
        diff = result.stdout.strip()
        if not diff:
            # Try unstaged diff
            result = subprocess.run(
                ["git", "diff", "--", file_path],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=5,
            )
            diff = result.stdout.strip()
        if not diff:
            # Try diff against HEAD~1
            result = subprocess.run(
                ["git", "diff", "HEAD~1", "--", file_path],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=5,
            )
            diff = result.stdout.strip()

        if diff:
            # Truncate very long diffs
            lines = diff.splitlines()
            if len(lines) > 40:
                truncated = "\n".join(lines[:40])
                print(f"\n{_colored_diff(truncated)}")
                print(f"{_DIM}  ... ({len(lines) - 40} more lines){_RESET}")
            else:
                print(f"\n{_colored_diff(diff)}")
        else:
            print(f"  {_DIM}{file_path} (no diff available){_RESET}")
    except Exception:
        print(f"  {_DIM}{file_path} (diff unavailable){_RESET}")


def print_phase_update(op_id: str, phase: str, elapsed_s: float = 0.0, detail: str = "") -> None:
    """Print a compact phase transition line."""
    short_id = op_id.split("-")[1][:8] if "-" in op_id else op_id[:8]
    phases_display = {
        "CLASSIFY": f"{_BLUE}CLASSIFY{_RESET}",
        "ROUTE": f"{_BLUE}ROUTE{_RESET}",
        "GENERATE": f"{_YELLOW}GENERATE{_RESET}",
        "VALIDATE": f"{_YELLOW}VALIDATE{_RESET}",
        "GATE": f"{_CYAN}GATE{_RESET}",
        "APPLY": f"{_GREEN}APPLY{_RESET}",
        "VERIFY": f"{_GREEN}VERIFY{_RESET}",
        "COMPLETE": f"{_BOLD}{_GREEN}COMPLETE{_RESET}",
        "FAILED": f"{_BOLD}{_RED}FAILED{_RESET}",
        "CANCELLED": f"{_DIM}CANCELLED{_RESET}",
    }
    phase_str = phases_display.get(phase.upper(), phase)
    elapsed_str = f" ({elapsed_s:.0f}s)" if elapsed_s > 0 else ""
    detail_str = f" {_DIM}{detail}{_RESET}" if detail else ""
    print(f"  {_DIM}op:{short_id}{_RESET} {phase_str}{elapsed_str}{detail_str}", flush=True)


def print_file_write(op_id: str, file_path: str, action: str = "writing") -> None:
    """Print when Ouroboros is writing to a specific file."""
    short_id = op_id.split("-")[1][:8] if "-" in op_id else op_id[:8]
    icon = f"{_GREEN}>{_RESET}" if action == "writing" else f"{_YELLOW}~{_RESET}"
    print(f"  {_DIM}op:{short_id}{_RESET} {icon} {_WHITE}{file_path}{_RESET}", flush=True)


def print_ouroboros_signature() -> None:
    """Print the Ouroboros attribution line after a successful commit."""
    print(f"  {_DIM}Co-Authored-By: Ouroboros Self-Development Engine{_RESET}", flush=True)


def print_breaker_event(provider: str, endpoint: str, state: str, detail: str = "") -> None:
    """Print circuit breaker state change."""
    if state.upper() == "OPEN":
        color = _RED
    elif state.upper() == "HALF_OPEN":
        color = _YELLOW
    else:
        color = _GREEN
    detail_str = f" ({detail})" if detail else ""
    print(
        f"  {_BOLD}[BREAKER]{_RESET} {provider}:{endpoint} "
        f"{color}{state.upper()}{_RESET}{detail_str}",
        flush=True,
    )


def print_throttle_event(provider: str, endpoint: str, multiplier: float) -> None:
    """Print throttle change event."""
    pct = int(multiplier * 100)
    if pct < 30:
        color = _RED
    elif pct < 70:
        color = _YELLOW
    else:
        color = _GREEN
    print(
        f"  {_BOLD}[THROTTLE]{_RESET} {provider}:{endpoint} "
        f"rate -> {color}{pct}%{_RESET}",
        flush=True,
    )


class BattleDiffTransport:
    """CommProtocol transport that prints live diffs to stdout.

    Plug this into the CommProtocol transport stack to get
    Claude-Code-style diff output during the battle test.
    """

    def __init__(self, repo_path: Path) -> None:
        self._repo_path = repo_path
        self._op_start_times: Dict[str, float] = {}

    async def send(self, msg: Any) -> None:
        """Handle a CommMessage. Shows headers, phases, file writes, diffs."""
        try:
            payload = msg.payload if hasattr(msg, "payload") else {}
            op_id = msg.op_id if hasattr(msg, "op_id") else ""
            msg_type = msg.msg_type.value if hasattr(msg, "msg_type") else ""

            if msg_type == "INTENT" and payload.get("risk_tier") not in ("routing",):
                self._op_start_times[op_id] = time.time()
                print_operation_start(
                    op_id=op_id,
                    goal=payload.get("goal", ""),
                    target_files=payload.get("target_files", []),
                    risk_tier=payload.get("risk_tier", ""),
                    sensor=payload.get("sensor", ""),
                )

            elif msg_type == "HEARTBEAT":
                phase = payload.get("phase", "")
                if phase and ":" not in phase:  # Skip FSM internal states
                    elapsed = time.time() - self._op_start_times.get(op_id, time.time())
                    # Extract detail from payload for richer display
                    detail = ""
                    if "target_file" in payload:
                        detail = payload["target_file"]
                    elif "model" in payload:
                        detail = f"model={payload['model']}"
                    print_phase_update(op_id, phase, elapsed, detail)

            elif msg_type == "PLAN":
                # PLAN messages contain file-level details during GENERATE/APPLY
                target_file = payload.get("target_file", "")
                action = payload.get("action", "writing")
                if target_file:
                    print_file_write(op_id, target_file, action)

            elif msg_type == "DECISION":
                outcome = payload.get("outcome", "")
                elapsed = time.time() - self._op_start_times.pop(op_id, time.time())
                files = payload.get("files_changed", [])
                score = payload.get("composite_score")
                print_operation_result(
                    op_id=op_id,
                    status=outcome,
                    files_changed=files,
                    composite_score=score,
                    duration_s=elapsed,
                    repo_path=self._repo_path,
                )
                # Ouroboros signature on successful operations
                if outcome in ("completed", "applied", "auto_approved"):
                    print_ouroboros_signature()

        except Exception:
            pass  # Never crash the pipeline for display issues
