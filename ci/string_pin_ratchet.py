#!/usr/bin/env python3
"""Hold the source-string pin count down, the way lint_gate holds
undefined names at zero.

A pin of the shape ``assert "<literal>" in src`` tests spelling. It fails on
safe refactors and passes on a comment containing the magic substring, and
three of them were found holding defects in place during the micro-fix
audit -- one pinned the disk read that WAS the bug, so it had to be deleted
before the bug could be.

There are too many to convert in one pass and converting them blindly would
be worse than leaving them. So this is a ratchet, not a gate: the count may
fall and may not rise. New tests must use ``tests/support/ast_contract``, and
the backlog drains whenever someone touches a file that has them.

Usage:
    python3 ci/string_pin_ratchet.py            # check against baseline
    python3 ci/string_pin_ratchet.py --update   # lower the baseline
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TESTS = REPO / "tests"
BASELINE = REPO / "ci" / "string_pin_baseline.json"

# ``assert "<8+ chars>" in src`` and its spellings. Deliberately narrow: it
# matches the anti-pattern (a source blob read from disk), not every string
# containment assertion, because asserting a substring of a RENDERED output
# is a legitimate behavioural test.
PIN_RE = re.compile(
    r"""assert\s+(?:f?["'])([^"']{8,})["']\s+(?:not\s+)?in\s+"""
    r"""(src|source|SRC|SOURCE)\b""",
)


def count() -> dict:
    per_file: dict = {}
    for path in sorted(TESTS.rglob("test_*.py")):
        try:
            hits = len(PIN_RE.findall(path.read_text(errors="replace")))
        except OSError:
            continue
        if hits:
            per_file[str(path.relative_to(REPO))] = hits
    return per_file


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--update", action="store_true")
    args = ap.parse_args()

    per_file = count()
    total = sum(per_file.values())

    if args.update or not BASELINE.exists():
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(json.dumps(
            {"total": total, "per_file": per_file}, indent=2, sort_keys=True,
        ) + "\n")
        print(f"baseline written: {total} pin(s) in {len(per_file)} file(s)")
        return 0

    prior = json.loads(BASELINE.read_text())
    prior_total = int(prior.get("total", 0))
    prior_files = prior.get("per_file", {})

    if total > prior_total:
        grew = sorted(
            (f, n, prior_files.get(f, 0))
            for f, n in per_file.items() if n > prior_files.get(f, 0)
        )
        print(f"✗ source-string pins rose {prior_total} -> {total}")
        for name, now, was in grew[:20]:
            print(f"    {name}: {was} -> {now}")
        print("\n  New tests must use tests/support/ast_contract instead.")
        print("  A pin on source text freezes spelling, not behaviour.")
        return 1

    if total < prior_total:
        print(f"✓ pins fell {prior_total} -> {total}. Run --update to lock it in.")
        return 0

    print(f"✓ source-string pins held at {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
