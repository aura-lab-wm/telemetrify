import AppKit
import ServiceManagement

/// Minimal NSApplicationDelegate. The app is LSUIElement-only and has no
/// dock icon or main window, so we don't need a window-reopen handler — but
/// we still want to make sure the process stays alive when nothing is in
/// focus.
final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        registerLoginItem()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        // MenuBarExtra has no windows in the traditional sense; never quit on
        // "last window closed" — only on explicit Quit from the menu.
        false
    }

    /// Launch-at-login via SMAppService (macOS 13+). Idempotent — calling
    /// register() when already enabled is a no-op — so running it on every
    /// launch is self-healing: a reinstall or an OS that dropped the item
    /// re-registers it automatically, no external Login Items hack. We only
    /// register, never force-unregister: if the user turns it off in System
    /// Settings we respect that.
    private func registerLoginItem() {
        let service = SMAppService.mainApp
        guard service.status != .enabled else { return }
        do {
            try service.register()
        } catch {
            NSLog("ai-pulse: could not register login item: \(error)")
        }
    }
}
