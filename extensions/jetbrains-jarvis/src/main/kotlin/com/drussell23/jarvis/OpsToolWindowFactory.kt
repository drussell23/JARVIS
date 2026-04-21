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
 * Tool window factory — registers the "JARVIS Ops" sidebar with
 * two content tabs:
 *
 *   * **Ops**    — live list of op IDs backed by [OpsController]
 *   * **Detail** — HTML-rendered [TaskDetail] for the selected op
 *
 * All UI updates dispatch onto the EDT via
 * [ApplicationManager.invokeLater] so the SSE consumer thread
 * never touches Swing directly. [OpDetailRenderer.sink] is
 * installed by [OpDetailPanel] — the controller calls
 * `OpDetailRenderer.render(detail)` and the panel updates on its
 * own.
 */
class OpsToolWindowFactory : ToolWindowFactory {
    override fun createToolWindowContent(project: Project, toolWindow: ToolWindow) {
        val opsPanel = JPanel(BorderLayout())
        val listModel = DefaultListModel<String>()
        val opList = JList(listModel).apply {
            selectionMode = ListSelectionModel.SINGLE_SELECTION
        }
        opsPanel.add(JBScrollPane(opList), BorderLayout.CENTER)

        val detailPanel = OpDetailPanel()

        val contentFactory = ContentFactory.getInstance()
        toolWindow.contentManager.addContent(
            contentFactory.createContent(opsPanel, "Ops", false)
        )
        toolWindow.contentManager.addContent(
            contentFactory.createContent(detailPanel, "Detail", false)
        )

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
        if (JarvisSettings.getInstance().enabled) {
            controller.start()
        }
    }
}
