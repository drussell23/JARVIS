"""Atomic Quarantine Migration (Campaign Slice 1, 2026-07-19).

Pipeline per candidate (mandate 1 — the external-importer pass is
AUTOMATED, never a manual grep):
  1. repo-wide static scan: any `from unified_supervisor import X`,
     `unified_supervisor.X`, or bare string mention of X outside the
     monolith → candidate is VETOED (stays put).
  2. AST-span extraction of survivors → backend/core/quarantine/
     supervisor_slice1.py (source preserved byte-identical).
  3. Atomic rewrite of unified_supervisor.py: spans replaced by a
     migration marker comment; PEP-562 symbol net wired via
     quarantine_loader.attach_symbol_net at module tail.
  4. Compile-verify BOTH files before any byte lands (write to temp,
     ast.parse, then atomic os.replace).

Usage: --plan (default, report only) | --apply <N> (migrate top-N
largest verified candidates).
"""
from __future__ import annotations
import ast, json, os, subprocess, sys
from pathlib import Path

REPO = Path(".").resolve()
SRC = REPO / "unified_supervisor.py"
QMOD = REPO / "backend/core/quarantine/supervisor_slice1.py"
REPORT = REPO / ".jarvis/supervisor_liveness_report.json"

apply_n = 0
if "--apply" in sys.argv:
    apply_n = int(sys.argv[sys.argv.index("--apply") + 1])

cands = json.loads(REPORT.read_text())["tier_a_candidates"]
cands.sort(key=lambda c: -c["span"])

def external_consumers(name: str) -> int:
    """Automated repo-wide pass (indexed git grep — 5M lines in ms),
    excluding the monolith itself, quarantine, and the sweep tools."""
    try:
        out = subprocess.run(
            ["git", "grep", "-l", "--", name,
             "--", "*.py",
             ":!unified_supervisor.py",
             ":!backend/core/quarantine/*",
             ":!scripts/scouts/*"],
            capture_output=True, text=True, timeout=60, cwd=REPO,
        ).stdout.splitlines()
        return len(out)
    except Exception as e:
        print(f"  scan error for {name}: {e} — VETO (fail closed)")
        return 999

verified, vetoed = [], []
for c in cands[:max(apply_n, 12)]:
    n = external_consumers(c["name"])
    (verified if n == 0 else vetoed).append({**c, "external_files": n})
    print(f"  {'OK  ' if n == 0 else 'VETO'} {c['name']:40s} span={c['span']:4d} external_files={n}")

if not apply_n:
    print(f"\nplan: {len(verified)} verified, {len(vetoed)} vetoed")
    sys.exit(0)

targets = verified[:apply_n]
if not targets:
    print("nothing verified to migrate"); sys.exit(1)

src_text = SRC.read_text()
lines = src_text.split("\n")
tree = ast.parse(src_text)
spans = {}
for node in tree.body:
    if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        spans[node.name] = (node.lineno, node.end_lineno)

extracted, removals = [], []
for t in targets:
    lo, hi = spans[t["name"]]
    extracted.append("\n".join(lines[lo - 1:hi]))
    removals.append((lo, hi, t["name"]))

qheader = (
    '"""Quarantined supervisor zones — Migration Slice 1 (2026-07-19).\n'
    "Source preserved byte-identical from unified_supervisor.py; any\n"
    "dynamic consumer is revived through the PEP-562 symbol net with a\n"
    "[QUARANTINE_BREACH] beacon. NEVER import this module directly.\n"
    '"""\n'
    "from __future__ import annotations  # noqa: F401\n\n"
)
qbody = qheader + "\n\n\n".join(extracted) + "\n"
ast.parse(qbody)                                    # verify BEFORE write

for lo, hi, name in sorted(removals, reverse=True):
    marker = (f"# [QUARANTINED slice1 2026-07-19] {name} "
              f"(was L{lo}-L{hi}) → backend/core/quarantine/"
              f"supervisor_slice1.py — revived on demand via symbol net")
    lines[lo - 1:hi] = [marker]

net_map = {t["name"]: f"backend.core.quarantine.supervisor_slice1:{t['name']}"
           for t in targets}
net_block = (
    "\n\n# ── Quarantine symbol net (Migration Slice 1, 2026-07-19) ──\n"
    "# Tier-A zones migrated to backend/core/quarantine/; any dynamic\n"
    "# consumer is revived with a [QUARANTINE_BREACH] beacon (PEP 562).\n"
    "try:\n"
    "    from backend.core.quarantine_loader import attach_symbol_net as _aqn\n"
    f"    _aqn(globals(), {json.dumps(net_map, indent=8)})\n"
    "except Exception:  # noqa: BLE001 — the net is optional, boot is not\n"
    "    pass\n"
)
new_src = "\n".join(lines) + net_block
ast.parse(new_src)                                  # verify BEFORE write

tmp_q = QMOD.with_suffix(".tmp"); tmp_q.write_text(qbody); os.replace(tmp_q, QMOD)
tmp_s = SRC.with_suffix(".tmp"); tmp_s.write_text(new_src); os.replace(tmp_s, SRC)
print(f"\nMIGRATED {len(targets)}: {[t['name'] for t in targets]}")
print(f"lines shed: {sum(t['span'] for t in targets)}")
