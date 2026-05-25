import Foundation
import Combine

public enum PollInterval: Int, CaseIterable, Identifiable, Sendable {
    case fast = 5
    case normal = 15
    case slow = 60

    public var id: Int { rawValue }
    public var label: String {
        switch self {
        case .fast: return "5s"
        case .normal: return "15s (default)"
        case .slow: return "60s"
        }
    }
}

/// Owns the timer-driven `SSHProbe` poll loop, publishes the latest
/// snapshot, and persists it to `Application Support/rocco-pulse/last.json`
/// so the UI can keep showing "last seen 12 min ago" across launches.
@MainActor
public final class StatusStore: ObservableObject {
    @Published public private(set) var snapshot: RoccoStatus?
    @Published public private(set) var lastError: String?
    @Published public private(set) var lastErrorKind: SSHProbeErrorKind?
    @Published public private(set) var lastFetchedAt: Date?
    @Published public var pollInterval: PollInterval = .normal {
        didSet { if oldValue != pollInterval { restartTimer() } }
    }

    private let probe: SSHProbe
    private let probeQueue = DispatchQueue(label: "dev.mastropaolo.rocco-pulse.probe", qos: .utility)
    private var timer: Timer?
    private let persistenceURL: URL

    public init(probe: SSHProbe = SSHProbe(), persistenceURL: URL? = nil) {
        self.probe = probe
        self.persistenceURL = persistenceURL ?? StatusStore.defaultPersistenceURL()
        loadCachedSnapshot()
    }

    public func start() {
        restartTimer()
        Task { await refresh() }
    }

    public func stop() {
        timer?.invalidate()
        timer = nil
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
        let dir = (base ?? URL(fileURLWithPath: NSTemporaryDirectory()))
            .appendingPathComponent("rocco-pulse", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir.appendingPathComponent("last.json")
    }

    private func persist(status: RoccoStatus) {
        do {
            let data = try JSONEncoder().encode(status)
            try data.write(to: persistenceURL, options: .atomic)
        } catch {
            // Persistence is best-effort — surface to logs but never to the UI.
            NSLog("rocco-pulse: failed to persist snapshot: \(error)")
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
