"""DW-primary O+V repair demo (2026-06-19).

Proves the DoubleWord provider can drive the core O+V repair cycle —
GENERATE -> VALIDATE(ast) -> APPLY -> VERIFY(pytest) -> state=applied — on a
real defect, WITHOUT booting the full 6-layer battle-test stack (which is what
starves the 16GB M1; a single generation does not). Claude + J-Prime are out of
scope by design: this is pure DW autarky.

Run: DOUBLEWORD_API_KEY=... python3 scripts/dw_ov_repair_demo.py
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request

KEY = os.environ.get("DOUBLEWORD_API_KEY", "")
BASE = os.environ.get("DOUBLEWORD_BASE_URL", "https://api.doubleword.ai/v1").rstrip("/")
# DW coders that passed the fleet calibration (valid AST output). Try in order.
MODELS = [
    "deepseek-ai/DeepSeek-V4-Pro",
    "openai/gpt-oss-120b",
    "deepseek-ai/DeepSeek-V4-Flash",
]

BUGGY = '''def add_two(a, b):
    """Return the sum of two integers."""
    return a - b  # BUG: should be a + b
'''

# An O+V-style repair instruction (mirrors the GENERATE prompt's contract).
REPAIR_PROMPT = (
    "You are repairing a Python defect. The function below FAILS its unit test "
    "`assert add_two(2, 3) == 5` because it subtracts instead of adds.\n\n"
    "```python\n" + BUGGY + "```\n\n"
    "Return the COMPLETE corrected function as ONLY a single python code block. "
    "No prose."
)


def dw_generate(model: str) -> tuple[str, int, float]:
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": REPAIR_PROMPT}],
        "max_tokens": 1024,
        "temperature": 0.0,
    }).encode()
    req = urllib.request.Request(
        BASE + "/chat/completions", data=body,
        headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"},
    )
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=90) as r:
        d = json.load(r)
    dt = time.monotonic() - t0
    text = d["choices"][0]["message"]["content"] or ""
    toks = int(d.get("usage", {}).get("completion_tokens", 0) or 0)
    return text, toks, dt


def extract_code(text: str) -> str:
    m = re.search(r"```(?:python)?\s*(.*?)```", text or "", re.S)
    return (m.group(1) if m else (text or "")).strip()


def main() -> int:
    if not KEY:
        print("ERROR: DOUBLEWORD_API_KEY not set"); return 2
    print("=== DW-primary O+V repair cycle (Claude + J-Prime out of scope) ===\n")
    for model in MODELS:
        print(f"-- provider=doubleword model={model}")
        try:
            text, toks, dt = dw_generate(model)
        except Exception as e:
            print(f"   GENERATE failed: {str(e)[:80]} — trying next model\n"); continue
        code = extract_code(text)
        # VALIDATE — ast.parse gate (the O+V parser gate)
        try:
            ast.parse(code)
        except Exception as e:
            print(f"   VALIDATE ast.parse FAILED: {e} — next model\n"); continue
        print(f"   GENERATE ok: {toks} tok in {dt:.1f}s | VALIDATE ast=OK")
        # APPLY — write candidate to a temp module + its test
        with tempfile.TemporaryDirectory() as td:
            modp = os.path.join(td, "fixed_util.py")
            testp = os.path.join(td, "test_fixed_util.py")
            with open(modp, "w") as fh:
                fh.write(code + "\n")
            with open(testp, "w") as fh:
                fh.write(
                    "from fixed_util import add_two\n"
                    "def test_sum():\n"
                    "    assert add_two(2, 3) == 5\n"
                    "    assert add_two(-1, 1) == 0\n"
                )
            # VERIFY — run the test against the applied candidate
            r = subprocess.run(
                [sys.executable, "-m", "pytest", testp, "-q"],
                cwd=td, capture_output=True, text=True, timeout=60,
            )
            verified = r.returncode == 0
        verdict = "state=applied ✅" if verified else "VERIFY FAILED ❌"
        print(f"   APPLY ok | VERIFY pytest={'PASS' if verified else 'FAIL'} -> {verdict}")
        print(f"\n   fixed function:\n{chr(10).join('     '+l for l in code.splitlines())}\n")
        if verified:
            print(f"🎯 DW autarky drove a full O+V repair to state=applied "
                  f"(model={model}, ${toks/1_000_000*1.1:.5f} est).")
            return 0
        print()
    print("❌ no DW model completed the repair cycle")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
