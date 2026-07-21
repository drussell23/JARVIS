"""Architecture pin: ZERO live gsutil invocations anywhere in the repo.

Google Cloud removes gsutil from the default gcloud CLI bundle in March
2027 (notice 2026-07-20, project jarvis-473803). Every GCS surface here
uses either the google-cloud-storage SDK (ADC) or ``gcloud storage`` —
this pin makes a regression to gsutil structurally impossible: a new
invocation fails CI naming the file:line.

Comments MAY mention gsutil (they document the very invariant this test
enforces); only executable occurrences fail.
"""
from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]

#: Scanned executable surfaces. Docs/memory/tests are exempt (prose).
_GLOBS = ("backend/**/*.py", "scripts/**/*.py", "scripts/**/*.sh",
          "deploy/**/*.sh", ".github/workflows/*.yml", "*.py")

#: Comment-leaders per suffix — a gsutil mention AFTER one of these on
#: the same line is documentation, not invocation.
_COMMENT = {".py": "#", ".sh": "#", ".yml": "#"}


def _live_occurrences() -> list:
    hits = []
    for pattern in _GLOBS:
        for path in _REPO.glob(pattern):
            if "node_modules" in path.parts or ".git" in path.parts:
                continue
            leader = _COMMENT.get(path.suffix)
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                if "gsutil" not in line:
                    continue
                code = line.split(leader, 1)[0] if leader else line
                # strings that merely NAME the tool in log/docstring text
                # ("never gsutil", "not a gsutil subprocess") are prose;
                # an invocation has gsutil as a word followed by an arg
                # or as a subprocess token.
                if "gsutil" not in code:
                    continue
                if re.search(
                    r"""(^|[\s(=&|;'"])gsutil(['"],|\s+-|\s+\w)""", code,
                ) and "never" not in code and "NOT" not in code:
                    hits.append(f"{path.relative_to(_REPO)}:{lineno}: "
                                f"{line.strip()[:100]}")
    return hits


def test_zero_live_gsutil_invocations():
    hits = _live_occurrences()
    assert not hits, (
        "gsutil leaves the default gcloud bundle March 2027 — migrate to "
        "`gcloud storage` (see this test's docstring):\n" + "\n".join(hits)
    )
