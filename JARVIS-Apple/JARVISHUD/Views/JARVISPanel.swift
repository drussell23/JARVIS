// JARVISHUD/Views/JARVISPanel.swift
// The JARVIS Panel — slides in from the right when summoned.
// Contains all SwiftUI content: header, tabs, chat, hive, command input.
// Frosted glass via NSVisualEffectView. Standard NSPanel behavior.

import AppKit
import SwiftUI

class JARVISPanel: NSPanel {

    static let defaultWidth: CGFloat = 360
    private var hostingView: NSHostingView<AnyView>?

    init(contentView swiftUIView: AnyView) {
        guard let screen = NSScreen.main else {
            super.init(contentRect: .zero, styleMask: [.borderless, .nonactivatingPanel],
                       backing: .buffered, defer: false)
            return
        }

        // Right-anchored, full height, default width
        let panelFrame = NSRect(
            x: screen.frame.maxX - Self.defaultWidth,
            y: screen.frame.minY,
            width: Self.defaultWidth,
            height: screen.frame.height
        )

        super.init(contentRect: panelFrame,
                   styleMask: [.borderless, .nonactivatingPanel, .fullSizeContentView],
                   backing: .buffered, defer: false)

        // Panel properties
        self.isOpaque = false
        self.backgroundColor = .clear
        self.hasShadow = true
        self.level = .floating
        self.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        self.titlebarAppearsTransparent = true
        self.titleVisibility = .hidden
        self.isMovableByWindowBackground = false
        self.becomesKeyOnlyIfNeeded = true

        // Frosted glass background
        let visualEffect = NSVisualEffectView(frame: panelFrame)
        visualEffect.material = .hudWindow
        visualEffect.blendingMode = .behindWindow
        visualEffect.state = .active
        visualEffect.wantsLayer = true
        visualEffect.layer?.cornerRadius = 0

        // Host the SwiftUI content on top of the glass
        let hosting = NSHostingView(rootView: swiftUIView)
        hosting.translatesAutoresizingMaskIntoConstraints = false

        visualEffect.addSubview(hosting)
        NSLayoutConstraint.activate([
            hosting.topAnchor.constraint(equalTo: visualEffect.topAnchor),
            hosting.bottomAnchor.constraint(equalTo: visualEffect.bottomAnchor),
            hosting.leadingAnchor.constraint(equalTo: visualEffect.leadingAnchor),
            hosting.trailingAnchor.constraint(equalTo: visualEffect.trailingAnchor),
        ])

        self.contentView = visualEffect
        hostingView = hosting
    }

    // MARK: - Panel Behavior

    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }

    // MARK: - Show/Hide with Animation

    func showPanel() {
        guard let screen = NSScreen.main else { return }

        // Start offscreen to the right
        let offscreenFrame = NSRect(
            x: screen.frame.maxX,
            y: screen.frame.minY,
            width: Self.defaultWidth,
            height: screen.frame.height
        )
        self.setFrame(offscreenFrame, display: false)
        self.orderFrontRegardless()
        self.alphaValue = 0

        // Slide in
        let targetFrame = NSRect(
            x: screen.frame.maxX - Self.defaultWidth,
            y: screen.frame.minY,
            width: Self.defaultWidth,
            height: screen.frame.height
        )

        NSAnimationContext.runAnimationGroup({ context in
            context.duration = 0.35
            context.timingFunction = CAMediaTimingFunction(name: .easeOut)
            self.animator().setFrame(targetFrame, display: true)
            self.animator().alphaValue = 1.0
        })
    }

    func hidePanel() {
        guard let screen = NSScreen.main else { return }

        let offscreenFrame = NSRect(
            x: screen.frame.maxX,
            y: screen.frame.minY,
            width: Self.defaultWidth,
            height: screen.frame.height
        )

        NSAnimationContext.runAnimationGroup({ context in
            context.duration = 0.25
            context.timingFunction = CAMediaTimingFunction(name: .easeIn)
            self.animator().setFrame(offscreenFrame, display: true)
            self.animator().alphaValue = 0
        }, completionHandler: {
            self.orderOut(nil)
        })
    }

    var isPanelVisible: Bool {
        return isVisible && alphaValue > 0
    }
}
