"""CONTEXT_EXPANSION survives the process-isolated Oracle.

``expand()`` called ``self._oracle.index_age_s()`` directly; the isolated
adapter raises for every non-IPC name, so the phase raised on 113 of 113
dispatches across five soaks (bt-2026-09-21-235603 .. bt-2026-09-23-180828)
and every op ran unexpanded.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.core.ouroboros.governance.context_expander import ContextExpander
from backend.core.ouroboros.governance.op_context import OperationContext, OperationPhase
from backend.core.ouroboros.oracle_adapter import (
    InProcessOracleAdapter,
    IsolatedOracleAdapter,
    oracle_index_age_s,
)


class _Proxy:
    is_ready = True


class _Generator:
    def __init__(self):
        self.prompts = []

    async def plan(self, prompt, deadline):
        self.prompts.append(prompt)
        return '{"schema_version": "expansion.1", "additional_files_needed": [], "reasoning": "enough"}'


class _Oracle:
    def __init__(self, age):
        self._age = age

    def index_age_s(self):
        return self._age


def _ctx(tmp_path):
    (tmp_path / "a.py").write_text("pass\n")
    ctx = OperationContext.create(target_files=("a.py",), description="isolated oracle")
    return ctx.advance(OperationPhase.ROUTE).advance(OperationPhase.CONTEXT_EXPANSION)


async def test_expansion_reaches_the_model_under_the_isolated_oracle(tmp_path):
    gen = _Generator()
    exp = ContextExpander(generator=gen, repo_root=tmp_path,
                          oracle=IsolatedOracleAdapter(_Proxy()))
    deadline = datetime.now(timezone.utc) + timedelta(minutes=5)
    await exp.expand(_ctx(tmp_path), deadline)
    assert gen.prompts, "expansion never reached a planning round"


@pytest.mark.parametrize("oracle,expected", [
    (IsolatedOracleAdapter(_Proxy()), None),        # not on the IPC surface
    (InProcessOracleAdapter(_Oracle(42.0)), 42.0),   # delegated to TheOracle
    (_Oracle(7), 7.0),
    (object(), None),
    (None, None),
])
def test_index_age_is_total_and_unknown_is_none_not_fresh(oracle, expected):
    assert oracle_index_age_s(oracle) == expected
