"""Supervisor Capability-Liveness Sweep (Campaign Step 1b, 2026-07-19).

Static AST reference graph over unified_supervisor.py CROSS-REFERENCED
with (a) event-bus topology markers (subscribe/publish/handler
registrations) and (b) dynamic-dispatch markers (getattr strings,
string literals naming the symbol, importlib/__import__) — mandate 1:
pure AST alone would false-positive every reflective consumer.

Emits a CANDIDATE report (never deletes): symbols with zero static
references AND zero bus wiring AND zero string-literal mentions are
Tier-A quarantine candidates; string-mentioned-only symbols are
Tier-B (verify by hand / rely on the breach loader).
"""
from __future__ import annotations
import ast, json, sys
from collections import defaultdict
from pathlib import Path

SRC = Path("unified_supervisor.py")
tree = ast.parse(SRC.read_text())

defs = {}                       # name -> (lineno, end, kind)
refs = defaultdict(int)         # name -> reference count
strings = set()                 # every string literal token
bus_wired = set()               # names appearing in subscribe/register calls

class Collector(ast.NodeVisitor):
    def __init__(self):
        self.stack = []
    def visit_ClassDef(self, node):
        if not self.stack:
            defs[node.name] = (node.lineno, node.end_lineno, "class")
        self.stack.append(node.name); self.generic_visit(node); self.stack.pop()
    def visit_FunctionDef(self, node):
        if not self.stack:
            defs[node.name] = (node.lineno, node.end_lineno, "func")
        self.stack.append(node.name); self.generic_visit(node); self.stack.pop()
    visit_AsyncFunctionDef = visit_FunctionDef
    def visit_Name(self, node):
        refs[node.id] += 1; self.generic_visit(node)
    def visit_Attribute(self, node):
        refs[node.attr] += 1; self.generic_visit(node)
    def visit_Constant(self, node):
        if isinstance(node.value, str):
            for tok in node.value.replace(".", " ").replace("/", " ").split():
                strings.add(tok)
        self.generic_visit(node)
    def visit_Call(self, node):
        fn = node.func
        name = getattr(fn, "attr", getattr(fn, "id", ""))
        if any(m in str(name).lower() for m in
               ("subscribe", "register", "add_handler", "on_event", "connect")):
            for a in ast.walk(node):
                if isinstance(a, ast.Name):
                    bus_wired.add(a.id)
                elif isinstance(a, ast.Attribute):
                    bus_wired.add(a.attr)
                elif isinstance(a, ast.Constant) and isinstance(a.value, str):
                    bus_wired.add(a.value)
        self.generic_visit(node)

Collector().visit(tree)

tier_a, tier_b = [], []
for name, (lo, hi, kind) in sorted(defs.items(), key=lambda kv: kv[1][0]):
    if name.startswith("__"):
        continue
    # A def's own occurrence counts once as a Name/Attribute? defs via
    # ClassDef/FunctionDef don't visit_Name — refs are TRUE uses, but a
    # decorator or self-recursive call inflates by a few; threshold 0.
    static_refs = refs.get(name, 0)
    in_bus = name in bus_wired
    in_strings = name in strings
    span = (hi or lo) - lo + 1
    if static_refs == 0 and not in_bus and not in_strings:
        tier_a.append({"name": name, "kind": kind, "line": lo, "span": span})
    elif static_refs == 0 and not in_bus and in_strings:
        tier_b.append({"name": name, "kind": kind, "line": lo, "span": span,
                        "note": "string-literal mention (reflection risk)"})

report = {
    "schema_version": "liveness_sweep.1",
    "file": str(SRC), "total_lines": SRC.read_text().count("\n") + 1,
    "top_level_defs": len(defs),
    "tier_a_candidates": tier_a,
    "tier_b_reflection_risk": tier_b,
    "tier_a_total_lines": sum(c["span"] for c in tier_a),
    "bus_wired_names": len(bus_wired),
}
out = Path(".jarvis/supervisor_liveness_report.json")
out.write_text(json.dumps(report, indent=1))
print(f"defs={len(defs)} tierA={len(tier_a)} ({report['tier_a_total_lines']} lines) tierB={len(tier_b)} bus_names={len(bus_wired)}")
for c in tier_a[:15]:
    print(f"  A {c['kind']:5s} {c['name']:42s} L{c['line']:6d} span={c['span']}")
