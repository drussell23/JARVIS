"""DW-primary O+V soak (2026-06-20).

Soaks the DoubleWord-driven O+V repair cycle over a BATTERY of diverse real
defects, multiple rounds, reporting per-op state=applied + aggregate success
rate / latency / cost. Pure DW autarky (no Claude, no J-Prime). Runs on a 16GB
M1 because it exercises only the core repair cycle (GENERATE -> VALIDATE/ast ->
APPLY -> VERIFY/pytest), NOT the 6-layer sensor stack that starves the loop.

Run: DOUBLEWORD_API_KEY=... python3 scripts/dw_ov_soak.py [rounds]
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
MODELS = ["deepseek-ai/DeepSeek-V4-Pro", "openai/gpt-oss-120b", "deepseek-ai/DeepSeek-V4-Flash"]
OUT_PER_MTOK = 1.10  # DeepSeek-V4 output tier, for a cost estimate

# Each defect: (name, buggy_source, test_body). The test CATCHES the bug.
DEFECTS = [
    ("arithmetic", "def add_two(a, b):\n    return a - b\n",
     "from m import add_two\ndef test():\n    assert add_two(2,3)==5\n    assert add_two(-1,1)==0\n"),
    ("comparison", "def is_positive(n):\n    return n < 0\n",
     "from m import is_positive\ndef test():\n    assert is_positive(5) is True\n    assert is_positive(-2) is False\n"),
    ("off_by_one", "def last_index(xs):\n    return len(xs)\n",
     "from m import last_index\ndef test():\n    assert last_index([1,2,3])==2\n    assert last_index(['a'])==0\n"),
    ("wrong_op_max", "def biggest(xs):\n    return min(xs)\n",
     "from m import biggest\ndef test():\n    assert biggest([3,7,1])==7\n"),
    ("boolean_logic", "def both_true(a, b):\n    return a or b\n",
     "from m import both_true\ndef test():\n    assert both_true(True, False) is False\n    assert both_true(True, True) is True\n"),
    ("string_reverse", "def reverse(s):\n    return s\n",
     "from m import reverse\ndef test():\n    assert reverse('abc')=='cba'\n"),
    ("missing_return", "def double(x):\n    result = x * 2\n",
     "from m import double\ndef test():\n    assert double(4)==8\n"),
    ("edge_empty", "def safe_div(a, b):\n    return a / b\n",
     "from m import safe_div\ndef test():\n    assert safe_div(6,2)==3\n    assert safe_div(5,0)==0\n"),
]

PROMPT_TMPL = (
    "You are repairing a Python defect. The function below FAILS its unit test. "
    "Fix the bug. Return the COMPLETE corrected function as ONLY a single python "
    "code block — no prose.\n\n```python\n{src}```\n\nFailing test:\n```python\n{test}```"
)


def dw_generate(model, prompt):
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}],
                       "max_tokens": 1024, "temperature": 0.0}).encode()
    req = urllib.request.Request(BASE + "/chat/completions", data=body,
        headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"})
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=90) as r:
        d = json.load(r)
    return (d["choices"][0]["message"].get("content") or "",
            int(d.get("usage", {}).get("completion_tokens", 0) or 0), time.monotonic() - t0)


def extract(text):
    m = re.search(r"```(?:python)?\s*(.*?)```", text or "", re.S)
    return (m.group(1) if m else (text or "")).strip()


def repair_one(_name, src, test):
    """Full O+V cycle for one defect. Returns (applied, model, toks, secs, note)."""
    prompt = PROMPT_TMPL.format(src=src, test=test)
    for model in MODELS:
        try:
            text, toks, dt = dw_generate(model, prompt)
        except Exception as e:
            return (False, model, 0, 0.0, f"generate_error:{str(e)[:40]}")
        code = extract(text)
        try:
            ast.parse(code)               # VALIDATE
        except Exception:
            continue                      # ast-invalid -> next model
        with tempfile.TemporaryDirectory() as td:  # APPLY + VERIFY
            with open(os.path.join(td, "m.py"), "w") as fh:
                fh.write(code + "\n")
            with open(os.path.join(td, "test_m.py"), "w") as fh:
                fh.write(test)
            r = subprocess.run([sys.executable, "-m", "pytest", "test_m.py", "-q"],
                               cwd=td, capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            return (True, model, toks, dt, "state=applied")
        # verified-fail -> try next model (capability cascade)
    return (False, MODELS[-1], 0, 0.0, "all_models_failed_verify")


def main():
    if not KEY:
        print("ERROR: DOUBLEWORD_API_KEY not set"); return 2
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    print(f"=== DW-primary O+V soak: {len(DEFECTS)} defects x {rounds} rounds "
          f"(pure DW autarky) ===\n")
    applied = total = toks_sum = 0
    lat = []
    for rnd in range(1, rounds + 1):
        print(f"── round {rnd}/{rounds} ──")
        for name, src, test in DEFECTS:
            ok, model, toks, dt, note = repair_one(name, src, test)
            total += 1; toks_sum += toks
            if ok:
                applied += 1; lat.append(dt)
            mark = "✅" if ok else "❌"
            print(f"  {mark} {name:16} {note:22} model={model.split('/')[-1]:22} "
                  f"{dt:4.1f}s {toks:4}tok")
        print()
    rate = 100.0 * applied / total if total else 0.0
    mean_lat = sum(lat) / len(lat) if lat else 0.0
    print("═══════════════════════════════════════════════════════════")
    print(f"  state=applied : {applied}/{total}  ({rate:.0f}%)")
    print(f"  mean latency  : {mean_lat:.1f}s (applied ops)")
    print(f"  total tokens  : {toks_sum}  (~${toks_sum/1_000_000*OUT_PER_MTOK:.5f})")
    print("═══════════════════════════════════════════════════════════")
    return 0 if applied == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
