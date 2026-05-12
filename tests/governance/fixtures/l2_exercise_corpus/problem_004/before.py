"""Configuration merger."""
from __future__ import annotations


def merge(base, override):
    """Return a new dict combining ``base`` and ``override``.

    Keys present in both inputs use the value from ``override``.
    Keys present in only one input are preserved verbatim.  The
    inputs are not modified.

    See ``test_before.py`` for the input/output behavior contract.
    """
    # BUG: shallow merge.  At any depth >= 2, the override's
    # nested dict completely REPLACES the base's nested dict
    # at that key instead of merging into it.
    result = dict(base)
    result.update(override)
    return result
