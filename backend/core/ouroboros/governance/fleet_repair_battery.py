"""Fleet repair battery — the canonical O+V repair-cycle defect set + runner.

The single source of truth for the DW-driven repair battery: a set of diverse
defects (each = buggy source + a test that catches it) and the pure
GENERATE -> VALIDATE(ast) -> APPLY -> VERIFY(pytest) cycle that runs ONE defect
through an injectable provider caller. `scripts/dw_ov_soak.py` and the
RepairSentinel both import from here — no logic duplication.

The caller matches FleetEvaluator's ``default_model_caller`` contract
(``async (model_id, messages, *, max_tokens) -> ProbeResult``) so the live DW
provider is shared, not re-implemented. NEVER executes model output during
VALIDATE — only ``ast.parse`` (reuses fleet_quality_battery). The APPLY+VERIFY
step DOES run the candidate under pytest in an isolated temp dir off the event
loop (asyncio.to_thread) — that is the O+V VERIFY phase, by design.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Sequence, Tuple

from backend.core.ouroboros.governance.fleet_quality_battery import (
    extract_code_block,
    is_ast_valid,
)

# DW coders proven valid in fleet calibration; tried in cascade order.
DEFAULT_MODELS: Tuple[str, ...] = (
    "deepseek-ai/DeepSeek-V4-Pro",
    "openai/gpt-oss-120b",
    "deepseek-ai/DeepSeek-V4-Flash",
)


@dataclass(frozen=True)
class Defect:
    """One repair target: a buggy function + a test that catches the bug +
    the function's name (so the mutator can rename it consistently)."""
    name: str
    fn_name: str
    buggy_src: str
    test_src: str


@dataclass(frozen=True)
class RepairResult:
    applied: bool
    model: str
    completion_tokens: int
    seconds: float
    note: str


# The canonical battery — 8 diverse defect classes (all proven repairable by DW
# in the 16/16 soak). test_src imports the function from module ``m``.
BATTERY: Tuple[Defect, ...] = (
    Defect("arithmetic", "add_two",
           "def add_two(a, b):\n    return a - b\n",
           "from m import add_two\ndef test():\n    assert add_two(2,3)==5\n    assert add_two(-1,1)==0\n"),
    Defect("comparison", "is_positive",
           "def is_positive(n):\n    return n < 0\n",
           "from m import is_positive\ndef test():\n    assert is_positive(5) is True\n    assert is_positive(-2) is False\n"),
    Defect("off_by_one", "last_index",
           "def last_index(xs):\n    return len(xs)\n",
           "from m import last_index\ndef test():\n    assert last_index([1,2,3])==2\n    assert last_index(['a'])==0\n"),
    Defect("wrong_op_max", "biggest",
           "def biggest(xs):\n    return min(xs)\n",
           "from m import biggest\ndef test():\n    assert biggest([3,7,1])==7\n"),
    Defect("boolean_logic", "both_true",
           "def both_true(a, b):\n    return a or b\n",
           "from m import both_true\ndef test():\n    assert both_true(True, False) is False\n    assert both_true(True, True) is True\n"),
    Defect("string_reverse", "reverse",
           "def reverse(s):\n    return s\n",
           "from m import reverse\ndef test():\n    assert reverse('abc')=='cba'\n"),
    Defect("missing_return", "double",
           "def double(x):\n    result = x * 2\n",
           "from m import double\ndef test():\n    assert double(4)==8\n"),
    Defect("edge_empty", "safe_div",
           "def safe_div(a, b):\n    return a / b\n",
           "from m import safe_div\ndef test():\n    assert safe_div(6,2)==3\n    assert safe_div(5,0)==0\n"),
)

_PROMPT_TMPL = (
    "You are repairing a Python defect. The function below FAILS its unit test. "
    "Fix the bug. Return the COMPLETE corrected function as ONLY a single python "
    "code block — no prose.\n\n```python\n{src}```\n\nFailing test:\n```python\n{test}```"
)


def repair_prompt(buggy_src: str, test_src: str) -> str:
    return _PROMPT_TMPL.format(src=buggy_src, test=test_src)


def _run_pytest(code: str, test_src: str, timeout_s: float) -> bool:
    """APPLY candidate to a temp module + VERIFY via pytest. Blocking — call via
    asyncio.to_thread. Returns True iff the test passes. NEVER raises."""
    try:
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "m.py"), "w", encoding="utf-8") as fh:
                fh.write(code + "\n")
            with open(os.path.join(td, "test_m.py"), "w", encoding="utf-8") as fh:
                fh.write(test_src)
            r = subprocess.run(
                [sys.executable, "-m", "pytest", "test_m.py", "-q"],
                cwd=td, capture_output=True, text=True, timeout=timeout_s,
            )
            return r.returncode == 0
    except Exception:  # noqa: BLE001 — verify failure is a fail, never a raise
        return False


async def repair_one(
    caller: Callable[..., Awaitable[Any]],
    defect: Defect,
    *,
    models: Sequence[str] = DEFAULT_MODELS,
    max_tokens: int = 1024,
    verify_timeout_s: float = 60.0,
) -> RepairResult:
    """Run ONE defect through the full O+V cycle via ``caller`` (FleetEvaluator
    ProbeResult contract). Cascades through ``models``. NEVER raises."""
    prompt = repair_prompt(defect.buggy_src, defect.test_src)
    messages = [{"role": "user", "content": prompt}]
    last_note = "no_models"
    for model in models:
        try:
            pr = await caller(model, messages, max_tokens=max_tokens)
        except Exception as exc:  # noqa: BLE001
            last_note = f"generate_error:{str(exc)[:40]}"
            continue
        if not getattr(pr, "ok", False):
            last_note = f"provider_not_ok:{getattr(pr, 'error', '')[:30]}"
            continue
        text = getattr(pr, "text", "") or ""
        if not is_ast_valid(text):              # VALIDATE — ast.parse only
            last_note = "ast_invalid"
            continue
        code = extract_code_block(text)
        toks = int(getattr(pr, "completion_tokens", 0) or 0)
        total_ms = float(getattr(pr, "total_ms", 0.0) or 0.0)
        verified = await asyncio.to_thread(
            _run_pytest, code, defect.test_src, verify_timeout_s,
        )
        if verified:                            # APPLY + VERIFY passed
            return RepairResult(True, model, toks, total_ms / 1000.0, "state=applied")
        last_note = "verify_failed"
    return RepairResult(False, models[-1] if models else "?", 0, 0.0, last_note)
