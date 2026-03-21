"""Load capabilities.yaml into a TopologyMap for the CuriosityEngine."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import yaml

from backend.core.topology.topology_map import CapabilityNode, TopologyMap

logger = logging.getLogger(__name__)

DEFAULT_CAPABILITIES_PATH = Path(__file__).parent / "capabilities.yaml"


def load_topology(path: Optional[Path] = None) -> TopologyMap:
    """Load capabilities.yaml and return a populated TopologyMap.

    Args:
        path: Override path to YAML file. Defaults to capabilities.yaml
              in the same directory as this module.

    Returns:
        TopologyMap with all capabilities registered and edges wired.
    """
    yaml_path = path or DEFAULT_CAPABILITIES_PATH
    if not yaml_path.exists():
        logger.warning("[TopologyLoader] %s not found — returning empty map", yaml_path)
        return TopologyMap()

    with open(yaml_path) as f:
        data = yaml.safe_load(f)

    if not data or "capabilities" not in data:
        logger.warning("[TopologyLoader] No capabilities in %s", yaml_path)
        return TopologyMap()

    topo = TopologyMap()
    entries = data["capabilities"]

    # First pass: register all nodes
    for entry in entries:
        node = CapabilityNode(
            name=entry["name"],
            domain=entry["domain"],
            repo_owner=entry["repo_owner"],
            active=entry.get("active", False),
        )
        topo.register(node)

    # Second pass: wire dependency edges
    for entry in entries:
        deps = entry.get("dependencies", [])
        if deps:
            topo.edges[entry["name"]] = set(deps)

    active = sum(1 for n in topo.nodes.values() if n.active)
    total = len(topo.nodes)
    domains = len(topo.all_domains())
    logger.info(
        "[TopologyLoader] Loaded %d capabilities (%d active) across %d domains from %s",
        total, active, domains, yaml_path.name,
    )
    return topo
