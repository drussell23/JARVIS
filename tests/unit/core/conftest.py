# tests/unit/core/conftest.py
"""Shared fixtures for core unit tests.

Provides a short ``tmp_path`` override so that Unix Domain Socket paths
stay within the macOS AF_UNIX 104-byte sun_path limit.
"""

import os
import sys
import tempfile

import pytest


@pytest.fixture
def tmp_path(request, tmp_path_factory):
    """Override built-in ``tmp_path`` with a short base directory.

    macOS limits AF_UNIX sun_path to 104 bytes.  The default pytest
    ``tmp_path`` (``/private/var/folders/.../pytest-of-.../...``) easily
    exceeds that.  This fixture creates the temp directory under ``/tmp``
    which keeps paths well within the limit.
    """
    short_base = tempfile.mkdtemp(prefix="jt_", dir="/tmp")
    path = type(tmp_path_factory)  # just get Path type
    from pathlib import Path

    p = Path(short_base)
    # Register cleanup
    request.addfinalizer(lambda: _rmtree_safe(p))
    return p


def _rmtree_safe(p):
    """Remove a directory tree, ignoring errors."""
    import shutil

    try:
        shutil.rmtree(str(p), ignore_errors=True)
    except Exception:
        pass
