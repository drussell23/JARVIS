package com.drussell23.jarvis

import com.intellij.openapi.application.ApplicationManager
import com.intellij.ui.components.JBScrollPane
import java.awt.BorderLayout
import javax.swing.JEditorPane
import javax.swing.JPanel

/**
 * Swing panel that renders a [TaskDetail] as read-only HTML.
 *
 * Mounted as a second `Content` on the JARVIS Ops tool window by
 * [OpsToolWindowFactory]. Listens to [OpDetailRenderer.sink] — the
 * [OpsController] sets the detail via `OpDetailRenderer.render()`
 * whenever the user selects an op in the tree or a stream event
 * changes the active op.
 *
 * Thread safety: every `sink` call is dispatched onto the EDT via
 * [ApplicationManager.invokeLater] because the controller fires
 * from an IO-dispatcher coroutine.
 */
class OpDetailPanel : JPanel(BorderLayout()) {

    private val editor: JEditorPane = JEditorPane().apply {
        contentType = "text/html"
        isEditable = false
        text = "<html><body><p><em>Select an op to see detail.</em></p></body></html>"
    }

    init {
        add(JBScrollPane(editor), BorderLayout.CENTER)
        OpDetailRenderer.sink = { detail ->
            ApplicationManager.getApplication().invokeLater {
                editor.text = OpDetailRenderer.renderToHtml(detail)
                // Scroll to top — selecting a new op shouldn't
                // inherit the previous op's scroll position.
                editor.caretPosition = 0
            }
        }
    }
}
