#!/usr/bin/env python3
"""Verify every NAMED SYMBOL the README claims actually exists in code.

Prose is not unverifiable — it names things. A class that does not exist
is as checkable as a line count, and far more misleading when wrong.
"""
from __future__ import annotations
import ast, re, subprocess
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")

# ---- build the repo symbol table (classes + defs), once -------------------
print("indexing repo symbols...", flush=True)
files = subprocess.run(
    ["git", "ls-files", "*.py"], cwd=ROOT, capture_output=True, text=True,
).stdout.split()

classes: dict[str, list[str]] = defaultdict(list)
funcs: dict[str, list[str]] = defaultdict(list)
for rel in files:
    if rel.startswith((".worktrees/", "tests/")):
        continue
    p = ROOT / rel
    try:
        tree = ast.parse(p.read_text(encoding="utf-8", errors="ignore"))
    except (SyntaxError, OSError):
        continue
    for n in ast.walk(tree):
        if isinstance(n, ast.ClassDef):
            classes[n.name].append(rel)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs[n.name].append(rel)
print(f"  {len(classes):,} classes, {len(funcs):,} functions "
      f"across {len(files):,} tracked .py files\n")

# ---- extract claimed symbols from the README -----------------------------
backticked = set(re.findall(r"`([A-Za-z_][A-Za-z0-9_.]*)`", README))
# Bold **Name** is also used for subsystem names in this README.
bolded = set(re.findall(r"\*\*([A-Z][A-Za-z0-9]+(?:[A-Z][A-Za-z0-9]*)+)\*\*",
                        README))

camel = sorted({s for s in backticked | bolded
                if re.fullmatch(r"[A-Z][A-Za-z0-9]*", s) and len(s) > 3
                and re.search(r"[a-z]", s) and re.search(r"[A-Z].*[A-Z]", s)})
snake = sorted({s for s in backticked
                if re.fullmatch(r"_?[a-z][a-z0-9_]{4,}", s) and "_" in s})

print("=" * 78)
print("A. CLASS-SHAPED NAMES CLAIMED IN THE README")
print("=" * 78)
missing_c = []
for name in camel:
    if name in classes:
        continue
    # Might be a function, a module, or an enum member.
    if name in funcs:
        continue
    missing_c.append(name)
print(f"  {len(camel)} claimed, {len(camel) - len(missing_c)} resolve to a "
      f"class/def, {len(missing_c)} DO NOT:")
for n in missing_c:
    print(f"    ??  {n}")

print()
print("=" * 78)
print("B. snake_case NAMES CLAIMED (methods / functions / flags)")
print("=" * 78)
missing_f = [n for n in snake if n not in funcs]
print(f"  {len(snake)} claimed, {len(snake) - len(missing_f)} resolve, "
      f"{len(missing_f)} DO NOT:")
for n in missing_f:
    print(f"    ??  {n}")

print()
print("=" * 78)
print("C. ENUMERATED COUNTS — check each by hand against the number found")
print("=" * 78)
for m in re.finditer(r"\*\*(\d+)[- ]([a-zA-Z][a-zA-Z ]{2,28}?)\*\*", README):
    print(f"  line {README[:m.start()].count(chr(10))+1:4d}: {m.group(0)}")
for m in re.finditer(r"\b(\d+)\s+(tiers?|phases?|sensors?|contexts?|agents?|"
                     r"zones?|principles?|layers?|patterns?)\b", README):
    line = README[:m.start()].count("\n") + 1
    print(f"  line {line:4d}: {m.group(0)}")


# ---- D. env-var defaults: README table vs code -------------------------------
print()
print("=" * 78)
print("D. ENV-VAR DEFAULTS — README table rows vs the literal in code")
print("=" * 78)
rows = re.findall(r"^\| `(JARVIS_[A-Z0-9_]+)` \| `([^`]*)` \|", README, re.M)
pat = re.compile(r'"(JARVIS_[A-Z0-9_]+)"\s*,\s*"([^"]*)"')
code_defaults: dict[str, set[str]] = defaultdict(set)
for rel in files:
    if rel.startswith(".worktrees/"):
        continue
    try:
        txt = (ROOT / rel).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        continue
    for m in pat.finditer(txt):
        code_defaults[m.group(1)].add(m.group(2).lower())

ok = mism = unk = 0
for flag, claimed in rows:
    have = code_defaults.get(flag)
    if not have:
        unk += 1
        print(f"  ?        {flag:44s} claims {claimed!r} (no literal default)")
    elif claimed.lower() in have:
        ok += 1
    else:
        mism += 1
        print(f"  MISMATCH {flag:44s} README {claimed!r} vs code {sorted(have)}")
print(f"\n  OK={ok}  MISMATCH={mism}  UNVERIFIABLE={unk}")
