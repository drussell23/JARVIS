"""SemanticGuardian holes the Antivenom audit exposed (2026-10-09).

Probed as the CHANGES they are, six P9.4 shapes slipped the guardian:
a body gutted to ``return True``, a risk tier lowered, a read-only flag
flipped, ``assertEqual`` -> ``assertNotEqual``, ``==`` -> ``!=`` and an auth
guard inverted around a call; three quine-shape tests had no detector at all;
and a segmented ``sk-proj-`` key matched no credential shape. Each closure is
pinned here with the shapes that must stay SILENT beside it.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.semantic_guardian import SemanticGuardian


@pytest.fixture
def guard(monkeypatch):
    monkeypatch.setenv("JARVIS_SEMANTIC_GUARD_ENABLED", "1")
    return SemanticGuardian()


def _hits(guard, old, new, path="x.py"):
    return {d.pattern for d in guard.inspect(file_path=path, old_content=old, new_content=new)}


# ----------------------------------------------------------------- polarity


@pytest.mark.parametrize("old,new", [
    ("self.assertEqual(a, b)", "self.assertNotEqual(a, b)"),
    ("self.assertIn(a, b)", "self.assertNotIn(a, b)"),
    ("self.assertIsNone(a)", "self.assertIsNotNone(a)"),
    ("self.assertTrue(a)", "self.assertFalse(a)"),
    ("m.assert_called()", "m.assert_not_called()"),
    ("assert a == b", "assert a != b"),
    ("assert a is b", "assert a is not b"),
    ("assert a in b", "assert a not in b"),
    ("assert a < b", "assert a >= b"),
    ("assert a", "assert not a"),
])
def test_every_spelling_of_an_assertion_flip_is_inverted(guard, old, new):
    body = "class T(unittest.TestCase):\n    def test_x(self):\n        {}\n"
    assert "test_assertion_inverted" in _hits(guard, body.format(old), body.format(new), "tests/test_t.py")


def test_double_negation_is_not_a_flip(guard):
    body = "def test_x():\n    {}\n"
    hits = _hits(guard, body.format("assert a == b"), body.format("assert not a != b"), "tests/test_t.py")
    assert "test_assertion_inverted" not in hits and "test_assertion_weakened" not in hits


def test_unittest_value_weakening_is_now_seen(guard):
    body = "class T(unittest.TestCase):\n    def test_x(self):\n        self.assertEqual(f(), {})\n"
    assert "test_assertion_weakened" in _hits(guard, body.format(5), body.format(6), "tests/test_t.py")


# -------------------------------------------------------------------- guard


def test_raise_guard_around_a_call_inverted_fires(guard):
    old = "def g(u):\n    if not authed(u):\n        raise PermissionError(u)\n    return u\n"
    new = "def g(u):\n    if authed(u):\n        raise PermissionError(u)\n    return u\n"
    assert "guard_boolean_inverted" in _hits(guard, old, new)


def test_comparison_guard_inverted_fires(guard):
    old = "def g(n):\n    if n == 0:\n        return None\n    return 1 / n\n"
    new = "def g(n):\n    if n != 0:\n        return None\n    return 1 / n\n"
    assert "guard_boolean_inverted" in _hits(guard, old, new)


def test_a_new_guard_is_not_an_inversion(guard):
    old = "def g(u):\n    return u\n"
    new = "def g(u):\n    if not authed(u):\n        raise PermissionError(u)\n    return u\n"
    assert "guard_boolean_inverted" not in _hits(guard, old, new)


# ----------------------------------------------------------------- collapse


def test_decision_gutted_to_a_constant_is_collapsed(guard):
    old = (
        "def authorize(user, action):\n    '''doc'''\n"
        "    if user is None:\n        return False\n"
        "    grants = user.grants\n    return action in grants\n"
    )
    new = "def authorize(user, action):\n    '''doc'''\n    return True\n"
    assert "function_body_collapsed" in _hits(guard, old, new)


def test_a_constant_computation_folded_is_a_refactor(guard):
    # The old body never read its inputs: it was a constant all along.
    old = "def f(x):\n    a = 1\n    b = 2\n    c = 3\n    return a + b + c\n"
    new = "def f(x):\n    return 6\n"
    assert "function_body_collapsed" not in _hits(guard, old, new)


def test_a_non_constant_simplification_is_a_refactor(guard):
    old = "def f(x):\n    a = x + 1\n    b = a * 2\n    c = b - 2\n    return c\n"
    new = "def f(x):\n    return 2 * x\n"
    assert "function_body_collapsed" not in _hits(guard, old, new)


# -------------------------------------------------------------- credentials

_KEY = "sk-proj-" + "abcdef0123456789" * 3


@pytest.mark.parametrize("new", [
    f"av_alias = '{_KEY}'\n",
    f"API_KEY = ('{_KEY}')\n",
    f"API_KEY = '{_KEY[:9]}' + '{_KEY[9:]}'\n",
    "ACCESS_TOKEN = ('p4ssw0rd_with_symbols')\n",
    "requests.get(url, api_key='hunter22hunter22')\n",
    "cfg = {'secret_key': 'hunter22hunter22'}\n",
    "cfg['auth_token'] = 'hunter22hunter22'\n",
])
def test_credentials_are_judged_by_what_the_code_binds(guard, new):
    assert "credential_shape_introduced" in _hits(guard, "", new)


def test_a_re_spelled_existing_key_is_not_introduced(guard):
    old = f"API_KEY = '{_KEY}'\n"
    new = f"API_KEY = ('{_KEY[:9]}' + '{_KEY[9:]}')\n"
    assert "credential_shape_introduced" not in _hits(guard, old, new)


def test_a_hyphenated_identifier_is_not_a_key(guard):
    assert "credential_shape_introduced" not in _hits(
        guard, "", "x = 'sk-learn-compatible-estimator-name'\n",
    )


# --------------------------------------------------------------- permission


@pytest.mark.parametrize("old,new", [
    ("risk_tier = RiskTier.APPROVAL_REQUIRED\n", "risk_tier = RiskTier.SAFE_AUTO\n"),
    ("floor = 'blocked'\n", "floor = 'notify_apply'\n"),
    ("is_read_only = True\n", "is_read_only = False\n"),
    ("requests.get(u, verify=True)\n", "requests.get(u, verify=False)\n"),
    ("allow_unsafe = False\n", "allow_unsafe = True\n"),
    ("disable_guard = False\n", "disable_guard = True\n"),
])
def test_governance_loosening_fires(guard, old, new):
    assert "permission_loosened" in _hits(guard, old, new)


@pytest.mark.parametrize("old,new", [
    ("risk_tier = RiskTier.SAFE_AUTO\n", "risk_tier = RiskTier.APPROVAL_REQUIRED\n"),
    ("is_read_only = False\n", "is_read_only = True\n"),
    ("timeout = 5\n", "timeout = 3\n"),
    ("verbose = True\n", "verbose = False\n"),
    ("", "is_read_only = False\n"),                         # new binding, not a loosening
])
def test_tightening_and_unrelated_changes_are_silent(guard, old, new):
    assert "permission_loosened" not in _hits(guard, old, new)


def test_bindings_are_scoped_to_their_function(guard):
    # The same name in two functions is two bindings: f's flag going
    # False is not g's flag loosening.
    old = "def f():\n    strict = False\n\ndef g():\n    strict = True\n"
    new = "def f():\n    strict = False\n\ndef g():\n    strict = True\n\ndef h():\n    strict = False\n"
    assert "permission_loosened" not in _hits(guard, old, new)


def test_the_tier_ladder_is_the_active_one(guard, monkeypatch):
    from backend.core.ouroboros.governance import semantic_guardian as sg
    monkeypatch.setattr(sg, "_active_tier_order", lambda: {"low": 0, "custom_high": 7})
    assert "permission_loosened" in _hits(guard, "t = 'custom_high'\n", "t = 'low'\n")


def test_flag_vocabulary_is_env_configurable(guard, monkeypatch):
    monkeypatch.setenv("JARVIS_SEMGUARD_RESTRICTIVE_FLAG_WORDS", "fenced")
    assert "permission_loosened" in _hits(guard, "fenced = True\n", "fenced = False\n")
    assert "permission_loosened" not in _hits(guard, "is_read_only = True\n", "is_read_only = False\n")


# ---------------------------------------------------------------- tautology

_T = "tests/test_t.py"


@pytest.mark.parametrize("src", [
    "def test_a():\n    m = MagicMock()\n    assert m.method() == m.method()\n",
    "def test_a():\n    assert MagicMock(spec=Payment).process(amount=1)\n",
    "def test_a():\n    with patch('mod.auth') as p:\n        p.return_value = True\n        assert p.return_value is True\n",
    "@patch('mod.auth')\ndef test_a(p):\n    p.return_value = 3\n    assert p.return_value == 3\n",
    "def test_a():\n    x = compute()\n    assert x == x\n",
])
def test_a_test_of_its_own_doubles_is_a_tautology(guard, src):
    assert "test_tautology" in _hits(guard, "", src, _T)


@pytest.mark.parametrize("src", [
    "def test_a():\n    assert add(1, 2) == 3\n",
    "def test_a():\n    hook = Mock()\n    run(hook)\n    hook.assert_called_once()\n",
    "def test_a():\n    with patch('m.f') as f:\n        f.return_value = 2\n        assert compute() == 4\n",
    "class T(unittest.TestCase):\n    def test_a(self):\n        self.assertEqual(widget.size(), 3)\n",
    "def test_a():\n    m = MagicMock()\n    assert m is not None\n    assert service(m).ok\n",
])
def test_a_test_that_exercises_real_code_is_not(guard, src):
    assert "test_tautology" not in _hits(guard, "", src, _T)


def test_tautology_judges_only_changed_tests_and_only_test_files(guard):
    src = "def test_a():\n    m = MagicMock()\n    assert m.x() == m.x()\n"
    assert "test_tautology" not in _hits(guard, src, src, _T)          # unchanged legacy debt
    assert "test_tautology" not in _hits(guard, "", src, "pkg/mod.py")  # not a test module


def test_unparseable_inputs_never_raise(guard):
    for old, new in [("def (", "x = 1\n"), ("x = 1\n", "def ("), ("", "")]:
        guard.inspect(file_path=_T, old_content=old, new_content=new)
