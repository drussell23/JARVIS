"""Reactive state propagation system -- replaces env-var signalling.

Provides a versioned, journaled, ownership-aware key-value store that
notifies watchers on change.  Every mutation is recorded in an
append-only journal with monotonic version numbers.

Design rules
------------
* **No** third-party or JARVIS imports -- stdlib only.
* All data types are frozen dataclasses (immutable value objects).
* Writers are identified by (writer, session_id) pairs.
* Every key carries an epoch; epoch bumps invalidate stale writers.
"""
