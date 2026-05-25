import XCTest
@testable import RoccoPulseCore

final class ServiceRegistryTests: XCTestCase {

    func testBuiltinsIncludeTelemetrifyRoccoAgentAndVllm() {
        let ids = ServiceRegistry.builtins().map { $0.id }
        XCTAssertTrue(ids.contains("telemetrify"))
        XCTAssertTrue(ids.contains("rocco-agent"))
        XCTAssertTrue(ids.contains("vllm"))
    }

    func testMergingDedupesBuiltinKinds() {
        // The on-the-wire snapshot will surface a discovered vllm row at
        // port 8000 from `ss -tlnp`. We DON'T want it duplicated next to
        // the built-in vllm row, so the merge must skip kinds we already
        // hand-coded a row for.
        let discovered: [RoccoStatus.Service] = [
            RoccoStatus.Service(port: 8000, proc: "vllm", pid: 1234,
                                kind: "vllm", command: "vllm serve …",
                                user: "amastropaolo"),
            RoccoStatus.Service(port: 11434, proc: "ollama", pid: 9999,
                                kind: "ollama", command: "ollama serve",
                                user: "amastropaolo"),
        ]
        let result = ServiceRegistry(services: ServiceRegistry.builtins())
            .merging(discovered: discovered)
        let ids = result.known.services.map { $0.id }
        XCTAssertFalse(ids.contains("discovered-8000-vllm-1234"),
            "vllm discovered row must be deduped against the built-in")
        XCTAssertTrue(ids.contains("discovered-11434-ollama-9999"),
            "ollama isn't built-in → must be surfaced as a known row")
    }

    func testMergingSkipsBoringInfrastructurePorts() {
        let discovered: [RoccoStatus.Service] = [
            RoccoStatus.Service(port: 22,   proc: "sshd", pid: 1,
                                kind: "ssh", command: "sshd", user: "root"),
            RoccoStatus.Service(port: 53,   proc: "systemd-resolve", pid: 2,
                                kind: "dns-stub", command: "", user: "root"),
            RoccoStatus.Service(port: 111,  proc: "rpcbind", pid: 3,
                                kind: "nfs-portmap", command: "", user: "root"),
            RoccoStatus.Service(port: 9100, proc: "node_exporter", pid: 4,
                                kind: "prometheus", command: "", user: "root"),
        ]
        let result = ServiceRegistry(services: ServiceRegistry.builtins())
            .merging(discovered: discovered)
        let discoveredIDs = result.known.services.map { $0.id }
            .filter { $0.hasPrefix("discovered-") }
        XCTAssertEqual(discoveredIDs, [],
            "no boring infra rows should leak into the known list")
        XCTAssertEqual(result.unknown, [],
            "boring infra is fully suppressed — not even surfaced as unknown")
    }

    /// REGRESSION: previously unknown-kind discoveries (random high
    /// ports the classifier couldn't tag) crowded out the actually-
    /// useful services with dozens of "port 60611" rows. They now
    /// land in `result.unknown` only.
    func testMergingRoutesUnknownDiscoveredToUnknownBucket() {
        let discovered: [RoccoStatus.Service] = [
            RoccoStatus.Service(port: 37291, proc: "python", pid: 5555,
                                kind: "unknown",
                                command: "python train.py --exp=42",
                                user: "amastropaolo"),
            RoccoStatus.Service(port: 60611, proc: "", pid: nil,
                                kind: "unknown", command: "", user: ""),
        ]
        let result = ServiceRegistry(services: ServiceRegistry.builtins())
            .merging(discovered: discovered)
        let ids = result.known.services.map { $0.id }
        XCTAssertFalse(ids.contains("discovered-37291-python-5555"),
            "unknown kinds must NOT appear in the known list")
        XCTAssertEqual(result.unknown.count, 2,
            "unknown discoveries must land in the unknown bucket")
        XCTAssertEqual(Set(result.unknown.map { $0.port }), [37291, 60611])
    }

    func testKnownDiscoveredKindsStillSurfaceInKnownList() {
        // ollama / jupyter are known kinds; they should appear in
        // result.known even though they aren't built-ins.
        let discovered: [RoccoStatus.Service] = [
            RoccoStatus.Service(port: 11434, proc: "ollama", pid: 9999,
                                kind: "ollama", command: "ollama serve",
                                user: "amastropaolo"),
            RoccoStatus.Service(port: 8888, proc: "python", pid: 7777,
                                kind: "jupyter",
                                command: "python -m jupyterlab",
                                user: "amastropaolo"),
        ]
        let result = ServiceRegistry(services: ServiceRegistry.builtins())
            .merging(discovered: discovered)
        let ids = result.known.services.map { $0.id }
        XCTAssertTrue(ids.contains("discovered-11434-ollama-9999"))
        XCTAssertTrue(ids.contains("discovered-8888-python-7777"))
        XCTAssertEqual(result.unknown, [],
            "known kinds shouldn't accidentally land in the unknown bucket")
    }

    func testMergingSurfacesAuraPulseAsKnownRow() {
        // :8765 is AURA Pulse's watcher-agent (root), NOT telemetrify. It
        // must render as its own known row — previously it was classified
        // "telemetrify" and silently dropped by the builtin-dedupe.
        let discovered: [RoccoStatus.Service] = [
            RoccoStatus.Service(port: 8765, proc: "", pid: nil,
                                kind: "aura-pulse", command: "", user: "root"),
        ]
        let result = ServiceRegistry(services: ServiceRegistry.builtins())
            .merging(discovered: discovered)
        let ids = result.known.services.map { $0.id }
        XCTAssertTrue(ids.contains("discovered-8765--0"),
            "aura-pulse must surface as a known row")
        XCTAssertEqual(result.unknown, [],
            "aura-pulse is a known kind, not unknown noise")
    }
}
