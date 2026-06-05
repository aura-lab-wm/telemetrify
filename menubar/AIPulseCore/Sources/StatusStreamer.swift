import Foundation

/// Reassembles newline-delimited frames from arbitrary read chunks —
/// ssh hands us whatever the kernel buffered, which can be half a JSON
/// frame or three of them.
struct LineAccumulator {
    private var buffer = Data()

    mutating func ingest(_ chunk: Data) -> [String] {
        buffer.append(chunk)
        var lines: [String] = []
        while let nl = buffer.firstIndex(of: UInt8(ascii: "\n")) {
            let frame = buffer[buffer.startIndex..<nl]
            buffer.removeSubrange(buffer.startIndex...nl)
            if let s = String(data: frame, encoding: .utf8)?
                .trimmingCharacters(in: .whitespaces),
               !s.isEmpty {
                lines.append(s)
            }
        }
        return lines
    }
}

/// LIVE status channel: one long-lived `ssh <host>` whose REMOTE side
/// does all the watching (mtime poll every 0.2s — backend cost, per the
/// operator's call) and pushes a compact one-line JSON frame whenever
/// rocco-agent rewrites the status file. The client's per-frame work is
/// reassemble + decode (~sub-ms for the ~11KB snapshot), off the main
/// thread. The 15s SSHProbe poll stays as the watchdog: if this stream
/// dies (sleep/wake, network), the app silently degrades to polling
/// while the stream reconnects with capped backoff.
public final class StatusStreamer: @unchecked Sendable {
    /// Fired on the streamer's internal queue for every NEW snapshot
    /// (identical consecutive frames are deduped). Hop to MainActor
    /// before touching UI state.
    public var onStatus: (@Sendable (RoccoStatus) -> Void)?

    private let host: String
    private let queue = DispatchQueue(label: "dev.mastropaolo.ai-pulse.stream", qos: .utility)
    private var process: Process?
    private var accumulator = LineAccumulator()
    private var lastFrame: String?
    private var stopped = false
    private var backoff: TimeInterval = 1

    public init(host: String = "rocco") {
        self.host = host
    }

    /// The remote watcher: pure POSIX sh + GNU stat (rocco is Linux).
    /// `tr -d '\n'` compacts the (possibly pretty-printed) JSON to one
    /// frame per line — escaped \n inside strings are untouched. Kept
    /// as ONE argv element; ssh joins args into a single remote command.
    static func sshArguments(host: String) -> [String] {
        let watcher = "l=; while :; do m=$(stat -c %Y ~/.cache/rocco-status.json 2>/dev/null); "
            + "if [ -n \"$m\" ] && [ \"$m\" != \"$l\" ]; then l=$m; "
            + "tr -d '\\n' < ~/.cache/rocco-status.json; echo; fi; sleep 0.2; done"
        return [
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=4",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=2",
            "-T",          // no tty — line-buffered pipe semantics
            "-C",          // compress: the 11KB frame ships as ~1.5KB
            host,
            watcher,
        ]
    }

    public func start() {
        queue.async { [weak self] in
            guard let self, !self.stopped else { return }
            self.launch()
        }
    }

    public func stop() {
        queue.async { [weak self] in
            guard let self else { return }
            self.stopped = true
            self.process?.terminate()
            self.process = nil
        }
    }

    /// Decode + dedupe one read chunk. Internal (not private) so tests
    /// can drive the exact byte patterns ssh produces.
    func ingest(_ chunk: Data) {
        for frame in accumulator.ingest(chunk) {
            guard frame != lastFrame else { continue }
            guard let status = try? RoccoStatus.decode(from: Data(frame.utf8)) else {
                continue   // ssh banners / motd noise — never fatal
            }
            lastFrame = frame
            backoff = 1    // healthy stream resets the reconnect clock
            onStatus?(status)
        }
    }

    // MARK: - process lifecycle (queue-confined)

    private func launch() {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/ssh")
        p.arguments = Self.sshArguments(host: host)
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = FileHandle.nullDevice

        pipe.fileHandleForReading.readabilityHandler = { [weak self] fh in
            let data = fh.availableData
            guard !data.isEmpty, let self else { return }
            self.queue.async { self.ingest(data) }
        }
        p.terminationHandler = { [weak self] _ in
            pipe.fileHandleForReading.readabilityHandler = nil
            self?.scheduleReconnect()
        }

        do {
            try p.run()
            process = p
        } catch {
            scheduleReconnect()
        }
    }

    private func scheduleReconnect() {
        queue.asyncAfter(deadline: .now() + backoff) { [weak self] in
            guard let self, !self.stopped else { return }
            self.backoff = min(self.backoff * 2, 30)
            self.launch()
        }
    }
}
