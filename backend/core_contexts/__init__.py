"""
Core Contexts -- the 5 execution environments that replace 39 legacy agents.

Each context is a thin orchestration layer over the atomic tools.
The 397B Architect model decides WHICH context to use and WHAT tools
to call.  The context just provides the tool access and execution
environment.

Contexts:
    Executor      -- screen vision, clicks, typing, app navigation
    Architect     -- plans, decomposes goals, selects contexts + tools
    Developer     -- code generation, review, testing, Ouroboros
    Communicator  -- messages, email, calendar, web search
    Observer      -- monitoring, anomaly detection, pattern recognition
"""
