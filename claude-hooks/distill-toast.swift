import AppKit
import Darwin

// distill-toast — a self-owned styled HUD (gradient pill + icon + text). Used by
// the SessionEnd /distill nudge AND the SessionStart recording disclosure.
// Native notifications can't be themed and depend on per-bundle banner
// permissions; this floating panel always renders and is fully styled.
//
// Args: <title> <subtitle> <body> [iconPath] [hex1] [hex2]
//   iconPath defaults to ~/.claude/hooks/distill-icon.png
//   hex1/hex2 default to the magenta theme; both must be given to take effect.

let args = CommandLine.arguments
let title    = args.count > 1 ? args[1] : "distill?"
let subtitle = args.count > 2 ? args[2] : ""
let body     = args.count > 3 ? args[3] : "Run /distill to bank items before they fade."
let iconPath = args.count > 4 && !args[4].isEmpty
    ? (args[4] as NSString).expandingTildeInPath
    : ("~/.claude/hooks/distill-icon.png" as NSString).expandingTildeInPath

func hexColor(_ hex: String, _ fallback: NSColor) -> NSColor {
    var s = hex.trimmingCharacters(in: .whitespaces)
    if s.hasPrefix("#") { s.removeFirst() }
    guard s.count == 6, let v = Int(s, radix: 16) else { return fallback }
    return NSColor(srgbRed: CGFloat((v >> 16) & 0xff) / 255.0,
                   green: CGFloat((v >> 8) & 0xff) / 255.0,
                   blue: CGFloat(v & 0xff) / 255.0, alpha: 1)
}
let c1 = args.count > 5 ? hexColor(args[5], NSColor(srgbRed: 0.7647, green: 0.2157, blue: 0.3922, alpha: 1))
                        : NSColor(srgbRed: 0.7647, green: 0.2157, blue: 0.3922, alpha: 1)
let c2 = args.count > 6 ? hexColor(args[6], NSColor(srgbRed: 0.1137, green: 0.1490, blue: 0.4431, alpha: 1))
                        : NSColor(srgbRed: 0.1137, green: 0.1490, blue: 0.4431, alpha: 1)

// Stacking slot: multiple sessions can fire at once, each spawning its own toast.
// Claim the lowest free slot (PID-stamped lock file, stale entries reclaimed) so
// concurrent toasts stack downward instead of overlapping. Released on fade-out.
func claimSlot(_ maxSlots: Int) -> Int {
    let fm = FileManager.default
    for n in 0..<maxSlots {
        let path = "/tmp/distill-toast-slot-\(n).lock"
        if fm.fileExists(atPath: path) {
            if let s = try? String(contentsOfFile: path, encoding: .utf8),
               let pid = pid_t(s.trimmingCharacters(in: .whitespacesAndNewlines)),
               kill(pid, 0) == 0 {
                continue                          // held by a live toast
            }
            try? fm.removeItem(atPath: path)      // stale -> reclaim
        }
        let fd = open(path, O_CREAT | O_EXCL | O_WRONLY, 0o644)
        if fd >= 0 {
            _ = "\(getpid())\n".withCString { write(fd, $0, strlen($0)) }
            close(fd)
            return n
        }
    }
    return 0
}
let slot = claimSlot(6)
let slotPath = "/tmp/distill-toast-slot-\(slot).lock"

let app = NSApplication.shared
app.setActivationPolicy(.accessory)

// layout metrics
let W: CGFloat = 400, PAD: CGFloat = 18, iconSize: CGFloat = 56
let tx = PAD + iconSize + 14
let tw = W - tx - PAD

// size the panel to the body (1–3 lines)
let bodyFont = NSFont.systemFont(ofSize: 12.5, weight: .regular)
let bodyMeasured = NSAttributedString(string: body, attributes: [.font: bodyFont])
    .boundingRect(with: NSSize(width: tw, height: 200),
                  options: [.usesLineFragmentOrigin, .usesFontLeading]).height
let bodyH = min(ceil(bodyMeasured), 3 * 17 + 4)
let H = max(104, 56 + bodyH + 22)

let panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: W, height: H),
                    styleMask: [.borderless, .nonactivatingPanel],
                    backing: .buffered, defer: false)
panel.level = .floating
panel.isOpaque = false
panel.backgroundColor = .clear
panel.hasShadow = true
panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]

let container = NSView(frame: NSRect(x: 0, y: 0, width: W, height: H))
container.wantsLayer = true
let root = container.layer!
root.cornerRadius = 22
root.masksToBounds = true
root.borderWidth = 1
root.borderColor = NSColor(white: 1, alpha: 0.16).cgColor

let grad = CAGradientLayer()
grad.frame = container.bounds
grad.colors = [c1.cgColor, c2.cgColor]
grad.startPoint = CGPoint(x: 0, y: 1)
grad.endPoint   = CGPoint(x: 1, y: 0)
root.addSublayer(grad)

// icon
let icon = NSImageView(frame: NSRect(x: PAD, y: (H - iconSize) / 2, width: iconSize, height: iconSize))
if let img = NSImage(contentsOfFile: iconPath) {
    icon.image = img
    icon.imageScaling = .scaleProportionallyUpOrDown
}
icon.wantsLayer = true
icon.layer?.cornerRadius = 14
icon.layer?.masksToBounds = true
icon.layer?.borderWidth = 1
icon.layer?.borderColor = NSColor(white: 1, alpha: 0.25).cgColor
container.addSubview(icon)

func label(_ s: String, size: CGFloat, weight: NSFont.Weight, alpha: CGFloat,
           y: CGFloat, h: CGFloat, lines: Int = 1) -> NSTextField {
    let l = NSTextField(labelWithString: s)
    l.font = .systemFont(ofSize: size, weight: weight)
    l.textColor = NSColor(white: 1, alpha: alpha)
    l.frame = NSRect(x: tx, y: y, width: tw, height: h)
    l.maximumNumberOfLines = lines
    l.lineBreakMode = lines > 1 ? .byWordWrapping : .byTruncatingTail
    l.backgroundColor = .clear
    l.isBezeled = false
    l.isEditable = false
    return l
}

container.addSubview(label(title,    size: 17, weight: .bold,     alpha: 1.00, y: H - 36, h: 24))
container.addSubview(label(subtitle, size: 11, weight: .semibold, alpha: 0.82, y: H - 54, h: 16))
container.addSubview(label(body,     size: 12.5, weight: .regular, alpha: 0.95, y: 14, h: bodyH, lines: 3))

panel.contentView = container

// position: top-right of the active screen, stacked by slot
let screen = NSScreen.main ?? NSScreen.screens.first!
let vf = screen.visibleFrame
let finalX = vf.maxX - W - 20
let finalY = vf.maxY - H - 20 - CGFloat(slot) * (H + 12)
panel.setFrameOrigin(NSPoint(x: finalX + 24, y: finalY))  // start slightly right for slide-in
panel.alphaValue = 0
panel.orderFrontRegardless()

NSAnimationContext.runAnimationGroup { ctx in
    ctx.duration = 0.32
    ctx.timingFunction = CAMediaTimingFunction(name: .easeOut)
    panel.animator().alphaValue = 1
    panel.animator().setFrameOrigin(NSPoint(x: finalX, y: finalY))
}

DispatchQueue.main.asyncAfter(deadline: .now() + 6.5) {
    NSAnimationContext.runAnimationGroup({ ctx in
        ctx.duration = 0.5
        ctx.timingFunction = CAMediaTimingFunction(name: .easeIn)
        panel.animator().alphaValue = 0
    }, completionHandler: {
        try? FileManager.default.removeItem(atPath: slotPath)
        NSApp.terminate(nil)
    })
}

app.run()
