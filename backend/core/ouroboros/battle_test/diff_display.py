"""BattleDiffDisplay -- live animated CLI output for the Ouroboros Battle Test.

Shows real-time, emoji-rich, colored output so the user can follow exactly
what the organism is doing: which sensors fired, what files are being analyzed,
code generation progress, validation results, and colored git diffs.

Hooks into the CommProtocol transport stack via BattleDiffTransport.
"""
from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── ANSI codes ──────────────────────────────────────────────────

_R = "\033[0m"       # reset
_B = "\033[1m"       # bold
_D = "\033[2m"       # dim
_I = "\033[3m"       # italic
_UL = "\033[4m"      # underline
_RED = "\033[31m"
_GRN = "\033[32m"
_YLW = "\033[33m"
_BLU = "\033[34m"
_MAG = "\033[35m"
_CYN = "\033[36m"
_WHT = "\033[37m"
_BRED = "\033[91m"   # bright red
_BGRN = "\033[92m"   # bright green
_BYLW = "\033[93m"   # bright yellow
_BBLU = "\033[94m"   # bright blue
_BMAG = "\033[95m"   # bright magenta
_BCYN = "\033[96m"   # bright cyan


# ── Phase emoji map ─────────────────────────────────────────────

_PHASE_EMOJI = {
    "CLASSIFY":          "\U0001f50d",  # magnifying glass
    "ROUTE":             "\U0001f9ed",  # compass
    "CONTEXT_EXPANSION": "\U0001f4da",  # books
    "GENERATE":          "\u2728",      # sparkles
    "VALIDATE":          "\U0001f9ea",  # test tube
    "GATE":              "\U0001f6e1\ufe0f",  # shield
    "APPROVE":           "\U0001f464",  # bust silhouette
    "APPLY":             "\U0001f4be",  # floppy disk
    "VERIFY":            "\u2705",      # check mark
    "COMPLETE":          "\U0001f389",  # party popper
    "FAILED":            "\u274c",      # cross mark
    "CANCELLED":         "\u23ed\ufe0f",  # skip forward
    "POSTMORTEM":        "\U0001f480",  # skull
}

_RISK_EMOJI = {
    "SAFE_AUTO":          f"{_BGRN}\U0001f7e2 SAFE{_R}",
    "APPROVAL_REQUIRED":  f"{_BYLW}\U0001f7e1 APPROVAL{_R}",
    "BLOCKED":            f"{_BRED}\U0001f534 BLOCKED{_R}",
}


# ── Separator lines ─────────────────────────────────────────────

def _header_line() -> str:
    return f"{_B}{_CYN}\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500{_R}"


def _thin_line() -> str:
    return f"{_D}\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508\u2508{_R}"


# ── Colored diff ────────────────────────────────────────────────

def _colored_diff(diff_text: str) -> str:
    """Colorize a unified diff string."""
    lines = []
    for line in diff_text.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            lines.append(f"{_B}{_WHT}{line}{_R}")
        elif line.startswith("@@"):
            lines.append(f"{_CYN}{line}{_R}")
        elif line.startswith("+"):
            lines.append(f"{_BGRN}{line}{_R}")
        elif line.startswith("-"):
            lines.append(f"{_BRED}{line}{_R}")
        else:
            lines.append(f"{_D}{line}{_R}")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════
# Public display functions
# ══════════════════════════════════════════════════════════════════


def print_operation_start(
    op_id: str,
    goal: str,
    target_files: List[str],
    risk_tier: str,
    sensor: str = "",
) -> None:
    """Print a rich header when an operation begins."""
    short_id = op_id.split("-")[1][:8] if "-" in op_id else op_id[:8]
    risk_display = _RISK_EMOJI.get(risk_tier, f"{_D}{risk_tier}{_R}")

    print(f"\n{_header_line()}")
    print(f"  \U0001f40d {_B}{_BCYN}OUROBOROS{_R}  {_D}op:{short_id}{_R}  {risk_display}")
    if sensor:
        print(f"  \U0001f4e1 {_D}Detected by:{_R} {_WHT}{sensor}{_R}")
    print(f"  \U0001f4cb {_WHT}{goal[:120]}{_R}")
    if target_files:
        print(f"  \U0001f4c2 {_D}Target files:{_R}")
        for f in target_files[:5]:
            print(f"     {_BBLU}\u2192{_R} {_WHT}{f}{_R}")
        if len(target_files) > 5:
            print(f"     {_D}... and {len(target_files) - 5} more{_R}")
    print(_header_line())


def print_phase_update(op_id: str, phase: str, elapsed_s: float = 0.0, detail: str = "") -> None:
    """Print a compact phase transition with emoji."""
    short_id = op_id.split("-")[1][:8] if "-" in op_id else op_id[:8]
    emoji = _PHASE_EMOJI.get(phase.upper(), "\u2502")
    phase_upper = phase.upper()

    # Color based on phase category
    if phase_upper in ("CLASSIFY", "ROUTE", "CONTEXT_EXPANSION"):
        color = _BBLU
    elif phase_upper in ("GENERATE",):
        color = _BYLW
    elif phase_upper in ("VALIDATE", "GATE"):
        color = _BCYN
    elif phase_upper in ("APPLY", "VERIFY"):
        color = _BGRN
    elif phase_upper in ("COMPLETE",):
        color = _BGRN
    elif phase_upper in ("FAILED", "POSTMORTEM"):
        color = _BRED
    else:
        color = _D

    elapsed_str = f" {_D}({elapsed_s:.0f}s){_R}" if elapsed_s > 0 else ""
    detail_str = f" {_D}\u2014 {detail}{_R}" if detail else ""
    print(
        f"  {_D}\u2502{_R} {emoji} {color}{_B}{phase_upper}{_R}{elapsed_str}{detail_str}",
        flush=True,
    )


def print_file_write(op_id: str, file_path: str, action: str = "writing") -> None:
    """Print when Ouroboros is writing to a specific file."""
    short_id = op_id.split("-")[1][:8] if "-" in op_id else op_id[:8]
    if action == "writing":
        emoji = "\u270d\ufe0f"   # writing hand
        color = _BGRN
    else:
        emoji = "\U0001f50e"     # magnifying glass right
        color = _BYLW
    print(
        f"  {_D}\u2502{_R} {emoji} {color}{action}{_R} {_B}{_WHT}{file_path}{_R}",
        flush=True,
    )


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

    if status in ("completed", "applied", "auto_approved"):
        icon = f"\u2705 {_BGRN}{_B}SUCCESS{_R}"
    elif status == "failed":
        icon = f"\u274c {_BRED}{_B}FAILED{_R}"
    elif status == "cancelled":
        icon = f"\u23ed\ufe0f  {_BYLW}SKIPPED{_R}"
    elif status == "queued":
        icon = f"\U0001f4e5 {_BYLW}QUEUED FOR REVIEW{_R}"
    else:
        icon = f"\u2753 {_D}{status}{_R}"

    score_str = ""
    if composite_score is not None:
        if composite_score < 0.3:
            score_str = f"  {_BGRN}\u2b06 score={composite_score:.3f}{_R}"
        elif composite_score < 0.6:
            score_str = f"  {_BYLW}\u2b50 score={composite_score:.3f}{_R}"
        else:
            score_str = f"  {_BRED}\u2b07 score={composite_score:.3f}{_R}"

    dur_str = f"  {_D}\u23f1 {duration_s:.1f}s{_R}" if duration_s > 0 else ""

    print(f"\n  {_thin_line()}")
    print(f"  {icon}  {_D}op:{short_id}{_R}{score_str}{dur_str}")

    # Show diff for each changed file
    if files_changed and repo_path and status in ("completed", "applied", "auto_approved"):
        print(f"\n  \U0001f4dd {_B}Changes:{_R}")
        for fpath in files_changed[:10]:
            _show_file_diff(repo_path, fpath)

    print()


def _show_file_diff(repo_path: Path, file_path: str) -> None:
    """Show a colored git diff for a single file."""
    try:
        # Try multiple diff strategies
        for args in (
            ["git", "diff", "--cached", "--", file_path],
            ["git", "diff", "--", file_path],
            ["git", "diff", "HEAD~1", "--", file_path],
        ):
            result = subprocess.run(
                args, cwd=repo_path, capture_output=True, text=True, timeout=5,
            )
            diff = result.stdout.strip()
            if diff:
                break

        if diff:
            lines = diff.splitlines()
            if len(lines) > 50:
                truncated = "\n".join(lines[:50])
                print(f"\n{_colored_diff(truncated)}")
                print(f"  {_D}... ({len(lines) - 50} more lines){_R}")
            else:
                print(f"\n{_colored_diff(diff)}")
        else:
            print(f"  {_D}\U0001f4c4 {file_path} (no diff available){_R}")
    except Exception:
        print(f"  {_D}\U0001f4c4 {file_path} (diff unavailable){_R}")


def print_ouroboros_signature() -> None:
    """Print the Ouroboros attribution after a successful commit."""
    print(
        f"  {_D}\U0001f40d Co-Authored-By: "
        f"{_I}Ouroboros Self-Development Engine{_R}",
        flush=True,
    )


def print_breaker_event(provider: str, endpoint: str, state: str, detail: str = "") -> None:
    """Print circuit breaker state change."""
    if state.upper() == "OPEN":
        emoji = "\U0001f6a8"  # rotating light
        color = _BRED
    elif state.upper() == "HALF_OPEN":
        emoji = "\U0001f7e1"  # yellow circle
        color = _BYLW
    else:
        emoji = "\U0001f7e2"  # green circle
        color = _BGRN
    detail_str = f" {_D}({detail}){_R}" if detail else ""
    print(
        f"  {emoji} {_B}BREAKER{_R} {_WHT}{provider}:{endpoint}{_R} "
        f"\u2192 {color}{_B}{state.upper()}{_R}{detail_str}",
        flush=True,
    )


def print_throttle_event(provider: str, endpoint: str, multiplier: float) -> None:
    """Print throttle change event."""
    pct = int(multiplier * 100)
    if pct < 30:
        emoji = "\U0001f534"  # red circle
        color = _BRED
    elif pct < 70:
        emoji = "\U0001f7e0"  # orange circle
        color = _BYLW
    else:
        emoji = "\U0001f7e2"  # green circle
        color = _BGRN
    bar_filled = int(pct / 5)
    bar_empty = 20 - bar_filled
    bar = f"{color}{'█' * bar_filled}{_D}{'░' * bar_empty}{_R}"
    print(
        f"  {emoji} {_B}THROTTLE{_R} {_WHT}{provider}:{endpoint}{_R} "
        f"{bar} {color}{_B}{pct}%{_R}",
        flush=True,
    )


def print_sensor_scan(sensor: str, detail: str = "") -> None:
    """Print when a sensor is scanning."""
    detail_str = f" {_D}\u2014 {detail}{_R}" if detail else ""
    print(
        f"  \U0001f4e1 {_D}Scanning:{_R} {_WHT}{sensor}{_R}{detail_str}",
        flush=True,
    )


def print_boot_step(step: str, status: str = "ok") -> None:
    """Print a boot sequence step."""
    if status == "ok":
        emoji = "\u2705"
    elif status == "skip":
        emoji = "\u23ed\ufe0f"
    elif status == "fail":
        emoji = "\u274c"
    else:
        emoji = "\u2502"
    print(f"  {emoji} {_WHT}{step}{_R}", flush=True)


# ══════════════════════════════════════════════════════════════════
# BattleDiffTransport — CommProtocol transport
# ══════════════════════════════════════════════════════════════════


class BattleDiffTransport:
    """CommProtocol transport with rich emoji-animated CLI output.

    Plugs into the CommProtocol transport stack and prints
    real-time, human-readable output showing exactly what
    the organism is doing at every step.
    """

    def __init__(self, repo_path: Path) -> None:
        self._repo_path = repo_path
        self._op_start_times: Dict[str, float] = {}
        self._seen_phases: Dict[str, set] = {}

    async def send(self, msg: Any) -> None:
        """Handle a CommMessage with rich formatted output."""
        try:
            payload = msg.payload if hasattr(msg, "payload") else {}
            op_id = msg.op_id if hasattr(msg, "op_id") else ""
            msg_type = msg.msg_type.value if hasattr(msg, "msg_type") else ""

            if msg_type == "INTENT" and payload.get("risk_tier") not in ("routing",):
                self._op_start_times[op_id] = time.time()
                self._seen_phases[op_id] = set()
                print_operation_start(
                    op_id=op_id,
                    goal=payload.get("goal", ""),
                    target_files=payload.get("target_files", []),
                    risk_tier=payload.get("risk_tier", ""),
                    sensor=payload.get("sensor", ""),
                )

            elif msg_type == "HEARTBEAT":
                phase = payload.get("phase", "")
                # Skip FSM internal states and duplicate phases
                if phase and ":" not in phase:
                    seen = self._seen_phases.get(op_id, set())
                    if phase.upper() not in seen:
                        seen.add(phase.upper())
                        elapsed = time.time() - self._op_start_times.get(op_id, time.time())
                        detail = ""
                        if "target_file" in payload:
                            detail = payload["target_file"]
                        elif "model" in payload:
                            detail = f"model={payload['model']}"
                        elif "brain" in payload:
                            detail = f"brain={payload['brain']}"
                        print_phase_update(op_id, phase, elapsed, detail)

            elif msg_type == "PLAN":
                target_file = payload.get("target_file", "")
                action = payload.get("action", "writing")
                if target_file:
                    print_file_write(op_id, target_file, action)

            elif msg_type == "DECISION":
                outcome = payload.get("outcome", "")
                elapsed = time.time() - self._op_start_times.pop(op_id, time.time())
                self._seen_phases.pop(op_id, None)
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
                if outcome in ("completed", "applied", "auto_approved"):
                    print_ouroboros_signature()

        except Exception:
            pass  # Never crash the pipeline for display issues
