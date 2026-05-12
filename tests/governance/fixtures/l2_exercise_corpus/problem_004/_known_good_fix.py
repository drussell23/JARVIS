"""Known-good fix for problem_004 recursive deep-merge.

Reference solution used by the spine to sanity-check that the
test suite IS solvable.  Not loaded by the canonical
``list_corpus_problems`` walker (underscore-prefixed → skipped).

Correct algorithm:
  * When BOTH base and override have a ``dict`` at the same key,
    recurse.
  * Otherwise, override's value wins (covers lists, scalars,
    type mismatches).
  * Recursion produces fresh dicts at every level so neither
    input is mutated (caught by the immutability tests).
"""
from __future__ import annotations


def merge(base, override):
    result = dict(base)
    for k, v in override.items():
        if (
            k in result
            and isinstance(result[k], dict)
            and isinstance(v, dict)
        ):
            # Recursion is the load-bearing semantic — without it,
            # nested dict overrides REPLACE instead of MERGE.
            result[k] = merge(result[k], v)
        else:
            # Lists, scalars, and type-mismatch keys all take the
            # override value verbatim.
            result[k] = v
    return result
