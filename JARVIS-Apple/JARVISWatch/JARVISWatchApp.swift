import SwiftUI

@main
struct JARVISWatchApp: App {
    @StateObject private var session = WatchSessionManager()

    var body: some Scene {
        WindowGroup {
            StatusView()
                .environmentObject(session)
        }
    }
}
