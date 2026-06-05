import SwiftUI
import AIPulseCore

/// LSUIElement-only app: NO `WindowGroup` in the Scene. The MenuBarExtra is
/// the entire user surface. Adding a WindowGroup here would race the status
/// item attachment on launch (the bug aura-pulse hit) and would also pull a
/// dock icon back in — both undesirable for a menubar utility.
@main
struct AIPulseApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var store = StatusStore()

    var body: some Scene {
        MenuBarExtra {
            StatusView()
                .environmentObject(store)
                .task {
                    store.start()
                }
        } label: {
            // The label exists from launch (the popover content doesn't),
            // so THIS is what brings polling + the live stream up before
            // the popover is ever opened — the menubar dot is live
            // immediately. start() is idempotent; the popover's .task
            // calling it again is harmless.
            MenuBarIcon(store: store)
                .task { store.start() }
        }
        .menuBarExtraStyle(.window)
    }
}
