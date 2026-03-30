import SwiftUI
import AppKit
import JARVISKit

@main
struct JARVISHUDApp: App {
    @NSApplicationDelegateAdaptor(HUDAppDelegate.self) var appDelegate

    var body: some Scene {
        WindowGroup {
            Text("JARVIS HUD")
                .frame(width: 0, height: 0)
                .hidden()
        }
        .windowStyle(.hiddenTitleBar)
    }
}

class HUDAppDelegate: NSObject, NSApplicationDelegate {
    var window: TransparentWindow?

    func applicationDidFinishLaunching(_ notification: Notification) {
        // HUD will be wired to Vercel SSE in a future update
        // For now, launches the existing HUD view
    }
}
