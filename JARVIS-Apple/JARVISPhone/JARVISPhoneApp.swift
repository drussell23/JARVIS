import SwiftUI

@main
struct JARVISPhoneApp: App {
    @StateObject private var session = PhoneSessionManager()

    var body: some Scene {
        WindowGroup {
            TabView {
                CommandCenterView()
                    .tabItem { Label("Command", systemImage: "waveform") }
                PRQueueView()
                    .tabItem { Label("Ouroboros", systemImage: "arrow.triangle.2.circlepath") }
                SettingsView()
                    .tabItem { Label("Settings", systemImage: "gear") }
            }
            .preferredColorScheme(.dark)
            .environmentObject(session)
            .task { await session.connect() }
        }
    }
}
