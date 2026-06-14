"""Permanent regression fence: no top-level class name may be defined twice in
unified_supervisor.py. Duplicate top-level definitions silently shadow each
other at import time (Python keeps the later one) and are a latent correctness
hazard. Introduced by the Sovereign Distillation (Phase A).
"""
import ast
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
TARGET = REPO_ROOT / "unified_supervisor.py"


def _top_level_class_names():
    tree = ast.parse(TARGET.read_text(encoding="utf-8"))
    return [n.name for n in tree.body if isinstance(n, ast.ClassDef)]


def test_target_file_exists():
    assert TARGET.is_file(), f"expected {TARGET} to exist"


def test_no_shadowed_top_level_class_definitions():
    counts = Counter(_top_level_class_names())
    dupes = {name: n for name, n in counts.items() if n > 1}
    assert not dupes, (
        "Top-level class names defined more than once (later def shadows "
        f"earlier): {dupes}"
    )
