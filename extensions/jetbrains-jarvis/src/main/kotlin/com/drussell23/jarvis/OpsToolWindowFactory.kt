package com.drussell23.jarvis

import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.project.Project
import com.intellij.openapi.wm.ToolWindow
import com.intellij.openapi.wm.ToolWindowFactory
import com.intellij.ui.components.JBScrollPane
import com.intellij.ui.content.ContentFactory
import java.awt.BorderLayout
import javax.swing.DefaultListModel
import javax.swing.JList
import javax.swing.JPanel
import javax.swing.ListSelectionModel

/**
 * Tool window factory — registers the "JARVIS Ops" sidebar.
 *
 * The sidebar renders a live list of op IDs backed by the
 * [OpsController]. Selecting an op dispatches to
 * [OpDetailRenderer] to show the task projection in a dedicated
 * content tab.
 *
 * All UI updates dispatch onto the EDT via
 * [ApplicationManager.invokeLater] so the SSE consumer thread
 * never touches Swing directly.
 */
class OpsToolWindowFactory : ToolWindowFactory {
    override fun createToolWindowContent(project: Project, toolWindow: ToolWindow) {
        val panel = JPanel(BorderLayout())
        val listModel = DefaultListModel<String>()
        val opList = JList(listModel).apply {
            selectionMode = ListSelectionModel.SINGLE_SELECTION
        }
        panel.add(JBScrollPane(opList), BorderLayout.CENTER)

        val content = ContentFactory.getInstance().createContent(panel, "Ops", false)
        toolWindow.contentManager.addContent(content)

        val controller = OpsController(
            settings = JarvisSettings.getInstance(),
            onOpsChanged = { opIds ->
                ApplicationManager.getApplication().invokeLater {
                    listModel.clear()
                    for (id in opIds) listModel.addElement(id)
                }
            },
        )
        opList.addListSelectionListener { e ->
            if (!e.valueIsAdjusting) {
                opList.selectedValue?.let { controller.openOp(it) }
            }
        }
        // Autostart if the plugin is enabled in settings.
        if (JarvisSettings.getInstance().enabled) {
            controller.start()
        }
    }
}
