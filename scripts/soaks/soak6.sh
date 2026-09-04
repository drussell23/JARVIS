#!/usr/bin/env bash
# Farming soak with sibling entropy + structural acceptance ON.
#
# Measures the only number that matters for the flywheel:
# metadata.n_distinct_structures in the trajectory corpus. Before this
# arc, 8 sibling rows across 3 groups carried 3 distinct answers and
# every group collapsed to one, so zero preference pairs were
# constructible.
cd /mnt/c/Users/Jarvis/Desktop/TrinityAi/jarvis || exit 1

# Recorder is already true in .env; stated here so the run is
# self-describing in the log.
export JARVIS_TRAJECTORY_RECORDER_ENABLED=true

# The arc under test. Defaults are already these values; exported so the
# soak record says what it ran rather than what it assumed.
export JARVIS_SIBLING_ENTROPY_ENABLED=true
export JARVIS_SIBLING_DIVERSITY_THRESHOLD=0.95
export JARVIS_SIBLING_MAX_RESAMPLE=1
export JARVIS_SIBLING_TEMP_CEILING=1.15
export JARVIS_LOCAL_SIBLING_CANDIDATES=3

exec /home/jarvis_svc/.venvs/ov/bin/python scripts/ouroboros_battle_test.py \
  --headless --cost-cap 0.50 --idle-timeout 900 --max-wall-seconds 2400 -v
