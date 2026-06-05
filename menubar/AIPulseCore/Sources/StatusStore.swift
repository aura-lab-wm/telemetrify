import Foundation
import Combine

/// Watchdog cadence for the fallback poll loop. The footer picker that
/// exposed this died with the live stream — the labels/CaseIterable went
/// with it. Values are seconds.
public enum PollInterval: Int, Sendable {
    case fast = 5
    case normal = 15
    case slow = 60
}

/// Owns the timer-driven `SSHProbe` poll loop, publishes the latest
/// snapshot, and persists it to `Application Support/ai-pulse/last.json`
/// so the UI can keep showing "last seen 12 min ago" across launches.
@MainActor
public final class StatusStore: ObservableObject {
    @Published public private(set) var snapshot: RoccoStatus?
    @Published public private(set) var gpuHistory = GPUHistory()
    /// When the last LIVE frame arrived. internal(set) so tests can pin it.
    @Published public internal(set) var lastStreamFrameAt: Date?
    @Published public private(set) var lastError: String?
    @Published public private(set) var lastErrorKind: SSHProbeErrorKind?
    @Published public private(set) var lastFetchedAt: Date?
    @Published public var pollInterval: PollInterval = .normal {
        didSet { if oldValue != pollInterval { restartTimer() } }
    }

    private let probe: SSHProbe
    private let probeQueue = DispatchQueue(label: "dev.mastropaolo.ai-pulse.probe", qos: .utility)
    private var timer: Timer?
    private var streamer: StatusStreamer?
    private let persistenceURL: URL

    public init(probe: SSHProbe = SSHProbe(), persistenceURL: URL? = nil) {
        self.probe = probe
        self.persistenceURL = persistenceURL ?? StatusStore.defaultPersistenceURL()
        loadCachedSnapshot()
    }

    public func start() {
        restartTimer()
        Task { await refresh() }
        // Idempotent: the popover's .task calls start() on EVERY open —
        // the watchdog timer restart is harmless, but the live stream
        // must only ever exist once or each open would leak an ssh.
        if streamer == nil { startStreamer() }
    }

    public func stop() {
        timer?.invalidate()
        timer = nil
        streamer?.stop()
        streamer = nil
    }

    /// Live channel: snapshots arrive moments after rocco-agent writes
    /// them instead of on the next poll tick. The poll timer above stays
    /// as the watchdog — if the stream drops (sleep/wake, network) the
    /// app degrades to exactly the old polling behavior while the
    /// streamer reconnects.
    private func startStreamer() {
        let s = StatusStreamer()
        s.onStatus = { [weak self] status in
            Task { @MainActor in self?.applyStreamed(status) }
        }
        s.start()
        streamer = s
    }

    private func applyStreamed(_ status: RoccoStatus) {
        lastFetchedAt = Date()
        lastStreamFrameAt = lastFetchedAt
        snapshot = status
        gpuHistory.append(gpus: status.gpus)
        lastError = nil
        lastErrorKind = nil
        persist(status: status)
    }

    public func refresh() async {
        await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
            probeQueue.async { [probe] in
                let result = Result { try probe.fetchStatus() }
                Task { @MainActor in
                    self.apply(result: result)
                    continuation.resume()
                }
            }
        }
    }

    private func apply(result: Result<RoccoStatus, Error>) {
        lastFetchedAt = Date()
        switch result {
        case .success(let status):
            self.snapshot = status
            self.gpuHistory.append(gpus: status.gpus)
            self.lastError = nil
            self.lastErrorKind = nil
            persist(status: status)
        case .failure(let error):
            self.lastError = error.localizedDescription
            // Only an SSHProbeError can claim a specific kind. Anything
            // else (ProcessLauncher timeout / spawn failure / cancellation
            // / sandbox denial) is .unknown — we must NOT label it as
            // .sshFailed or the popover gives the wrong recovery hint
            // ("try ssh-add" for problems that aren't SSH at all).
            self.lastErrorKind = (error as? SSHProbeError)?.kind ?? .unknown
        }
    }

    /// True while the push stream is delivering: a frame landed within
    /// the last 10s (agent writes every 2s; 10s = several missed beats).
    /// When false the app is on the polling watchdog.
    public func isLive(now: Date = Date()) -> Bool {
        guard let t = lastStreamFrameAt else { return false }
        return now.timeIntervalSince(t) < 10
    }

    private func restartTimer() {
        timer?.invalidate()
        let interval = TimeInterval(pollInterval.rawValue)
        let t = Timer(timeInterval: interval, repeats: true) { [weak self] _ in
            guard let self else { return }
            Task { await self.refresh() }
        }
        RunLoop.main.add(t, forMode: .common)
        self.timer = t
    }

    // MARK: - Persistence

    private static func defaultPersistenceURL() -> URL {
        let base = try? FileManager.default.url(
            for: .applicationSupportDirectory,
            in: .userDomainMask,
            appropriateFor: nil,
            create: true
        )
        let root = base ?? URL(fileURLWithPath: NSTemporaryDirectory())
        let dir = root.appendingPathComponent("ai-pulse", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let url = dir.appendingPathComponent("last.json")
        // One-time rebrand migration: the pre-rename app cached under
        // rocco-pulse/ — move that snapshot over so "last seen" survives.
        migrateLegacyCache(
            legacy: root.appendingPathComponent("rocco-pulse/last.json"),
            current: url)
        return url
    }

    /// Move the legacy cache to its new home exactly once. A populated
    /// `current` is never overwritten; failures are silent (cache is
    /// best-effort by design).
    nonisolated static func migrateLegacyCache(legacy: URL, current: URL) {
        let fm = FileManager.default
        guard fm.fileExists(atPath: legacy.path),
              !fm.fileExists(atPath: current.path) else { return }
        try? fm.moveItem(at: legacy, to: current)
    }

    private func persist(status: RoccoStatus) {
        do {
            let data = try JSONEncoder().encode(status)
            try data.write(to: persistenceURL, options: .atomic)
        } catch {
            // Persistence is best-effort — surface to logs but never to the UI.
            NSLog("ai-pulse: failed to persist snapshot: \(error)")
        }
    }

    private func loadCachedSnapshot() {
        guard
            FileManager.default.fileExists(atPath: persistenceURL.path),
            let data = try? Data(contentsOf: persistenceURL),
            let cached = try? RoccoStatus.decode(from: data)
        else { return }
        self.snapshot = cached
    }
}
