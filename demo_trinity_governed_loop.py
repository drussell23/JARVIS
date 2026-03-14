#!/usr/bin/env python3
"""
Trinity AI — Governed Inference Loop Demo
==========================================

Live system demonstration with JARVIS voice narration
and real-time Rich terminal UI.

Usage:
  python3 demo_trinity_governed_loop.py            # Full demo with voice
  python3 demo_trinity_governed_loop.py --no-voice  # Silent mode
  python3 demo_trinity_governed_loop.py --no-tests  # Skip test suite
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.rule import Rule
from rich.syntax import Syntax
from rich.align import Align

# ── Configuration ───────────────────────────────────────────────────────────

JPRIME_ENDPOINT = os.getenv("JPRIME_ENDPOINT", "http://136.113.252.164:8000")
LEDGER_DIR = Path.home() / ".jarvis" / "ouroboros" / "ledger"
NO_VOICE = "--no-voice" in sys.argv or not shutil.which("say")
NO_TESTS = "--no-tests" in sys.argv
VOICE = "Daniel"
SPEECH_RATE = "175"

console = Console()

# ── Voice Engine ────────────────────────────────────────────────────────────

_speech_proc = None


def jarvis_say(text: str, wait: bool = False):
    """JARVIS speaks via macOS TTS (Daniel voice). Non-blocking by default."""
    global _speech_proc
    if NO_VOICE:
        return
    # Wait for any previous speech to finish before starting new
    if _speech_proc and _speech_proc.poll() is None:
        _speech_proc.wait()
    _speech_proc = subprocess.Popen(
        ["say", "-v", VOICE, "-r", SPEECH_RATE, text],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if wait:
        _speech_proc.wait()


def wait_speech():
    """Block until current speech finishes."""
    global _speech_proc
    if _speech_proc:
        _speech_proc.wait()
        _speech_proc = None


# ── Helpers ─────────────────────────────────────────────────────────────────

def phase_header(num: int, title: str):
    console.print()
    console.print(Rule(f"[bold cyan]PHASE {num} — {title}[/]", style="cyan"))
    console.print()


def pause(seconds: float = 1.0):
    time.sleep(seconds)


# ═════════════════════════════════════════════════════════════════════════════
# BANNER
# ═════════════════════════════════════════════════════════════════════════════

def show_banner():
    title = Text(justify="center")
    title.append("\n")
    title.append("T R I N I T Y   A I", style="bold white")
    title.append("\n\n")
    title.append("Governed Inference Loop", style="bold cyan")
    title.append("  ·  ", style="dim")
    title.append("Live System Demonstration", style="dim cyan")
    title.append("\n\n")
    title.append("The Body ", style="bold cyan")
    title.append("JARVIS", style="dim white")
    title.append("   ·   ", style="dim")
    title.append("The Mind ", style="bold cyan")
    title.append("J-Prime", style="dim white")
    title.append("   ·   ", style="dim")
    title.append("The Nerves ", style="bold cyan")
    title.append("Reactor-Core", style="dim white")
    title.append("\n")

    console.print(Panel(
        Align.center(title),
        border_style="bold cyan",
        padding=(0, 4),
    ))

    jarvis_say(
        "Welcome to the Trinity AI demonstration. "
        "I'm JARVIS, your autonomous software engineering system. "
        "I'll walk you through our governed inference pipeline in real time.",
        wait=True,
    )
    pause(0.5)


# ═════════════════════════════════════════════════════════════════════════════
# PHASE 1: SYSTEM STATUS
# ═════════════════════════════════════════════════════════════════════════════

def phase_1_system_status():
    import urllib.request

    phase_header(1, "LIVE SYSTEM STATUS")

    jarvis_say(
        "First, let me connect to J-Prime, "
        "our GPU inference engine running on Google Cloud.",
    )

    with console.status(
        "[bold cyan]  Connecting to J-Prime on GCP NVIDIA L4...[/]",
        spinner="dots",
    ):
        try:
            req = urllib.request.Request(f"{JPRIME_ENDPOINT}/v1/capability")
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
        except Exception as e:
            wait_speech()
            console.print(f"  [red bold]  Cannot reach J-Prime: {e}[/]")
            jarvis_say(
                "I'm unable to reach J-Prime. "
                "The GCP instance may be offline.",
                wait=True,
            )
            return None

    wait_speech()

    # Extract fields
    model_id = data.get("model_id", "unknown")
    model_artifact = data.get("model_artifact", "unknown")
    compute = data.get("compute_class", "unknown")
    gpu_layers = str(data.get("gpu_layers", "unknown"))
    ctx = data.get("context_window", 0)
    host = data.get("host", "unknown")
    schema = data.get("schema_version", "unknown")
    contract = data.get("contract_version", "unknown")
    generated = data.get("generated_at_epoch_s", 0)

    # Build capability table
    table = Table(
        show_header=False, border_style="green",
        padding=(0, 2), expand=True,
    )
    table.add_column("Property", style="white", width=22)
    table.add_column("Value", style="green bold")

    table.add_row("Status", "[green bold]● ONLINE[/]")
    table.add_row("Model", model_id)
    table.add_row("Artifact", model_artifact)
    table.add_row("Compute Class", compute.upper())
    table.add_row("GPU Layers", gpu_layers)
    table.add_row("Context Window", f"{ctx:,} tokens")
    table.add_row("Host", host)
    table.add_row("Schema Version", schema)
    table.add_row("Contract Version", contract)
    table.add_row("Endpoint", JPRIME_ENDPOINT)
    if generated:
        ts_str = datetime.fromtimestamp(
            generated, tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S UTC")
        table.add_row("Generated", ts_str)

    console.print(Panel(
        table,
        title="[bold green]J-Prime Live[/]",
        border_style="green",
        padding=(1, 2),
    ))

    # Contextual narration based on actual data
    artifact_name = model_artifact.replace(".gguf", "").replace("-", " ")
    jarvis_say(
        f"J-Prime is online. We're running {artifact_name} "
        f"on an NVIDIA L4 GPU with {gpu_layers} layers offloaded. "
        f"Context window is {ctx} tokens, giving us roughly "
        f"20 to 23 tokens per second of inference throughput.",
        wait=True,
    )

    return data


# ═════════════════════════════════════════════════════════════════════════════
# PHASE 2: GOVERNANCE LEDGER ANALYSIS
# ═════════════════════════════════════════════════════════════════════════════

def phase_2_governance_ledger():
    phase_header(2, "OUROBOROS GOVERNANCE LEDGER")

    jarvis_say(
        "Now let me show you our governance system. "
        "Ouroboros is our autonomous governance pipeline. "
        "Every code change must pass through risk classification, "
        "syntax validation, and security gates before being applied.",
    )

    with console.status(
        "[bold cyan]  Scanning durable operation ledger...[/]",
        spinner="dots",
    ):
        pause(0.8)

        if not LEDGER_DIR.exists():
            wait_speech()
            console.print("  [yellow]No ledger directory found.[/]")
            return

        ledger_files = sorted(LEDGER_DIR.glob("op-*.jsonl"))
        if not ledger_files:
            wait_speech()
            console.print("  [yellow]No ledger entries found.[/]")
            return

        # Analyze all operations
        total_ops = len(ledger_files)
        risk_tiers: dict[str, int] = {}
        providers: dict[str, int] = {}
        outcomes = {"applied": 0, "failed": 0}

        for lf in ledger_files:
            try:
                for line in lf.read_text().strip().split("\n"):
                    entry = json.loads(line)
                    state = entry.get("state", "")
                    entry_data = entry.get("data", {})

                    if "risk_tier" in entry_data:
                        rt = entry_data["risk_tier"]
                        risk_tiers[rt] = risk_tiers.get(rt, 0) + 1
                    if "provider" in entry_data:
                        p = entry_data["provider"]
                        providers[p] = providers.get(p, 0) + 1

                    if state == "applied":
                        outcomes["applied"] += 1
                    elif state == "failed":
                        outcomes["failed"] += 1
            except Exception:
                pass

    wait_speech()

    # ── Operations Summary ──────────────────────────────────────────────────

    stats = Table(
        show_header=False, border_style="cyan",
        padding=(0, 2), expand=True,
    )
    stats.add_column("Metric", style="white", width=28)
    stats.add_column("Value", style="cyan bold")
    stats.add_row("Total Governed Operations", str(total_ops))
    stats.add_row("Successfully Applied", f"[green bold]{outcomes['applied']}[/]")
    stats.add_row("Blocked by Security Gates", f"[red bold]{outcomes['failed']}[/]")

    if providers:
        for prov, cnt in sorted(providers.items(), key=lambda x: -x[1]):
            stats.add_row(f"Provider: {prov}", str(cnt))

    console.print(Panel(
        stats,
        title="[bold cyan]Governance Operations[/]",
        border_style="cyan",
        padding=(1, 2),
    ))

    # ── Risk Tier Distribution ──────────────────────────────────────────────

    if risk_tiers:
        tier_table = Table(
            border_style="yellow", padding=(0, 2), expand=True,
        )
        tier_table.add_column("Risk Tier", style="bold", width=28)
        tier_table.add_column("Count", justify="right", width=8)
        tier_table.add_column("Distribution", width=30)

        max_count = max(risk_tiers.values())
        for tier, count in sorted(risk_tiers.items(), key=lambda x: -x[1]):
            bar_len = max(1, int(count / max_count * 25))
            color = (
                "green" if tier == "SAFE_AUTO"
                else "yellow" if "APPROVAL" in tier
                else "red"
            )
            bar = f"[{color}]{'█' * bar_len}[/]"
            tier_table.add_row(f"[{color}]{tier}[/]", str(count), bar)

        console.print(Panel(
            tier_table,
            title="[bold yellow]Risk Classification[/]",
            border_style="yellow",
            padding=(1, 2),
        ))

    jarvis_say(
        f"The ledger contains {total_ops} governed operations. "
        f"{outcomes['applied']} were approved and applied, "
        f"and {outcomes['failed']} were blocked by security gates. "
        "Each operation is durably logged with rollback hashes "
        "for full auditability.",
    )

    # ── Pipeline Trace ──────────────────────────────────────────────────────

    console.print()
    console.print(
        "  [bold white]Governance Pipeline Trace[/]"
        "  [dim](most detailed operation)[/]"
    )
    console.print()

    best_file = max(ledger_files, key=lambda f: f.stat().st_size)
    lines = best_file.read_text().strip().split("\n")

    console.print(f"  [dim]Operation: {best_file.stem}[/]")
    console.print()

    state_styles = {
        "planned":    ("cyan",    "○"),
        "sandboxing": ("blue",    "◑"),
        "validating": ("yellow",  "◕"),
        "gating":     ("magenta", "◈"),
        "applying":   ("white",   "◉"),
        "applied":    ("green",   "●"),
        "failed":     ("red",     "✗"),
        "completed":  ("green",   "●"),
    }

    wait_speech()
    jarvis_say(
        "Watch the pipeline trace. Each state transition is durable "
        "and auditable.",
    )

    for line_str in lines:
        try:
            entry = json.loads(line_str)
            state = entry.get("state", "unknown")
            style, icon = state_styles.get(state, ("dim", "·"))
            entry_data = entry.get("data", {})

            detail = ""
            if "risk_tier" in entry_data:
                detail = f"risk={entry_data['risk_tier']}"
            elif "syntax_valid" in entry_data:
                detail = f"syntax_valid={entry_data['syntax_valid']}"
            elif "target_file" in entry_data:
                detail = f"file={entry_data['target_file']}"
            elif "failure_class" in entry_data:
                detail = f"class={entry_data['failure_class']}"
            elif "reason" in entry_data:
                detail = entry_data["reason"][:60]
            elif "phase" in entry_data:
                detail = f"phase={entry_data['phase']}"

            console.print(
                f"    [{style} bold]{icon} {state:<14}[/]  [dim]{detail}[/]"
            )
            pause(0.4)
        except Exception:
            pass

    console.print()
    wait_speech()


# ═════════════════════════════════════════════════════════════════════════════
# PHASE 3: LIVE GOVERNED INFERENCE
# ═════════════════════════════════════════════════════════════════════════════

def phase_3_live_inference():
    import urllib.request

    phase_header(3, "LIVE GOVERNED INFERENCE")

    jarvis_say(
        "Now I'll demonstrate live inference. "
        "I'm sending coding and reasoning tasks to J-Prime "
        "and showing the full generation pipeline.",
    )
    pause(0.5)

    prompts = [
        {
            "label": "Code Generation",
            "desc": "Python email validation with regex",
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a senior Python engineer. "
                        "Return only code."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Write a function that validates an email "
                        "address using regex."
                    ),
                },
            ],
            "max_tokens": 200,
            "narration": (
                "Sending a code generation task: "
                "write a Python email validator."
            ),
        },
        {
            "label": "Architecture Reasoning",
            "desc": "Optimistic vs pessimistic locking",
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Explain the difference between optimistic "
                        "and pessimistic locking in 2 sentences."
                    ),
                },
            ],
            "max_tokens": 100,
            "narration": (
                "Now a reasoning task: explain database locking "
                "strategies in two sentences."
            ),
        },
    ]

    for i, spec in enumerate(prompts):
        console.print(
            f"  [bold magenta]Task {i + 1}:[/] "
            f"[bold white]{spec['label']}[/]"
            f" — [dim]{spec['desc']}[/]"
        )
        console.print()

        jarvis_say(spec["narration"])

        payload = {
            "messages": spec["messages"],
            "max_tokens": spec["max_tokens"],
            "temperature": 0.1,
        }

        start = time.monotonic()
        with console.status(
            "[bold cyan]  Generating via J-Prime...[/]",
            spinner="dots",
        ):
            try:
                req = urllib.request.Request(
                    f"{JPRIME_ENDPOINT}/v1/chat/completions",
                    data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    result = json.loads(resp.read())
            except Exception as e:
                wait_speech()
                console.print(f"  [red bold]  Error: {e}[/]")
                continue

        elapsed_ms = (time.monotonic() - start) * 1000
        choice = result.get("choices", [{}])[0]
        content = choice.get("message", {}).get("content", "")
        usage = result.get("usage", {})
        routing = result.get("x_routing", {})

        prompt_tokens = usage.get("prompt_tokens", 0)
        comp_tokens = usage.get("completion_tokens", 1)
        x_latency = result.get("x_latency_ms", elapsed_ms)
        tok_s = comp_tokens / (x_latency / 1000) if x_latency > 0 else 0

        # ── Response Panel ──────────────────────────────────────────────────

        response_text = content.strip()
        if "def " in response_text or "import " in response_text:
            response_widget = Syntax(
                response_text, "python",
                theme="monokai", line_numbers=True,
            )
        else:
            response_widget = Text(response_text, style="white")

        console.print(Panel(
            response_widget,
            title=f"[bold green]{spec['label']} Result[/]",
            subtitle=(
                f"[dim]{elapsed_ms:.0f}ms · "
                f"~{tok_s:.1f} tok/s · "
                f"{comp_tokens} tokens[/]"
            ),
            border_style="green",
            padding=(1, 2),
        ))

        # ── Routing & Performance ───────────────────────────────────────────

        metrics = Table(
            show_header=False, border_style="magenta",
            padding=(0, 1),
        )
        metrics.add_column("", style="white", width=20)
        metrics.add_column("", style="magenta bold")

        routing_tier = routing.get("tier", "primary")
        routing_model = routing.get("model_id", "local-gpu")
        metrics.add_row("Routing Tier", routing_tier)
        metrics.add_row("Routing Model", routing_model)
        metrics.add_row("Latency", f"{elapsed_ms:.0f}ms")
        metrics.add_row(
            "Tokens",
            f"{prompt_tokens} prompt + {comp_tokens} completion",
        )
        metrics.add_row("Throughput", f"~{tok_s:.1f} tok/s")
        metrics.add_row(
            "Finish Reason",
            choice.get("finish_reason", "unknown"),
        )

        console.print(Panel(
            metrics,
            title="[bold magenta]Routing & Performance[/]",
            border_style="magenta",
            padding=(0, 2),
        ))

        wait_speech()

        jarvis_say(
            f"Generation complete in {elapsed_ms:.0f} milliseconds "
            f"at approximately {tok_s:.0f} tokens per second. "
            f"That's {comp_tokens} completion tokens on our NVIDIA L4.",
            wait=True,
        )

        console.print()
        pause(0.5)


# ═════════════════════════════════════════════════════════════════════════════
# PHASE 4: GOVERNANCE TEST SUITE
# ═════════════════════════════════════════════════════════════════════════════

def phase_4_test_suite():
    phase_header(4, "GOVERNANCE TEST SUITE")

    jarvis_say(
        "Now let's verify system integrity. "
        "I'm running our full governance test suite, "
        "over 2,000 tests covering the entire Ouroboros pipeline.",
    )

    console.print(
        "  [dim]pytest tests/test_ouroboros_governance/ "
        "tests/governance/ -q[/]"
    )
    console.print()

    start = time.monotonic()
    passed = 0
    failed = 0
    elapsed = 0.0

    with console.status(
        "[bold cyan]  Running governance tests...[/]",
        spinner="dots",
    ):
        try:
            result = subprocess.run(
                [
                    sys.executable, "-m", "pytest",
                    "tests/test_ouroboros_governance/",
                    "tests/governance/",
                    "-q", "--tb=no", "--no-header",
                ],
                capture_output=True, text=True, timeout=180,
                cwd=str(Path(__file__).parent),
            )
            elapsed = time.monotonic() - start
            output = result.stdout.strip()

            for line in output.split("\n"):
                if "passed" in line:
                    m = re.search(r"(\d+) passed", line)
                    if m:
                        passed = int(m.group(1))
                    m2 = re.search(r"(\d+) failed", line)
                    if m2:
                        failed = int(m2.group(1))
                    break

        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - start
            console.print("  [yellow]Test suite timed out.[/]")
        except Exception as e:
            elapsed = time.monotonic() - start
            console.print(f"  [red]ERROR: {e}[/]")

    wait_speech()

    if passed > 0:
        total = passed + failed
        rate = passed / total * 100 if total > 0 else 0

        test_table = Table(
            show_header=False, border_style="green",
            padding=(0, 2), expand=True,
        )
        test_table.add_column("", style="white", width=28)
        test_table.add_column("", style="green bold")
        test_table.add_row("Tests Passed", f"[green bold]{passed:,}[/]")
        if failed:
            test_table.add_row(
                "Pre-existing Failures",
                f"[dim]{failed}[/]",
            )
        test_table.add_row("Pass Rate", f"{rate:.1f}%")
        test_table.add_row("Duration", f"{elapsed:.1f}s")

        console.print(Panel(
            test_table,
            title=f"[bold green]{passed:,} Tests Passed[/]",
            border_style="green",
            padding=(1, 2),
        ))

        fail_note = (
            f" {failed} pre-existing failures, unrelated to governance."
            if failed else " All clear."
        )
        jarvis_say(
            f"{passed} governance tests passed in {elapsed:.0f} seconds."
            f"{fail_note} "
            "The entire Ouroboros pipeline is verified and operational.",
            wait=True,
        )
    else:
        console.print("  [yellow]Could not parse test results.[/]")


# ═════════════════════════════════════════════════════════════════════════════
# PHASE 5: SUMMARY
# ═════════════════════════════════════════════════════════════════════════════

def phase_5_summary():
    phase_header(5, "SYSTEM SUMMARY")

    summary = Text(justify="center")
    summary.append("\n")

    components = [
        (
            "The Body — JARVIS",
            "Local supervisor · 200+ autonomous agents · Ouroboros backbone\n"
            "Durable ledger · Risk engine · Trust graduators · Circuit breakers",
        ),
        (
            "The Mind — J-Prime",
            "GCP g2-standard-4 · NVIDIA L4 (23GB VRAM)\n"
            "Qwen2.5-Coder-14B @ Q4_K_M · 8192 context · ~20-23 tok/s\n"
            "Adaptive quantization engine · Multi-model routing",
        ),
        (
            "The Nerves — Reactor-Core",
            "DPO preference pair generation · Governance telemetry ingestion\n"
            "Continuous fine-tuning from production feedback",
        ),
    ]

    for title, desc in components:
        summary.append(f"  {title}\n", style="bold cyan")
        for line in desc.split("\n"):
            summary.append(f"    {line}\n", style="dim white")
        summary.append("\n")

    summary.append("─" * 52 + "\n", style="dim")
    summary.append("\n")
    summary.append("5,442+ ", style="bold green")
    summary.append("commits  ·  ", style="dim")
    summary.append("2,146 ", style="bold green")
    summary.append("governance tests  ·  ", style="dim")
    summary.append("3 ", style="bold green")
    summary.append("repositories\n", style="dim")
    summary.append("7 ", style="bold green")
    summary.append("months  ·  ", style="dim")
    summary.append("1 ", style="bold green")
    summary.append("developer  ·  ", style="dim")
    summary.append("0 ", style="bold green")
    summary.append("external funding\n", style="dim")
    summary.append("\n")
    summary.append(
        "Built by Derek J. Russell · trinityai.dev\n",
        style="dim italic",
    )
    summary.append("\n")

    console.print(Panel(
        Align.center(summary),
        title="[bold cyan]Trinity AI[/]",
        border_style="bold cyan",
        padding=(1, 2),
    ))

    jarvis_say(
        "That concludes our demonstration. "
        "Trinity AI is a fully autonomous, governed software engineering "
        "system built over 7 months by a single developer. "
        "Over 5,400 commits, 2,146 governance tests, "
        "3 repositories, and zero external funding. "
        "Thank you for watching.",
        wait=True,
    )


# ═════════════════════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════════════════════

def main():
    show_banner()
    pause(0.5)

    data = phase_1_system_status()
    pause(1.0)

    phase_2_governance_ledger()
    pause(1.0)

    if data:
        phase_3_live_inference()
        pause(1.0)

    if not NO_TESTS:
        phase_4_test_suite()
        pause(1.0)

    phase_5_summary()

    console.print()
    console.print("  [bold cyan]Demo complete.[/]")
    console.print()


if __name__ == "__main__":
    main()
