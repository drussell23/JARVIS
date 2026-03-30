//
//  ClickThroughHostingView.swift
//  JARVIS-HUD
//
//  Custom NSHostingView with intelligent click-through behavior
//  Only captures clicks on actual UI elements, passes everything else to desktop
//

import SwiftUI
import AppKit

/// Custom hosting view that enables true click-through for transparent areas
class ClickThroughHostingView<Content: View>: NSHostingView<Content> {

    /// Override hit testing to enable precise click-through
    /// Returns nil for clicks outside of UI elements, allowing desktop interaction
    override func hitTest(_ point: NSPoint) -> NSView? {
        // Get the default hit test result
        guard let hitView = super.hitTest(point) else {
            return nil
        }

        // If we hit ourselves (the hosting view), that means we hit empty space
        // Return nil to pass the click through to the desktop
        if hitView == self {
            return nil
        }

        // If we hit a SwiftUI view, check if it's an interactive element
        if isInteractiveElement(hitView) {
            return hitView
        }

        // Check if we hit a text or label (non-interactive) - pass through
        if isNonInteractiveText(hitView) {
            return nil
        }

        // Default: pass through to desktop
        return nil
    }

    /// Check if a view is an interactive element that should capture clicks
    private func isInteractiveElement(_ view: NSView) -> Bool {
        // Interactive elements that SHOULD capture clicks:
        // - NSButton (buttons)
        // - NSTextField that accepts input (text fields)
        // - NSControl subclasses

        if view is NSButton {
            return true
        }

        if let textField = view as? NSTextField, textField.isEditable {
            return true
        }

        if view is NSControl {
            return true
        }

        // Check parent hierarchy for interactive containers
        if let parent = view.superview {
            // If parent is a text field or button, this is interactive
            if parent is NSTextField || parent is NSButton {
                return true
            }
        }

        return false
    }

    /// Check if a view is non-interactive text/label
    private func isNonInteractiveText(_ view: NSView) -> Bool {
        // Non-editable text fields are just labels, pass through
        if let textField = view as? NSTextField, !textField.isEditable {
            return true
        }

        if view is NSText {
            return true
        }

        return false
    }
}

/// SwiftUI wrapper for ClickThroughHostingView
struct ClickThroughContainer<Content: View>: NSViewRepresentable {
    let content: Content

    init(@ViewBuilder content: () -> Content) {
        self.content = content()
    }

    func makeNSView(context: Context) -> ClickThroughHostingView<Content> {
        let hostingView = ClickThroughHostingView(rootView: content)

        // Make the hosting view itself transparent
        hostingView.layer?.backgroundColor = .clear

        return hostingView
    }

    func updateNSView(_ nsView: ClickThroughHostingView<Content>, context: Context) {
        nsView.rootView = content
    }
}
