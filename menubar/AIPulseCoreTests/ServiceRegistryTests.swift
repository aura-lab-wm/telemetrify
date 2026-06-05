import XCTest
@testable import AIPulseCore

final class ServiceRegistryTests: XCTestCase {

    func testBuiltinsIncludeTelemetrifyRoccoAgentAndVllm() {
        let ids = ServiceRegistry.builtins().map { $0.id }
        XCTAssertTrue(ids.contains("telemetrify"))
        XCTAssertTrue(ids.contains("rocco-agent"))
        XCTAssertTrue(ids.contains("vllm"))
    }

    func testBuiltinsIncludeLocalOllama() {
        // Local Mac-side Ollama (Ollama.app, :11434) gets a built-in row —
        // distinct from any REMOTE ollama the rocco-agent discovers on the
        // L40S box, which still flows in via merging(discovered:).
        let builtins = ServiceRegistry.builtins()
        guard let ollama = builtins.first(where: { $0.id == "ollama-local" }) else {
            return XCTFail("ollama-local must be a built-in row")
        }
        XCTAssertEqual(ollama.kind,
            .http(url: URL(string: "http://127.0.0.1:11434/api/version")!,
                  summaryKey: "version"))
        // Down/unknown → Start launches Ollama.app via the existing
        // .openURL command (NSWorkspace.open handles app bundles).
        let expectedStart = ServiceCommand.openURL(
            URL(fileURLWithPath: "/Applications/Ollama.app"))
        XCTAssertEqual(ollama.action(for: .down)?.label, "Start")
        XCTAssertEqual(ollama.action(for: .down)?.command, expectedStart)
        XCTAssertEqual(ollama.action(for: .unknown)?.command, expectedStart)
        // Up → quick Stop (quit Ollama.app via pkill). Every service
        // shows a lifecycle icon in every state now.
        XCTAssertEqual(ollama.action(for: .up)?.command,
                       .quitLocalApp(name: "Ollama"))
    }

    func testMergingStillSurfacesRemoteOllamaDespiteLocalBuiltin() {
        // The built-in is the LOCAL Mac instance; a discovered ollama on
        // rocco is a different machine and must not be deduped away.
        let discovered: [RoccoStatus.Service] = [
            RoccoStatus.Service(port: 11434, proc: "ollama", pid: 9999,
                                kind: "ollama", command: "ollama serve",
                                user: "amastropaolo"),
        ]
        let result = ServiceRegistry(services: ServiceRegistry.builtins())
            .merging(discovered: discovered)
        let ids = result.known.services.map { $0.id }
        XCTAssertTrue(ids.contains("ollama-local"))
        XCTAssertTrue(ids.contains("discovered-11434-ollama-9999"),
            "remote ollama must still surface alongside the local built-in")
    }

    func testDiscoveredRowsNameTheHostNotTheUser() {
        // "ollama (amastropaolo)" didn't say WHICH MACHINE it runs on —
        // ambiguous next to the local "ollama (mac)" built-in. Discovered
        // rows all come from the rocco-agent snapshot, so name the host;
        // the owning user already shows in the row summary ("by …").
        let discovered: [RoccoStatus.Service] = [
            RoccoStatus.Service(port: 11434, proc: "ollama", pid: 9999,
                                kind: "ollama", command: "ollama serve",
                                user: "amastropaolo"),
        ]
        let result = ServiceRegistry(services: ServiceRegistry.builtins())
            .merging(discovered: discovered)
        let remote = result.known.services.first {
            $0.id == "discovered-11434-ollama-9999"
        }
        XCTAssertEqual(remote?.displayName, "ollama (rocco)")
        let local = result.known.services.first { $0.id == "ollama-local" }
        XCTAssertEqual(local?.displayName, "ollama (mac)")
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
