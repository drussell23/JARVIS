#!/usr/bin/env python3
"""Live-fire battle test — Rich Formatted Output Control arc.

Resolves the gap quote:
  "Rich formatted output control (structured outputs, specific format):
   O+V relies on prompt discipline."

Scenarios
---------
 1. JSON contract with schema catches missing required field.
 2. JSON contract with regex catches format violation.
 3. Markdown-sections contract detects missing + stub sections.
 4. CSV contract catches missing/unknown columns + parse errors.
 5. YAML contract catches scanner errors without crashing.
 6. Code-block contract catches wrong fence language.
 7. Plain contract enforces length caps.
 8. Extractor hints capture groups on every validation.
 9. Repair loop converges when model fixes output on retry.
10. Repair loop exhausts bounded attempts and fails fast.
11. Renderer registry formats per surface.
12. /format REPL: list / show / validate round-trip.
13. §1 authority: contracts authored by operator / orchestrator only.
14. Authority invariant grep on arc modules.

Run::
    python3 scripts/livefire_output.py
"""
from __future__ import annotations

import asyncio
import re as _re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from backend.core.ouroboros.governance.output_contract import (  # noqa: E402
    MarkdownSection,
    OutputContract,
    OutputFormat,
)
from backend.core.ouroboros.governance.output_validator import (  # noqa: E402
    OutputContractRegistry,
    OutputExtractor,
    OutputRendererRegistry,
    OutputRepairLoop,
    OutputRepairPrompt,
    OutputValidator,
    RenderSurface,
    build_repair_prompt,
    dispatch_format_command,
    get_default_renderer_registry,
)


C_PASS, C_FAIL, C_BOLD, C_DIM, C_END = (
    "\033[92m", "\033[91m", "\033[1m", "\033[2m", "\033[0m",
)


def _banner(text: str) -> None:
    print(f"\n{C_BOLD}{'━' * 72}{C_END}\n{C_BOLD}▶ {text}{C_END}\n{C_BOLD}{'━' * 72}{C_END}")


def _pass(t: str) -> None:
    print(f"  {C_PASS}✓ {t}{C_END}")


def _fail(t: str) -> None:
    print(f"  {C_FAIL}✗ {t}{C_END}")


class Scenario:
    def __init__(self, title: str) -> None:
        self.title = title
        self.passed: List[str] = []
        self.failed: List[str] = []

    def check(self, d: str, ok: bool) -> None:
        (self.passed if ok else self.failed).append(d)
        (_pass if ok else _fail)(d)

    @property
    def ok(self) -> bool:
        return not self.failed


async def scenario_json_missing_required() -> Scenario:
    """JSON contract catches missing required field."""
    s = Scenario("JSON: missing required field → issue")
    c = OutputContract.from_mapping({
        "name": "result", "format": "json",
        "schema": {
            "fields": {
                "ok": {"type": "boolean", "required": True},
                "data": {"type": "object", "required": True},
            },
        },
    })
    r = OutputValidator().validate(c, '{"ok": true}')
    s.check(f"ok=False (got {r.ok})", r.ok is False)
    s.check(
        "missing_required_field issue present",
        any(i.code == "missing_required_field" for i in r.issues),
    )
    return s


async def scenario_json_regex() -> Scenario:
    """JSON contract catches regex format violation."""
    s = Scenario("JSON: regex mismatch")
    c = OutputContract.from_mapping({
        "name": "r", "format": "json",
        "schema": {
            "fields": {"id": {"type": "string", "regex": r"^op-\d+$"}},
        },
    })
    r = OutputValidator().validate(c, '{"id": "not-matching"}')
    s.check(
        "regex_mismatch reported",
        any(i.code == "regex_mismatch" for i in r.issues),
    )
    r2 = OutputValidator().validate(c, '{"id": "op-42"}')
    s.check("valid id passes", r2.ok)
    return s


async def scenario_markdown_sections() -> Scenario:
    """Markdown-sections catches missing / too-short sections."""
    s = Scenario("Markdown sections: missing + short body")
    c = OutputContract.from_mapping({
        "name": "pr", "format": "markdown_sections",
        "sections": [
            {"name": "Summary", "min_body_chars": 20},
            {"name": "Test plan"},
        ],
    })
    r = OutputValidator().validate(c, "## Summary\nhi\n")
    s.check(
        "missing_section for 'Test plan'",
        any(
            i.code == "missing_section" and "Test plan" in i.message
            for i in r.issues
        ),
    )
    s.check(
        "section_body_too_short for 'Summary'",
        any(i.code == "section_body_too_short" for i in r.issues),
    )
    return s


async def scenario_csv_columns() -> Scenario:
    """CSV catches missing/unknown columns + parse errors."""
    s = Scenario("CSV: missing column + unknown column")
    c = OutputContract.from_mapping({
        "name": "report", "format": "csv",
        "schema": {
            "fields": {
                "name": {"type": "string", "required": True},
            },
            "strict": True,
        },
    })
    r = OutputValidator().validate(c, "not_name,extra\nalice,x\n")
    s.check(
        "missing_csv_column for 'name'",
        any(i.code == "missing_csv_column" for i in r.issues),
    )
    s.check(
        "unknown_csv_column for unexpected columns",
        any(i.code == "unknown_csv_column" for i in r.issues),
    )
    return s


async def scenario_yaml_graceful() -> Scenario:
    """YAML catches scanner errors without crashing."""
    s = Scenario("YAML: scanner error captured as issue")
    c = OutputContract.from_mapping({
        "name": "cfg", "format": "yaml",
    })
    r = OutputValidator().validate(c, "not: valid: yaml: [[[")
    s.check("ok=False", r.ok is False)
    s.check(
        "yaml_parse_error issue present",
        any(i.code == "yaml_parse_error" for i in r.issues),
    )
    # Good YAML passes
    r2 = OutputValidator().validate(c, "key: value\nport: 8080\n")
    s.check("clean YAML passes", r2.ok)
    return s


async def scenario_code_block_wrong_lang() -> Scenario:
    """Code-block contract catches wrong fence language."""
    s = Scenario("Code block: wrong fence language")
    c = OutputContract.from_mapping({
        "name": "snippet", "format": "code_block",
        "fence_language": "python",
    })
    r = OutputValidator().validate(c, "```javascript\nconsole.log(1)\n```")
    s.check(
        "code_block_error reported",
        any(i.code == "code_block_error" for i in r.issues),
    )
    r2 = OutputValidator().validate(c, "```python\nprint('hi')\n```")
    s.check("correct language passes", r2.ok)
    return s


async def scenario_plain_length_caps() -> Scenario:
    """Plain contract enforces min / max length."""
    s = Scenario("Plain: length caps enforced")
    c = OutputContract.from_mapping({
        "name": "notes", "format": "plain",
        "min_length_chars": 10, "max_length_chars": 100,
    })
    r_short = OutputValidator().validate(c, "tiny")
    s.check(
        "under_min_length reported",
        any(i.code == "under_min_length" for i in r_short.issues),
    )
    r_long = OutputValidator().validate(c, "x" * 1000)
    s.check(
        "over_max_length reported",
        any(i.code == "over_max_length" for i in r_long.issues),
    )
    return s


async def scenario_extractor_hints() -> Scenario:
    """Extractor hints capture groups on every validation."""
    s = Scenario("Extractor hints: always populated")
    c = OutputContract.from_mapping({
        "name": "p", "format": "plain",
        "extractor_hints": [r"file:\s*(\S+)"],
    })
    r = OutputValidator().validate(
        c, "file: backend/auth.py\nfile: tests/foo.py\n"
    )
    hints = r.extracted.get("hints", {})
    files = hints.get(r"file:\s*(\S+)", [])
    s.check(
        f"extracted 2 files (got {files})",
        files == ["backend/auth.py", "tests/foo.py"],
    )
    return s


async def scenario_repair_loop_converges() -> Scenario:
    """Repair loop converges when model fixes output."""
    s = Scenario("Repair loop: converges after one retry")
    c = OutputContract.from_mapping({
        "name": "c", "format": "json",
        "schema": {"fields": {"ok": {"type": "boolean", "required": True}}},
    })

    def _simulated_model(_orig: str, prompt: OutputRepairPrompt) -> str:
        # The repair prompt told us what's missing → return it
        assert "ok" in prompt.text
        return '{"ok": true}'

    loop = OutputRepairLoop(max_attempts=3)
    outcome = loop.run(
        contract=c, original_prompt="give me ok",
        initial_raw='{"wrong_field": 1}', model_fn=_simulated_model,
    )
    s.check("converged", outcome.converged)
    s.check(
        f"converged on attempt {outcome.attempts} (=1)",
        outcome.attempts == 1,
    )
    s.check("exactly 1 repair prompt built", len(outcome.repair_prompts) == 1)
    return s


async def scenario_repair_loop_exhausts() -> Scenario:
    """Repair loop exhausts bounded attempts."""
    s = Scenario("Repair loop: exhausts bounded attempts")
    c = OutputContract.from_mapping({
        "name": "c", "format": "json",
        "schema": {"fields": {"ok": {"type": "boolean", "required": True}}},
    })

    def _stubborn_model(_o: str, _p: OutputRepairPrompt) -> str:
        return "still nonsense"

    loop = OutputRepairLoop(max_attempts=2)
    outcome = loop.run(
        contract=c, original_prompt="x",
        initial_raw="nope", model_fn=_stubborn_model,
    )
    s.check("did NOT converge", outcome.converged is False)
    s.check(
        f"bounded to 2 attempts (got {outcome.attempts})",
        outcome.attempts == 2,
    )
    return s


async def scenario_renderer_per_surface() -> Scenario:
    """Renderer emits surface-specific formatted output."""
    s = Scenario("Renderer: REPL + IDE variants differ")
    reg = get_default_renderer_registry()
    c = OutputContract.from_mapping({
        "name": "pr", "format": "markdown_sections",
        "sections": [{"name": "Summary"}],
    })
    r = OutputValidator().validate(c, "## Summary\nshort body here\n")
    repl = reg.render(
        result=r, format=OutputFormat.MARKDOWN_SECTIONS,
        surface=RenderSurface.REPL,
    )
    ide = reg.render(
        result=r, format=OutputFormat.MARKDOWN_SECTIONS,
        surface=RenderSurface.IDE,
    )
    s.check("REPL render non-empty", bool(repl))
    s.check("IDE render non-empty", bool(ide))
    s.check(
        "REPL and IDE variants differ",
        repl != ide,
    )
    return s


async def scenario_repl_round_trip() -> Scenario:
    """/format REPL: list / show / validate round-trip."""
    s = Scenario("/format REPL full round trip")
    reg = OutputContractRegistry()
    reg.register(OutputContract.from_mapping({
        "name": "greet", "format": "json",
        "schema": {
            "fields": {"hello": {"type": "string", "required": True}},
        },
        "description": "a greeting contract",
    }))
    r_list = dispatch_format_command("/format list", contract_registry=reg)
    s.check("/format list ok", r_list.ok)
    s.check("contract 'greet' appears", "greet" in r_list.text)

    r_show = dispatch_format_command(
        "/format show greet", contract_registry=reg,
    )
    s.check("/format show ok", r_show.ok)
    s.check("description shown", "greeting contract" in r_show.text)

    r_valid = dispatch_format_command(
        '/format validate greet {"hello":"world"}',
        contract_registry=reg,
    )
    s.check("/format validate reports ok=True", "ok=True" in r_valid.text)

    r_invalid = dispatch_format_command(
        '/format validate greet {"other":"x"}',
        contract_registry=reg,
    )
    s.check(
        "/format validate reports ok=False on missing field",
        "ok=False" in r_invalid.text,
    )
    return s


async def scenario_authority_invariant() -> Scenario:
    """Arc modules import no gate/execution code."""
    s = Scenario("Authority invariant grep")
    forbidden = [
        "orchestrator", "policy_engine", "iron_gate", "risk_tier_floor",
        "semantic_guardian", "tool_executor", "candidate_generator",
        "change_engine",
    ]
    modules = [
        "backend/core/ouroboros/governance/output_contract.py",
        "backend/core/ouroboros/governance/output_validator.py",
    ]
    for path in modules:
        src = Path(path).read_text()
        violations = []
        for mod in forbidden:
            if _re.search(
                rf"^\s*(from|import)\s+[^#\n]*{_re.escape(mod)}",
                src, _re.MULTILINE,
            ):
                violations.append(mod)
        s.check(
            f"{Path(path).name}: zero forbidden imports",
            not violations,
        )
    return s


ALL_SCENARIOS = [
    scenario_json_missing_required,
    scenario_json_regex,
    scenario_markdown_sections,
    scenario_csv_columns,
    scenario_yaml_graceful,
    scenario_code_block_wrong_lang,
    scenario_plain_length_caps,
    scenario_extractor_hints,
    scenario_repair_loop_converges,
    scenario_repair_loop_exhausts,
    scenario_renderer_per_surface,
    scenario_repl_round_trip,
    scenario_authority_invariant,
]


async def main() -> int:
    print(f"{C_BOLD}Rich Formatted Output Control — live-fire{C_END}")
    print(f"{C_DIM}Contract + validator + extractor + repair + renderer + REPL{C_END}")
    t0 = time.monotonic()
    results: List[Scenario] = []
    for fn in ALL_SCENARIOS:
        title = fn.__doc__.splitlines()[0] if fn.__doc__ else fn.__name__
        _banner(title)
        try:
            results.append(await fn())
        except Exception as exc:
            sc = Scenario(fn.__name__)
            sc.failed.append(f"raised: {type(exc).__name__}: {exc}")
            _fail(f"raised: {type(exc).__name__}: {exc}")
            import traceback
            traceback.print_exc()
            results.append(sc)
    elapsed = time.monotonic() - t0
    _banner("SUMMARY")
    total_pass = sum(len(s.passed) for s in results)
    total_fail = sum(len(s.failed) for s in results)
    ok = sum(1 for s in results if s.ok)
    for sc in results:
        status = f"{C_PASS}PASS{C_END}" if sc.ok else f"{C_FAIL}FAIL{C_END}"
        print(f"  {status} {sc.title}  ({len(sc.passed)} ✓, {len(sc.failed)} ✗)")
    print()
    print(
        f"  {C_BOLD}Total:{C_END} {total_pass} checks passed, "
        f"{total_fail} failed — {ok}/{len(results)} scenarios OK"
    )
    print(f"  {C_DIM}elapsed: {elapsed:.2f}s{C_END}")
    print()
    if total_fail == 0:
        print(
            f"  {C_PASS}{C_BOLD}"
            f"RICH FORMATTED OUTPUT CONTROL GAP: CLOSED"
            f"{C_END}"
        )
        return 0
    print(
        f"  {C_FAIL}{C_BOLD}{total_fail} check(s) failed{C_END}"
    )
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
