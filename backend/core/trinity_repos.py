"""
trinity_repos - where the three Trinity repositories are, one answer.

Four components located the repos four ways: ``JARVIS_PRIME_PATH`` (the
.env), ``JARVIS_PRIME_REPO_PATH`` (early pre-warm), ``J_PRIME_REPO``
(cross-repo sync), each falling back to a fixed macOS folder such as
``~/Documents/repos/JARVIS-Prime``. On any other layout they disagreed, and
cross-repo sync could not find even the repository it was running from.

A repository is recognised by what it contains, not by its folder's name:

* jarvis   -> ``unified_supervisor.py``
* prime    -> the ``jarvis_prime`` package
* reactor  -> the ``reactor_core`` package

Resolution: an explicit environment variable (every historical name, the
first that points at a real repository wins); else this repository itself
for ``jarvis``; else a sibling directory of it holding the marker.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Dict, Optional, Tuple

#: name -> (marker inside the repo, environment variables that may name it)
_REPOS: Dict[str, Tuple[str, Tuple[str, ...]]] = {
    "jarvis": ("unified_supervisor.py", ("JARVIS_REPO_PATH", "JARVIS_REPO", "JARVIS_PATH")),
    "prime": ("jarvis_prime", ("JARVIS_PRIME_PATH", "JARVIS_PRIME_REPO_PATH", "J_PRIME_REPO")),
    "reactor": ("reactor_core", ("REACTOR_CORE_PATH", "REACTOR_CORE_REPO_PATH", "REACTOR_CORE_REPO")),
}


def _is_repo(path: Path, marker: str) -> bool:
    try:
        return (path / marker).exists()
    except OSError:
        return False


def _this_repo() -> Optional[Path]:
    for parent in Path(__file__).resolve().parents:
        if _is_repo(parent, _REPOS["jarvis"][0]):
            return parent
    return None


@lru_cache(maxsize=None)
def repo_path(name: str) -> Optional[Path]:
    """The repository *name* ("jarvis" | "prime" | "reactor"), or None."""
    marker, env_vars = _REPOS[name]
    for var in env_vars:
        raw = (os.environ.get(var) or "").strip()
        if raw and _is_repo(Path(raw).expanduser(), marker):
            return Path(raw).expanduser().resolve()
    home = _this_repo()
    if home is None:
        return None
    if name == "jarvis":
        return home
    try:
        for sibling in sorted(home.parent.iterdir()):
            if sibling.is_dir() and _is_repo(sibling, marker):
                return sibling.resolve()
    except OSError:
        pass
    return None


__all__ = ["repo_path"]
