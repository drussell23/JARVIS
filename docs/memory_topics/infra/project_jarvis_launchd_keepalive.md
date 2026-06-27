---
title: Project Jarvis Launchd Keepalive
modules: [backend/loading_server.py, scripts/run_supervisor.sh]
status: historical
source: project_jarvis_launchd_keepalive.md
---

**When asked to "shut down JARVIS", a plain `kill`/SIGKILL is NOT
enough — it respawns within ~30s.** `unified_supervisor.py` (+ its
child `backend/loading_server.py`) is managed by a **launchd
KeepAlive job** `com.jarvis.supervisor`, plist at
`~/Library/LaunchAgents/com.jarvis.supervisor.plist` (wrapper
`scripts/run_supervisor.sh`; RunAtLoad=false so it's a manual
`launchctl start` + KeepAlive, not login-autostart). Killing the PID
just makes launchd start a fresh one (new pid each time).

**Durable, reversible stop (the correct fix):**
```
launchctl disable gui/$(id -u)/com.jarvis.supervisor   # persistent
launchctl bootout  gui/$(id -u)/com.jarvis.supervisor  # may say "No such process" — fine
pkill -9 -f 'unified_supervisor.py|backend/loading_server.py'
```
`disable` survives reboot until reversed. **Restore with**
`launchctl enable gui/$(id -u)/com.jarvis.supervisor` then
`launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.jarvis.supervisor.plist`
(or `launchctl start com.jarvis.supervisor`). Do NOT `rm` the plist
(that's the destructive UNINSTALL path the operator did not ask for).

Verify with `launchctl print gui/$(id -u)/com.jarvis.supervisor`
+ `pgrep -fl unified_supervisor.py` after a delay (respawn would
reappear within ~30s if only killed, not disabled).
