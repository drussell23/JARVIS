#!/usr/bin/env python3
"""Vision benchmark: does the local vision lane read screens correctly?

Each case is RENDERED here, deterministically, with the host's own UI fonts,
so its ground truth is exact rather than remembered: terminal tracebacks,
test summaries, dialogs, code at a line number, tables, toggles, progress
bars, small and low-contrast text, counting. The question is asked through
the PRODUCTION client (backend.vision.local_vision_client.LocalVisionClient)
-- the same endpoint resolution, payload and failure handling O+V uses -- so
a pass here is a pass of the real path, not of a test harness.

Alongside accuracy it watches the serving engine: which models are resident
before and after every request (a primary evicted to admit vision is a
failure even when every answer is right), VRAM and GPU temperature.

    python3 scripts/benchmarks/vision_benchmark.py [--out report.json] [--generation-probe]

``--generation-probe`` interleaves a short generation on the primary model
between vision requests, the co-residency pattern a live session produces.
Exit 0 when every engine invariant held; the accuracy is reported, not
gated -- what is good enough is the operator's call.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_FONT_DIRS = [Path("/mnt/c/Windows/Fonts"), Path("C:/Windows/Fonts"), Path("/usr/share/fonts/truetype/dejavu")]
_FONT_FILES = {"ui": ("segoeui.ttf", "DejaVuSans.ttf"), "ui_bold": ("segoeuib.ttf", "DejaVuSans-Bold.ttf"),
               "mono": ("consola.ttf", "DejaVuSansMono.ttf")}


def font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    for name in _FONT_FILES[kind]:
        for d in _FONT_DIRS:
            if (d / name).is_file():
                return ImageFont.truetype(str(d / name), size)
    return ImageFont.load_default()


BG_DARK, FG_LIGHT, BG_LIGHT, FG_DARK = (30, 30, 30), (220, 220, 220), (243, 243, 243), (30, 30, 30)


def canvas(w: int = 1280, h: int = 720, bg=BG_LIGHT) -> Tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGB", (w, h), bg)
    return img, ImageDraw.Draw(img)


def window(d: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int, title: str, *, dark=False) -> None:
    d.rectangle([x, y, x + w, y + h], fill=BG_DARK if dark else (255, 255, 255), outline=(120, 120, 120))
    d.rectangle([x, y, x + w, y + 34], fill=(45, 45, 48) if dark else (225, 225, 225))
    d.text((x + 12, y + 6), title, font=font("ui", 16), fill=FG_LIGHT if dark else FG_DARK)


def lines(d, x, y, rows: Sequence[str], *, kind="mono", size=18, fill=FG_LIGHT, step=None) -> None:
    f = font(kind, size)
    for i, r in enumerate(rows):
        d.text((x, y + i * (step or int(size * 1.45))), r, font=f, fill=fill)


@dataclass
class Case:
    name: str
    category: str
    question: str
    expect: List[str]                      # every token must appear (normalised); "re:" = regex
    render: Callable[[], Image.Image]
    forbid: List[str] = field(default_factory=list)


def _terminal(title: str, rows: Sequence[str], size=18) -> Image.Image:
    img, d = canvas(bg=(200, 200, 200))
    window(d, 40, 40, 1200, 640, title, dark=True)
    lines(d, 60, 90, rows, size=size)
    return img


def cases() -> List[Case]:
    out: List[Case] = []
    add = out.append

    add(Case("traceback", "terminal", "This terminal shows a Python error. What is the exception type, and what "
             "string value caused it? Answer briefly.", ["valueerror", "abc"], lambda: _terminal("bash", [
                 "$ python3 scripts/import_rows.py data/rows.csv",
                 "Traceback (most recent call last):",
                 '  File "scripts/import_rows.py", line 42, in <module>',
                 "    count = int(row['qty'])",
                 "ValueError: invalid literal for int() with base 10: 'abc'"])))
    add(Case("pytest_summary", "terminal", "How many tests failed according to the summary line? Answer with a number.",
             ["3"], lambda: _terminal("pytest", [
                 "tests/test_router.py ........F..                    [ 60%]",
                 "tests/test_cache.py ....FF..                        [100%]",
                 "",
                 "=================== short test summary info ===================",
                 "FAILED tests/test_router.py::test_fallback - AssertionError",
                 "FAILED tests/test_cache.py::test_ttl - KeyError: 'k'",
                 "FAILED tests/test_cache.py::test_evict - TimeoutError",
                 "============== 3 failed, 41 passed in 12.31s ==============="]), forbid=["41 failed"]))
    add(Case("git_status", "terminal", "Which file is shown as modified (not staged)? Give the path.",
             ["backend/vision/local_vision_client.py"], lambda: _terminal("git", [
                 "$ git status", "On branch main",
                 "Changes to be committed:", "        new file:   tests/vision/test_benchmark.py", "",
                 "Changes not staged for commit:",
                 "        modified:   backend/vision/local_vision_client.py", "",
                 "Untracked files:", "        notes.txt"])))

    def dialog() -> Image.Image:
        img, d = canvas()
        window(d, 340, 220, 600, 260, "Disk Space Low")
        lines(d, 370, 280, ["You are running out of disk space on Local Disk (D:).",
                            "Only 4.2 GB remains available."], kind="ui", size=19, fill=FG_DARK)
        for bx, label in ((620, "Cancel"), (750, "Free up space")):
            d.rectangle([bx, 420, bx + (100 if label == "Cancel" else 170), 456], outline=(0, 95, 184), width=2)
            d.text((bx + 14, 426), label, font=font("ui", 17), fill=FG_DARK)
        return img
    add(Case("dialog", "dialog", "Which drive letter is low on space, and how much space remains?", [r"re:\bd\b", "4.2"], dialog,
             forbid=[r"re:\bc:"]))
    add(Case("dialog_buttons", "dialog", "What are the labels of the two buttons in the dialog?",
             ["cancel", "free up space"], dialog))

    def editor() -> Image.Image:
        img, d = canvas(bg=(37, 37, 38))
        code = ["from __future__ import annotations", "", "import asyncio", "import json", "",
                "", "class LaneAdmission:", '    """Who may use the lane."""', "", "    admitting: bool",
                "    holder: str", "", "", "def read_admission(base_url: str) -> LaneAdmission:",
                '    """One reading of both sources."""', "    return LaneAdmission(True, '')"]
        f = font("mono", 18)
        for i, c in enumerate(code, 1):
            d.text((30, 30 + (i - 1) * 27), f"{i:>3}", font=f, fill=(120, 120, 120))
            d.text((90, 30 + (i - 1) * 27), c, font=f, fill=(212, 212, 212))
        return img
    add(Case("code_line", "code", "In this code editor, what is the name of the function defined on line 14?",
             ["read_admission"], editor))
    add(Case("code_class", "code", "What class is defined in this file?", ["laneadmission"], editor))

    def table() -> Image.Image:
        img, d = canvas()
        rows = [("Model", "p50 latency", "Accuracy"), ("qwen3-coder-ov:30b", "812 ms", "71%"),
                ("qwen2.5-coder:32b", "1430 ms", "64%"), ("jarvis-vision:8b", "390 ms", "n/a"),
                ("qwen2.5-coder:7b", "205 ms", "48%")]
        for r, row in enumerate(rows):
            for c, cell in enumerate(row):
                x, y = 120 + c * 330, 140 + r * 60
                d.rectangle([x, y, x + 330, y + 60], outline=(170, 170, 170),
                            fill=(225, 230, 240) if r == 0 else (255, 255, 255))
                d.text((x + 14, y + 17), cell, font=font("ui_bold" if r == 0 else "ui", 20), fill=FG_DARK)
        return img
    add(Case("table_min", "table", "Which model has the lowest p50 latency?", ["qwen2.5-coder:7b"], table))
    add(Case("table_lookup", "table", "What accuracy does qwen2.5-coder:32b have?", ["64"], table))

    def toggles() -> Image.Image:
        img, d = canvas()
        for i, (label, on) in enumerate((("Wi-Fi", True), ("Bluetooth", False), ("Airplane mode", False),
                                         ("Location", True))):
            y = 160 + i * 90
            d.text((300, y), label, font=font("ui", 24), fill=FG_DARK)
            x = 760
            d.rounded_rectangle([x, y, x + 76, y + 36], radius=18, fill=(0, 103, 192) if on else (190, 190, 190))
            cx = x + 58 if on else x + 18
            d.ellipse([cx - 14, y + 4, cx + 14, y + 32], fill=(255, 255, 255))
            d.text((x + 100, y + 2), "On" if on else "Off", font=font("ui", 22), fill=FG_DARK)
        return img
    add(Case("toggles", "controls", "Which settings are switched OFF?", ["bluetooth", "airplane"], toggles,
             forbid=["wi-fi is off", "location is off"]))

    def progress() -> Image.Image:
        img, d = canvas()
        d.text((300, 280), "Installing updates... please keep your PC on", font=font("ui", 24), fill=FG_DARK)
        d.rectangle([300, 340, 980, 372], outline=(150, 150, 150), fill=(230, 230, 230))
        d.rectangle([300, 340, 300 + int(680 * 0.73), 372], fill=(0, 120, 212))
        d.text((300, 390), "73% complete", font=font("ui", 20), fill=FG_DARK)
        return img
    add(Case("progress", "controls", "What percentage complete is the installation?", ["73"], progress))

    def circles() -> Image.Image:
        img, d = canvas()
        for i, col in enumerate(((220, 50, 50), (50, 160, 60), (40, 90, 220), (230, 180, 30), (150, 60, 190))):
            x = 200 + i * 190
            d.ellipse([x, 300, x + 110, 410], fill=col)
        return img
    add(Case("count", "counting", "How many circles are in the image? Answer with a number.", ["5"], circles))

    def small_text() -> Image.Image:
        img, d = canvas()
        lines(d, 40, 40, [f"2026-10-07T09:{m:02d}:11Z INFO  request_id=7f3a{m:02d}c1 GET /health 200 3ms"
                          for m in range(0, 40, 2)], size=11, fill=(60, 60, 60), step=16)
        lines(d, 40, 380, ["2026-10-07T09:41:58Z ERROR request_id=9b41e0d7 POST /v1/chat/completions 503 lease held"],
              size=11, fill=(60, 60, 60))
        return img
    add(Case("small_text", "stress", "Find the ERROR line. What is its request_id and HTTP status code?",
             ["9b41e0d7", "503"], small_text))

    def low_contrast() -> Image.Image:
        img, d = canvas(bg=(240, 240, 240))
        d.text((360, 330), "Last synced: 3 minutes ago", font=font("ui", 22), fill=(185, 185, 185))
        return img
    add(Case("low_contrast", "stress", "When was the last sync, according to the faint gray text?", ["3 minutes"],
             low_contrast))

    def address_bar() -> Image.Image:
        img, d = canvas()
        d.rectangle([0, 0, 1280, 90], fill=(222, 225, 230))
        d.rounded_rectangle([140, 26, 1180, 64], radius=18, fill=(255, 255, 255))
        d.text((170, 33), "https://docs.python.org/3/library/asyncio-subprocess.html", font=font("ui", 20),
               fill=FG_DARK)
        d.text((140, 160), "Subprocesses", font=font("ui_bold", 40), fill=FG_DARK)
        return img
    add(Case("url", "browser", "What domain is the browser showing?", ["docs.python.org"], address_bar))

    def toast() -> Image.Image:
        img, d = canvas()
        d.rectangle([780, 560, 1250, 690], fill=(196, 43, 28))
        d.text((800, 575), "Build failed", font=font("ui_bold", 24), fill=(255, 255, 255))
        d.text((800, 620), "2 errors, 5 warnings in jarvis-prime", font=font("ui", 19), fill=(255, 255, 255))
        return img
    add(Case("toast", "notification", "How many errors does the build notification report?", ["2"], toast,
             forbid=["5 errors"]))

    def json_view() -> Image.Image:
        return _terminal("config.json", ["{", '  "service": "jarvis_prime",', '  "host": "127.0.0.1",',
                                         '  "port": 8000,', '  "engine": {', '    "binary": "llama-server",',
                                         '    "parallel": 1', "  }", "}"])
    add(Case("json", "code", 'What is the value of "port" in this JSON?', ["8000"], json_view))

    def title_bar() -> Image.Image:
        img, d = canvas()
        window(d, 0, 0, 1279, 719, "Q3_capacity_review_v4.xlsx - Excel")
        return img
    add(Case("title", "window", "What document is open, according to the title bar?", ["q3_capacity_review_v4"],
             title_bar))

    def diff() -> Image.Image:
        img, d = canvas(bg=(30, 30, 30))
        rows = [(" def admit(self):", FG_LIGHT), ("-    return True", (240, 110, 110)),
                ("+    return self.state == 'serving'", (120, 220, 120)), ("     # end", FG_LIGHT)]
        f = font("mono", 20)
        for i, (r, col) in enumerate(rows):
            d.text((60, 120 + i * 34), r, font=f, fill=col)
        return img
    add(Case("diff", "code", "In this diff, what line was added (the green + line)?", ["self.state", "serving"], diff))

    def checklist() -> Image.Image:
        img, d = canvas()
        for i, (label, on) in enumerate((("Run unit tests", True), ("Update changelog", False),
                                         ("Tag release", True), ("Notify team", False))):
            y = 200 + i * 70
            d.rectangle([360, y, 392, y + 32], outline=(80, 80, 80), width=2, fill=(0, 103, 192) if on else None)
            if on:
                d.line([366, y + 16, 376, y + 26, 388, y + 6], fill=(255, 255, 255), width=4)
            d.text((410, y), label, font=font("ui", 24), fill=FG_DARK)
        return img
    add(Case("checklist", "controls", "Which items are checked?", ["unit tests", "tag release"], checklist,
             forbid=["changelog is checked"]))

    def chart() -> Image.Image:
        img, d = canvas()
        for i, (label, v) in enumerate((("Mon", 120), ("Tue", 340), ("Wed", 210), ("Thu", 90), ("Fri", 260))):
            x = 220 + i * 180
            d.rectangle([x, 620 - v, x + 110, 620], fill=(0, 120, 212))
            d.text((x + 30, 635), label, font=font("ui", 22), fill=FG_DARK)
        return img
    add(Case("chart", "chart", "Which day has the tallest bar?", ["tue"], chart))

    def nvsmi() -> Image.Image:
        return _terminal("nvidia-smi", [
            "+-----------------------------------------------------------------------------+",
            "| GPU  Name                 | Memory-Usage         | GPU-Util  Temp  Power    |",
            "|===========================+======================+==========================|",
            "|   0  NVIDIA GeForce RTX 5090 | 22772MiB / 32607MiB |    41%    38C   142W    |",
            "+-----------------------------------------------------------------------------+"], size=17)
    add(Case("nvsmi", "terminal", "How much GPU memory is used, in MiB?", ["22772"], nvsmi))

    def chat() -> Image.Image:
        img, d = canvas()
        for i, (who, at, msg) in enumerate((("Priya", "14:05", "build is green again"),
                                           ("Marcus", "14:32", "can you rerun the soak tonight?"),
                                           ("Priya", "14:40", "on it"))):
            y = 140 + i * 130
            d.text((200, y), f"{who}  {at}", font=font("ui_bold", 20), fill=(90, 90, 90))
            d.rounded_rectangle([200, y + 34, 900, y + 94], radius=14, fill=(230, 236, 245))
            d.text((220, y + 50), msg, font=font("ui", 22), fill=FG_DARK)
        return img
    add(Case("chat", "messaging", "Who sent the message at 14:32, and what did they ask?", ["marcus", "soak"], chat))
    return out


# ---------------------------------------------------------------------------
# Engine telemetry
# ---------------------------------------------------------------------------

def _get(url: str, timeout: float = 5.0) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode() or "{}")
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def gpu() -> Dict[str, float]:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.total,temperature.gpu,power.draw",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10).stdout
        used, total, temp, power = (float(x) for x in out.strip().split(",")[:4])
        return {"used_mib": used, "total_mib": total, "temp_c": temp, "power_w": power}
    except Exception:  # noqa: BLE001
        return {}


def resident(engine_root: str) -> List[str]:
    return sorted(m.get("name", "") for m in (_get(engine_root + "/api/ps").get("models") or []))


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower().replace("’", "'")).strip()


def _has(a: str, token: str) -> bool:
    if token.startswith("re:"):
        return re.search(token[3:], a) is not None
    return _norm(token) in a


def score(answer: str, case: Case) -> bool:
    a = _norm(answer)
    return all(_has(a, t) for t in case.expect) and not any(_has(a, f) for f in case.forbid)


async def _generation_probe(engine_root: str, model: str) -> Dict[str, object]:
    from backend.core.ouroboros.governance.local_inference_director import LocalConfig, LocalPrimeClient
    t0 = time.monotonic()
    client = LocalPrimeClient(LocalConfig.from_env())
    try:
        out = await client.complete(system="Answer in one word.", user="Name the capital of France.",
                                    prompt_tokens=20)
        text = out[0] if isinstance(out, tuple) else str(out)
        return {"ok": bool(str(text).strip()), "s": round(time.monotonic() - t0, 2)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200], "s": round(time.monotonic() - t0, 2)}
    finally:
        await client.aclose()               # the probe owns its session; never leak it


async def run(args: argparse.Namespace) -> int:
    from backend.vision.local_vision_client import LocalVisionClient
    client = LocalVisionClient()
    engine_root = client.base_url[:-3] if client.base_url.endswith("/v1") else client.base_url
    primary = (os.environ.get("JARVIS_LOCAL_MODEL_NAME") or "").strip()
    report: Dict[str, object] = {"endpoint": client.base_url, "model": client.model, "primary": primary,
                                 "engine": _get(engine_root + "/health").get("service", "?"),
                                 "before": {"resident": resident(engine_root), "gpu": gpu()}, "cases": []}
    if not client.enabled:
        print("vision client disabled (JARVIS_VISION_MODEL_NAME unset?)")
        return 2
    peak_used, peak_temp, invariant_breaks = 0.0, 0.0, []
    for case in cases():
        img = case.render()
        ans = await client.describe(img, case.question + " Be concise.", max_tokens=args.max_tokens)
        res = resident(engine_root)
        g = gpu()
        peak_used, peak_temp = max(peak_used, g.get("used_mib", 0)), max(peak_temp, g.get("temp_c", 0))
        row = {"case": case.name, "category": case.category, "ok": ans.ok, "pass": ans.ok and score(ans.text, case),
               "answer": ans.text[:300], "error": ans.error, "latency_ms": round(ans.latency_ms),
               "resident": res, "gpu_used_mib": g.get("used_mib"), "temp_c": g.get("temp_c")}
        if primary and primary not in res:
            invariant_breaks.append(f"{case.name}: primary {primary} not resident after the vision request")
        if args.generation_probe:
            row["generation"] = await _generation_probe(engine_root, primary)
            res2 = resident(engine_root)
            if client.model not in res2 or (primary and primary not in res2):
                invariant_breaks.append(f"{case.name}: co-residency broken after generation: {res2}")
        report["cases"].append(row)
        print(f"{'PASS' if row['pass'] else 'FAIL'} {case.name:<15} {row['latency_ms']:>6} ms  "
              f"{(ans.text or ans.error or '')[:90]!r}", flush=True)
    rows = report["cases"]
    passed = sum(1 for r in rows if r["pass"])
    lat = sorted(r["latency_ms"] for r in rows if r["ok"])
    report["summary"] = {
        "passed": passed, "total": len(rows), "accuracy": round(passed / max(1, len(rows)), 3),
        "by_category": {c: f"{sum(1 for r in rows if r['category'] == c and r['pass'])}/"
                           f"{sum(1 for r in rows if r['category'] == c)}"
                        for c in sorted({r['category'] for r in rows})},
        "latency_ms_p50": lat[len(lat) // 2] if lat else None, "latency_ms_max": lat[-1] if lat else None,
        "peak_gpu_used_mib": peak_used, "peak_temp_c": peak_temp,
        "after": {"resident": resident(engine_root), "gpu": gpu()},
        "invariant_breaks": invariant_breaks,
    }
    print(json.dumps(report["summary"], indent=2))
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0 if not invariant_breaks else 1


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="")
    ap.add_argument("--max-tokens", type=int, default=200)
    ap.add_argument("--generation-probe", action="store_true")
    ap.add_argument("--render-only", default="", help="write the case images to this directory and exit")
    args = ap.parse_args(argv)
    if args.render_only:
        d = Path(args.render_only)
        d.mkdir(parents=True, exist_ok=True)
        for c in cases():
            c.render().save(d / f"{c.name}.png")
        print(f"rendered {len(cases())} cases to {d}")
        return 0
    from backend.core.env_bootstrap import load_env_once
    load_env_once()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
