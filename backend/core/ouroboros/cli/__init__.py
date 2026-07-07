"""backend.core.ouroboros.cli -- console entry points.

Home of the ``ov`` dispatcher (:mod:`backend.core.ouroboros.cli.ov`), the
PEP 621 ``[project.scripts]`` target. The dispatcher translates ``ov``
subcommands into the legacy battle-test bootstrap's argv and delegates --
it never re-parses the real flags (DRY).
"""
from __future__ import annotations
