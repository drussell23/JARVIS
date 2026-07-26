"""Archived diagnostic scripts. Not tests — and running them costs the operator.

What these are
--------------
``legacy/`` and ``deprecated/`` hold hand-run diagnostic scripts from earlier
eras of the weather/vision work. They are named ``test_*.py`` because that is
what they were called when someone ran them by hand, and pytest therefore
collects and RUNS them.

Why that is not merely untidy
-----------------------------
Fourteen of them call ``subprocess.run(['open', '-a', 'Weather'])`` inside a
``test_*`` function. The autonomous loop's TestWatcher runs pytest continuously,
so every sweep that reached this directory opened the Weather app on the
operator's desktop — observed live::

    20:47:18  pytest spawned by TestWatcher (pid 42556)
    20:47:30  Weather.app launches
    20:47:39  pytest exits rc=3

The operator reported this four times as "the Weather app keeps opening on its
own". It was us: a self-developing system running archived scripts that drive
the GUI.

Collection alone is safe — the launches sit inside test functions, not at
module scope — which is exactly why this hid for so long: importing these files
proves nothing, and only a full RUN reproduces it.

So the directory declares what it is, the same way
``tests/governance/fixtures`` does for the L2 corpus. Deleting them would also
work; ignoring them keeps the history readable without letting an automated
sweep act on the desktop.
"""
from __future__ import annotations

collect_ignore_glob = ["*"]
