"""Voice→build intent classification for Karen Full-Duplex Voice (Sprint 4).

Decides whether a final spoken transcript is a BUILD command (route to the
governed loop) or should be IGNOREd (chat/noise). Pluggable via the
`VoiceIntentClassifier` protocol with a deterministic default whose verb set
is injectable, not a hardcoded frozen table.
"""
from __future__ import annotations
