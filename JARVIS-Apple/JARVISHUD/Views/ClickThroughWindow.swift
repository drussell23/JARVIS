//
//  ClickThroughWindow.swift
//  JARVIS-HUD
//
//  Intelligent click-through window for JARVIS overlay
//  Passes clicks to desktop except when clicking on actual UI elements
//

import SwiftUI
import AppKit

/// Custom NSWindow with intelligent click-through behavior
/// Only intercepts clicks on actual JARVIS UI elements, passes everything else to desktop
class ClickThroughWindow: NSWindow {

    override init(contentRect: NSRect, styleMask style: NSWindow.StyleMask, backing backingStoreType: NSWindow.BackingStoreType, defer flag: Bool) {
        super.init(contentRect: contentRect, styleMask: style, backing: backingStoreType, defer: flag)

        // Make window completely transparent
        self.isOpaque = false
        self.backgroundColor = .clear
        self.hasShadow = false

        // Remove all window chrome
        self.titlebarAppearsTransparent = true
        self.titleVisibility = .hidden
        self.styleMask.insert(.borderless)
        self.styleMask.insert(.fullSizeContentView)

        // Always on top, works in all Spaces
        self.level = .statusBar
        self.collectionBehavior = [.canJoinAllSpaces, .stationary, .fullScreenAuxiliary]

        // CRITICAL: Enable click-through for transparent areas
        // Only UI elements (buttons, text fields) will capture clicks
        self.ignoresMouseEvents = false
    }

    /// Override hit testing to enable smart click-through
    /// Returns nil for transparent areas, allowing clicks to pass through to desktop
    override func contentView(at point: NSPoint) -> NSView? {
        // Get the view at this point
        guard let contentView = self.contentView else { return nil }

        // Convert point to content view coordinates
        let localPoint = contentView.convert(point, from: nil)

        // Hit test to find deepest view at this point
        if let hitView = contentView.hitTest(localPoint) {
            // Check if this is an actual interactive element or just background
            if isInteractiveView(hitView) {
                return hitView
            }
        }

        // No interactive element found - pass click through to desktop
        return nil
    }

    /// Determine if a view should intercept mouse events
    private func isInteractiveView(_ view: NSView) -> Bool {
        // Allow clicks on these interactive elements:
        // - NSButton (buttons)
        // - NSTextField (text input)
        // - NSTextView (text areas)
        // - Any view with a click gesture recognizer
        // - Any view that is not just a container

        if view is NSButton { return true }
        if view is NSTextField { return true }
        if view is NSTextView { return true }
        if view.isKind(of: NSControl.self) { return true }

        // Check for gesture recognizers (SwiftUI buttons)
        if !view.gestureRecognizers.isEmpty { return true }

        // Check if view has a non-clear background (likely a UI element)
        if let backgroundColor = view.layer?.backgroundColor,
           backgroundColor != NSColor.clear.cgColor {
            return true
        }

        return false
    }

    /// Allow the window to become key (receive keyboard input)
    override var canBecomeKey: Bool {
        return true
    }

    /// Allow the window to become main
    override var canBecomeMain: Bool {
        return true
    }
}

/// SwiftUI wrapper for ClickThroughWindow
struct ClickThroughWindowAccessor: NSViewRepresentable {

    func makeNSView(context: Context) -> NSView {
        let view = NSView()
        DispatchQueue.main.async {
            if let window = view.window as? ClickThroughWindow {
                // Window is already configured by ClickThroughWindow init
                // Just ensure it covers the screen
                if let screen = NSScreen.main {
                    window.setFrame(screen.frame, display: true)
                }
            }
        }
        return view
    }

    func updateNSView(_ nsView: NSView, context: Context) {}
}
