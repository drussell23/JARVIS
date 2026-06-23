#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Autonomous Epistemic Memory Matrix -- absorb the lessons of a successful PR.

When a self-improvement PR merges, this pipeline distils its ONE core structural
lesson and injects it as a PERMANENT constraint into the repo's ``.cursorrules``
-- so the AI coding environment cannot hallucinate the same architectural error
again. The engine literally rewrites its own baseline constraints from its wins.

Flow (all async, all local, all fail-soft):
  1. structural diff parse -> changed files + touched def/class symbols + hunks.
  2. lightweight LOCAL model call (Ollama, default ``qwen2.5-coder:3b``) abstracts
     the single most important structural lesson as one imperative rule. If the
     model is unreachable it falls back to a deterministic synthesis -- NEVER fails.
  3. append-only, content-hash-deduplicated injection into ``.cursorrules`` (a
     managed AUTO block), stamped with PR number + date + the lesson's hash.

ZERO-INTERFERENCE by construction: a pure leaf -- imports NOTHING from the JARVIS
core, touches no FSM state, makes no GCP call, holds no shared resource. It reads
a PR diff (via ``gh`` or an Extractor artifact) and writes one local text file.

Usage:
    python3 scripts/epistemic_memory_ingest.py --pr 69670
    python3 scripts/epistemic_memory_ingest.py --from-artifact sovereign_pr_69670_*.md
    python3 scripts/epistemic_memory_ingest.py --diff-file some.diff --pr 69670 --no-model
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from typing import Dict, List, Optional, Tuple

_BEGIN = "<!-- SOVEREIGN-EPISTEMIC-CONSTRAINTS:BEGIN -->"
_END = "<!-- SOVEREIGN-EPISTEMIC-CONSTRAINTS:END -->"


def _env(name: str, default: str) -> str:
    v = os.environ.get(name, "")
    return v.strip() if v and v.strip() else default


# --------------------------------------------------------------------------- #
# 1. Structural diff parse (no ast.parse on partial patches -> zero parse-risk).
# --------------------------------------------------------------------------- #
def parse_diff_structure(diff: str) -> Dict[str, object]:
    """Extract changed files + touched def/class symbols + a compact +/- summary
    from a unified diff. Fail-soft -> partial. Pure."""
    files: List[str] = []
    symbols: List[str] = []
    added = removed = 0
    try:
        for line in (diff or "").splitlines():
            if line.startswith("+++ b/"):
                files.append(line[6:].strip())
            elif line.startswith("@@"):
                # hunk context often names the enclosing def/class
                m = re.search(r"@@.*@@\s*(?:async\s+)?(def|class)\s+(\w+)", line)
                if m:
                    symbols.append(f"{m.group(1)} {m.group(2)}")
            elif line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
                if line.startswith("+"):
                    added += 1
                else:
                    removed += 1
                m = re.match(r"[+-]\s*(?:async\s+)?(def|class)\s+(\w+)", line)
                if m:
                    symbols.append(f"{m.group(1)} {m.group(2)}")
    except Exception:  # noqa: BLE001 -- memory ingest never crashes
        pass
    # de-dup, preserve order
    _seen: set = set()
    symbols = [s for s in symbols if not (s in _seen or _seen.add(s))]
    return {"files": files, "symbols": symbols, "added": added, "removed": removed}


# --------------------------------------------------------------------------- #
# 2. Lesson abstraction -- local Ollama model, deterministic fallback.
# --------------------------------------------------------------------------- #
def _ollama_endpoint() -> str:
    return _env("JARVIS_EPISTEMIC_MODEL_ENDPOINT", "http://localhost:11434/api/generate")


def _ollama_model() -> str:
    return _env("JARVIS_EPISTEMIC_MODEL", "qwen2.5-coder:3b")


def _build_prompt(struct: Dict, title: str, body: str) -> str:
    syms = ", ".join(struct.get("symbols", [])[:12]) or "(none parsed)"
    files = ", ".join(struct.get("files", [])[:8]) or "(none)"
    return (
        "You distil the ONE core structural/architectural lesson from a merged "
        "self-improvement PR, to be stored as a PERMANENT coding rule that prevents "
        "the same class of mistake.\n\n"
        f"PR title: {title}\n"
        f"Changed files: {files}\n"
        f"Touched symbols: {syms}\n"
        f"PR rationale (truncated): {(body or '')[:800]}\n\n"
        "Output EXACTLY one imperative rule, max 28 words, in the form "
        "'Always/Never X, because Y.' Output ONLY the rule -- no preamble, no markdown."
    )


def _call_ollama_sync(prompt: str, *, timeout_s: float) -> Optional[str]:
    """Blocking Ollama call (run via asyncio.to_thread). Returns the rule or None."""
    try:
        payload = json.dumps({
            "model": _ollama_model(), "prompt": prompt, "stream": False,
            "options": {"temperature": 0.1, "num_predict": 80},
        }).encode("utf-8")
        req = urllib.request.Request(
            _ollama_endpoint(), data=payload, headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        rule = str(data.get("response", "")).strip()
        # keep the first non-empty line, strip surrounding quotes/markdown bullets
        for ln in rule.splitlines():
            ln = ln.strip().lstrip("-*").strip().strip('"').strip()
            if ln:
                return ln[:280]
        return None
    except Exception:  # noqa: BLE001 -- model optional; fall back deterministically
        return None


def _deterministic_lesson(struct: Dict, title: str) -> str:
    """Never-fail fallback: synthesise a rule from the title + touched symbols."""
    syms = ", ".join(struct.get("symbols", [])[:4])
    base = title.strip() or "a merged self-improvement change"
    if syms:
        return f"Preserve the structural intent of `{syms}` established by: {base}."
    return f"Preserve the structural intent established by: {base}."


async def abstract_lesson(struct: Dict, *, title: str, body: str,
                          use_model: bool = True, timeout_s: float = 30.0) -> Tuple[str, str]:
    """Return (lesson, source) where source in {'model','deterministic'}. Async, fail-soft."""
    if use_model:
        prompt = _build_prompt(struct, title, body)
        try:
            rule = await asyncio.to_thread(_call_ollama_sync, prompt, timeout_s=timeout_s)
        except Exception:  # noqa: BLE001
            rule = None
        if rule:
            return rule, "model"
    return _deterministic_lesson(struct, title), "deterministic"


# --------------------------------------------------------------------------- #
# 3. Sovereign constraint injection into .cursorrules (idempotent, append-only).
# --------------------------------------------------------------------------- #
def _rule_line(lesson: str, pr_number: int, sha: str) -> str:
    h = hashlib.sha256(lesson.strip().lower().encode("utf-8")).hexdigest()[:8]
    stamp = time.strftime("%Y-%m-%d")
    pr = f"#{pr_number}" if pr_number else "#?"
    return f"- [{pr}] {lesson.strip()}  _(<!--h:{h}-->{stamp}, sha:{sha[:7]})_"


def inject_constraint(lesson: str, *, pr_number: int, sha: str,
                      cursorrules_path: str = ".cursorrules") -> Tuple[bool, str]:
    """Append the lesson into the managed AUTO block of .cursorrules. Idempotent
    (content-hash dedup). Returns (injected, reason). Fail-soft."""
    try:
        line = _rule_line(lesson, pr_number, sha)
        h_tag = re.search(r"<!--h:(\w+)-->", line)
        h = h_tag.group(1) if h_tag else ""
        existing = ""
        if os.path.exists(cursorrules_path):
            with open(cursorrules_path, encoding="utf-8") as fh:
                existing = fh.read()
        if h and f"<!--h:{h}-->" in existing:
            return False, "duplicate (lesson already present)"
        if _BEGIN in existing and _END in existing:
            new = existing.replace(_END, f"{line}\n{_END}", 1)
        else:
            header = (
                "# .cursorrules -- Sovereign Epistemic Constraints\n\n"
                "> Auto-maintained by `epistemic_memory_ingest.py`. Each rule is a\n"
                "> PERMANENT architectural constraint distilled from a MERGED\n"
                "> self-improvement PR. Append-only; deduplicated by content hash.\n"
                "> Do not hand-edit inside the AUTO block.\n\n"
                f"{_BEGIN}\n{line}\n{_END}\n"
            )
            new = (existing + "\n" + header) if existing.strip() else header
        tmp = cursorrules_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(new)
        os.replace(tmp, cursorrules_path)
        return True, "injected"
    except Exception as exc:  # noqa: BLE001
        return False, f"injection failed: {exc!r}"


# --------------------------------------------------------------------------- #
# PR / diff acquisition (gh or an Extractor artifact) -- reuse-light.
# --------------------------------------------------------------------------- #
def _gh_pr(number: int, repo: Optional[str]) -> Tuple[str, Dict]:
    base = ["--repo", repo] if repo else []
    try:
        v = subprocess.run(["gh", "pr", "view", str(number), *base, "--json",
                            "title,body,mergeCommit,headRefName"],
                           capture_output=True, text=True, timeout=60)
        meta = json.loads(v.stdout or "{}")
    except Exception:  # noqa: BLE001
        meta = {}
    try:
        d = subprocess.run(["gh", "pr", "diff", str(number), *base],
                           capture_output=True, text=True, timeout=120)
        diff = d.stdout or ""
    except Exception:  # noqa: BLE001
        diff = ""
    return diff, meta


def _diff_from_artifact(path: str) -> Tuple[str, Dict]:
    """Pull the ```diff block + title out of an Extractor markdown artifact."""
    try:
        with open(path, encoding="utf-8") as fh:
            md = fh.read()
    except Exception:  # noqa: BLE001
        return "", {}
    title = ""
    m = re.search(r"^#\s*Sovereign Artifact\s*-\s*(.+)$", md, re.MULTILINE)
    if m:
        title = m.group(1).strip()
    dm = re.search(r"```diff\n(.*?)```", md, re.DOTALL)
    diff = dm.group(1) if dm else ""
    return diff, {"title": title, "body": ""}


async def ingest(*, pr_number: int = 0, repo: Optional[str] = None,
                 artifact: str = "", diff_file: str = "", use_model: bool = True,
                 cursorrules_path: str = ".cursorrules") -> int:
    if artifact:
        diff, meta = _diff_from_artifact(artifact)
    elif diff_file:
        try:
            with open(diff_file, encoding="utf-8") as fh:
                diff = fh.read()
        except Exception:  # noqa: BLE001
            diff = ""
        meta = {}
    elif pr_number:
        diff, meta = _gh_pr(pr_number, repo)
    else:
        print("[epistemic] need --pr, --from-artifact, or --diff-file."); return 2
    if not diff.strip():
        print("[epistemic] no diff content found -- nothing to absorb."); return 2

    struct = parse_diff_structure(diff)
    sha = str((meta.get("mergeCommit") or {}).get("oid", "") if isinstance(meta.get("mergeCommit"), dict) else "")
    title = str(meta.get("title", "") or "")
    body = str(meta.get("body", "") or "")
    lesson, source = await abstract_lesson(struct, title=title, body=body, use_model=use_model)
    print(f"[epistemic] lesson ({source}): {lesson}")
    injected, reason = inject_constraint(lesson, pr_number=pr_number, sha=sha,
                                         cursorrules_path=cursorrules_path)
    print(f"[epistemic] .cursorrules -> {reason} "
          f"(files={len(struct['files'])} symbols={len(struct['symbols'])} "
          f"+{struct['added']}/-{struct['removed']})")
    return 0 if injected or "duplicate" in reason else 1


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Autonomous Epistemic Memory Matrix -> .cursorrules.")
    p.add_argument("--pr", type=int, default=0)
    p.add_argument("--repo", default="")
    p.add_argument("--from-artifact", default="", help="an extract_sovereign_pr.py markdown artifact (glob ok).")
    p.add_argument("--diff-file", default="")
    p.add_argument("--no-model", action="store_true", help="skip the local model; deterministic only.")
    p.add_argument("--cursorrules", default=".cursorrules")
    args = p.parse_args(argv)
    artifact = args.from_artifact
    if artifact and ("*" in artifact or "?" in artifact):
        hits = sorted(glob.glob(artifact))
        artifact = hits[-1] if hits else artifact
    return asyncio.run(ingest(
        pr_number=args.pr, repo=args.repo or None, artifact=artifact,
        diff_file=args.diff_file, use_model=not args.no_model,
        cursorrules_path=args.cursorrules,
    ))


if __name__ == "__main__":
    sys.exit(main())
