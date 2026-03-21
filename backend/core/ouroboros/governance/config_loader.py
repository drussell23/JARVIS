"""Multi-level YAML config inheritance for the Ouroboros governance layer (GAP 7).

Layer loading order (later layers override earlier ones):
  1. <global_root>/.jarvis/governance.yaml   — user-global defaults
  2. <repo_root>/.jarvis/governance.yaml     — per-repo overrides
  3. <repo_root>/.jarvis/governance.local.yaml — local (gitignored) overrides

Environment variables always take precedence over every YAML layer;
that precedence is enforced by the caller (GovernedLoopConfig.from_env).

Public API
----------
load_layered_config(global_root, repo_root) -> Dict[str, Any]
    Merge all reachable layers and return the flattened dict.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("Ouroboros.ConfigLoader")


def _load_yaml_dict(path: Path) -> Optional[Dict[str, Any]]:
    """Load a single YAML file and return its contents as a dict.

    Returns None when:
    - The file does not exist.
    - The file contains invalid YAML (parse error).
    - The top-level value is not a mapping (e.g. a list or scalar).

    Never raises; all errors are logged at DEBUG/WARNING level.
    """
    if not path.exists():
        return None

    try:
        import yaml  # type: ignore[import]
    except ImportError:
        try:
            # Fallback: stdlib pyyaml-compatible shim is not available;
            # attempt a minimal safe_load via the pyyaml package name.
            import importlib
            yaml = importlib.import_module("yaml")
        except ImportError:
            logger.warning(
                "PyYAML is not installed; cannot load config file %s. "
                "Install pyyaml to enable YAML config inheritance.",
                path,
            )
            return None

    try:
        raw = path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Skipping malformed YAML at %s: %s", path, exc)
        return None

    if data is None:
        # Empty file is treated as an empty dict (valid, not skipped).
        return {}

    if not isinstance(data, dict):
        logger.debug(
            "Skipping non-dict YAML at %s (got %s)", path, type(data).__name__
        )
        return None

    return dict(data)


def load_layered_config(
    global_root: Path,
    repo_root: Path,
) -> Dict[str, Any]:
    """Merge governance YAML layers and return the combined configuration dict.

    Parameters
    ----------
    global_root:
        Root directory that contains the user-global ``.jarvis/governance.yaml``.
        Typically ``Path.home()``.
    repo_root:
        Root of the current repository/project.  Contains both the per-repo
        ``.jarvis/governance.yaml`` and the optional
        ``.jarvis/governance.local.yaml``.

    Returns
    -------
    dict
        Merged key→value mapping.  Empty dict when no layers are found.
    """
    layers: list[Path] = [
        global_root / ".jarvis" / "governance.yaml",
        repo_root / ".jarvis" / "governance.yaml",
        repo_root / ".jarvis" / "governance.local.yaml",
    ]

    merged: Dict[str, Any] = {}
    for layer_path in layers:
        layer_data = _load_yaml_dict(layer_path)
        if layer_data is not None:
            merged.update(layer_data)
            logger.debug(
                "Loaded config layer %s (%d keys)", layer_path, len(layer_data)
            )

    return merged
