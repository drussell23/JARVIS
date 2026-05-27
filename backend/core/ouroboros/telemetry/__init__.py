"""Ouroboros telemetry package.

Diagnostic-only utilities for instrumenting the asyncio control plane.
Slice 33 Arc 0 introduces :mod:`loop_sink` — a non-invasive blocking-time
recorder used to identify which on-loop call-sites starve the event
loop in production soaks.

Modules in this package MUST NOT introduce coupling into orchestration,
governance, or provider code. They are pure observability utilities.
"""
