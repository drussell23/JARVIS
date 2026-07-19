"""Structural pytest isolation (mandate 2): the quarantine namespace is
NEVER collected — discovery must not execute migrated zones or fire
breach beacons."""
collect_ignore_glob = ["*"]
