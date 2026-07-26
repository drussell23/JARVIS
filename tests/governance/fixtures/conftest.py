"""This directory is DATA. Nothing under it is a test.

The collection error
--------------------
``pytest tests/governance`` aborted before running anything::

    import file mismatch:
    imported module 'test_before' has this __file__ attribute:
      .../l2_exercise_corpus/problem_001/test_before.py
    which is not the same as the test file we want to collect:
      .../l2_exercise_corpus/problem_002/test_before.py

Four ``problem_NNN/test_before.py`` files share a basename with no package
structure between them, so pytest resolved all four to the module name
``test_before`` and refused the second.

Why ignoring is the fix, not renaming
-------------------------------------
The obvious repair is to rename them ``test_before_001.py`` and so on. That
would silence the error and introduce a worse one: these files are the
DELIBERATELY FAILING tests of the L2 repair corpus — the defect the engine is
graded on fixing. Collecting them at all injects four known-red tests into
every suite run, and "the corpus is red" would become indistinguishable from
"the organism regressed".

They are inputs, read by path from ``test_fixture_l2_exercise_problem_NNN.py``
alongside ``before.py``, ``manifest.json`` and ``_known_good_fix.py`` — none of
which pytest tries to collect, because none of them happens to start with
``test_``. The basename collision was never the disease; it was the symptom of
test-shaped DATA sitting inside the collection tree.

So the directory declares what it is. ``collect_ignore_glob`` matches every
entry here, which prunes the corpus directories themselves and therefore
everything beneath them.
"""
from __future__ import annotations

collect_ignore_glob = ["*"]
