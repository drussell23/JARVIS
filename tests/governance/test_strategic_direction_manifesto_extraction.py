"""StrategicDirection — the Manifesto reaches the prompt whole.

Pins:
  * The manifesto section ends at the next heading of the same or a
    higher level. A ``"### "`` sub-heading inside it does not end it
    (the old ``"## "`` substring probe matched inside ``"### "`` and
    injected only the opening paragraph — 464 chars on the 2026-10-13
    README).
  * Principles are found inside the manifesto section wherever it
    sits; there is no fixed character window (Principle 7 fell past
    the old 20,000-char window and was silently dropped — 6 of 7
    injected).
  * A numbered bold line outside the manifesto cannot take a
    principle's slot.
  * ``#`` lines inside fenced code blocks are not headings.
  * The repository's own README yields all seven principles and a
    manifesto excerpt that includes them.
"""
from __future__ import annotations

from pathlib import Path

from backend.core.ouroboros.governance.strategic_direction import (
    StrategicDirectionService,
)

_SVC = StrategicDirectionService

_TITLES = (
    "The unified organism (tri-partite microkernel)",
    "Progressive awakening (adaptive lifecycle)",
    "Asynchronous tendrils (disciplined concurrency)",
    "The synthetic soul (Trinity consciousness)",
    "Intelligence-driven routing (the cognitive forge)",
    "Threshold-triggered neuroplasticity (Ouroboros)",
    "Absolute observability (systemic transparency)",
)


def _principles_block(filler: str = "") -> str:
    parts = []
    for n, title in enumerate(_TITLES, start=1):
        parts.append(f"**{n}. {title}**  \nBody of principle {n}.\n\n{filler}")
    return "".join(parts)


def _readme(*, before: str = "", filler: str = "", after: str = "") -> str:
    return (
        "# JARVIS\n\nIntro.\n\n"
        f"{before}"
        "## Symbiotic AI-Native Manifesto: Trinity Ecosystem Unification\n\n"
        "This initiative is **not a software refactor**.\n\n"
        "### The seven principles\n\n"
        f"{_principles_block(filler)}"
        "## Local setup\n\n"
        f"{after}"
    )


def test_sub_heading_does_not_end_the_manifesto():
    section = _SVC._extract_manifesto(_readme())
    assert section.startswith("## Symbiotic AI-Native Manifesto")
    assert "### The seven principles" in section
    assert "**7. Absolute observability" in section
    assert "## Local setup" not in section


def test_principles_have_no_fixed_window():
    # Push Principle 7 far past the old 20,000-char window.
    readme = _readme(filler="x" * 5_000 + "\n\n")
    assert readme.index("**7. Absolute") > 30_000
    principles = _SVC._extract_principles(readme)
    assert [p.split(". ", 1)[1] for p in principles] == list(_TITLES)


def test_numbered_bold_outside_the_manifesto_cannot_steal_a_slot():
    before = "## Benchmarks\n\n**1. Decode throughput**\n\n**2. Prefill**\n\n"
    principles = _SVC._extract_principles(_readme(before=before))
    assert principles[0] == f"1. {_TITLES[0]}"
    assert principles[1] == f"2. {_TITLES[1]}"
    assert len(principles) == 7


def test_comment_lines_in_code_fences_are_not_headings():
    readme = _readme().replace(
        "### The seven principles\n\n",
        "### The seven principles\n\n```bash\n# Clone the repo\n## not a heading\n```\n\n",
    )
    section = _SVC._extract_manifesto(readme)
    assert "**7. Absolute observability" in section


def test_stops_before_the_sections_that_follow_the_principles():
    readme = _readme().replace(
        "## Local setup", "### The zero-shortcut mandate\n\nMandate text.\n\n## Local setup",
    )
    section = _SVC._extract_manifesto(readme)
    assert "**7. Absolute observability" in section
    assert "zero-shortcut" not in section


def test_no_manifesto_falls_back_to_the_window():
    readme = "# Title\n\n**1. Only principle**\n"
    assert _SVC._extract_manifesto(readme) == ""
    assert _SVC._extract_principles(readme) == ["1. Only principle"]


def test_repository_readme_yields_all_seven_principles():
    root = Path(__file__).resolve().parents[2]
    readme = (root / "README.md").read_text(encoding="utf-8", errors="replace")
    principles = _SVC._extract_principles(readme)
    assert [int(p.split(".", 1)[0]) for p in principles] == [1, 2, 3, 4, 5, 6, 7]
    section = _SVC._extract_manifesto(readme)
    assert "**1. The unified organism" in section
