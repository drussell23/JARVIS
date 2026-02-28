"""
Process-early runtime safety guards for scientific native libraries.

Loaded automatically by Python's site module when this repository is on
sys.path (for example when running `python3 unified_supervisor.py` from the
workspace root). This is intentionally minimal and side-effect free except
for safe environment defaults.
"""

from __future__ import annotations

import os


def _set_default(name: str, value: str) -> None:
    # Preserve explicit operator/runtime settings.
    if not os.environ.get(name):
        os.environ[name] = value


# OpenBLAS/BLAS safety:
# - Crash reports show native segfaults in libopenblas threading paths.
# - Enforcing single-threaded BLAS by default avoids oversubscription races
#   and known instability on some Apple Python + OpenBLAS combinations.
_set_default("OPENBLAS_NUM_THREADS", "1")
_set_default("OMP_NUM_THREADS", "1")
_set_default("MKL_NUM_THREADS", "1")
_set_default("VECLIB_MAXIMUM_THREADS", "1")
_set_default("NUMEXPR_NUM_THREADS", "1")
_set_default("GOTO_NUM_THREADS", "1")
_set_default("OPENBLAS_MAIN_FREE", "1")

# v279.1: Pin ARM64 kernel to avoid dynamic dispatch bugs in OpenBLAS 0.3.23.dev.
import platform as _platform
if _platform.machine() in ("arm64", "aarch64"):
    _set_default("OPENBLAS_CORETYPE", "ARMV8")
del _platform

