import AppKit

/// Minimal NSApplicationDelegate. The app is LSUIElement-only and has no
/// dock icon or main window, so we don't need a window-reopen handler — but
/// we still want to make sure the process stays alive when nothing is in
/// focus.
final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        // MenuBarExtra has no windows in the traditional sense; never quit on
        // "last window closed" — only on explicit Quit from the menu.
        false
    }
}
