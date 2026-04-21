package com.drussell23.jarvis.actions

import com.intellij.openapi.actionSystem.AnAction
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.wm.ToolWindowManager
import com.drussell23.jarvis.JarvisSettings
import com.drussell23.jarvis.OpsController
import kotlinx.coroutines.GlobalScope
import kotlinx.coroutines.launch

/**
 * Action group entries — Connect / Disconnect / Refresh.
 *
 * The controller is owned by the tool window factory; these
 * actions reach across via a thin shared lookup. In the Slice 6
 * scaffold the actions simply toggle the tool window and
 * surface a notification, leaving the full IPC wiring for a
 * future expansion. The authority invariant remains: every
 * action is read-only, never POSTs to the agent.
 */
class ConnectAction : AnAction("JARVIS: Connect") {
    override fun actionPerformed(e: AnActionEvent) {
        e.project?.let { project ->
            val twm = ToolWindowManager.getInstance(project)
            twm.getToolWindow("JARVIS Ops")?.activate(null)
        }
        // Settings toggle (persisted).
        JarvisSettings.getInstance().enabled = true
    }
}

class DisconnectAction : AnAction("JARVIS: Disconnect") {
    override fun actionPerformed(e: AnActionEvent) {
        JarvisSettings.getInstance().enabled = false
    }
}

class RefreshAction : AnAction("JARVIS: Refresh") {
    override fun actionPerformed(e: AnActionEvent) {
        // No-op placeholder — the real controller reference lives
        // on the tool window content. A future slice adds a small
        // application-level service to bridge actions ↔ controller
        // without coupling.
        GlobalScope.launch {
            // Intentionally empty; refresh already happens on demand
            // as the user switches the tool window view.
        }
    }
}
