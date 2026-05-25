import XCTest
@testable import RoccoPulseCore

final class ServiceRegistryTests: XCTestCase {

    func testBuiltinsIncludeTelemetrifyRoccoAgentAndVllm() {
        let ids = ServiceRegistry.builtins().map { $0.id }
        XCTAssertTrue(ids.contains("telemetrify"))
        XCTAssertTrue(ids.contains("rocco-agent"))
        XCTAssertTrue(ids.contains("vllm"))
    }

    func testMergingDiscoveredSkipsBuiltinKinds() {
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
        let merged = ServiceRegistry(services: ServiceRegistry.builtins())
            .merging(discovered: discovered)
        let ids = merged.services.map { $0.id }
        XCTAssertFalse(ids.contains("discovered-8000-vllm-1234"),
            "vllm discovered row must be deduped against the built-in")
        XCTAssertTrue(ids.contains("discovered-11434-ollama-9999"),
            "ollama isn't built-in → must be surfaced as a discovered row")
    }

    func testMergingSkipsBoringInfrastructurePorts() {
        // sshd, dns, rpcbind, prometheus → not interesting in the popover
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
        let merged = ServiceRegistry(services: ServiceRegistry.builtins())
            .merging(discovered: discovered)
        let discoveredIDs = merged.services.map { $0.id }
            .filter { $0.hasPrefix("discovered-") }
        XCTAssertEqual(discoveredIDs, [],
            "no boring infra rows should leak into the popover")
    }

    func testMergingSurfacesUnknownDiscoveredByPort() {
        // A new training job listens on a random high port we don't
        // classify — still useful to see in the popover.
        let discovered: [RoccoStatus.Service] = [
            RoccoStatus.Service(port: 37291, proc: "python", pid: 5555,
                                kind: "unknown",
                                command: "python train.py --exp=42",
                                user: "amastropaolo"),
        ]
        let merged = ServiceRegistry(services: ServiceRegistry.builtins())
            .merging(discovered: discovered)
        let ids = merged.services.map { $0.id }
        XCTAssertTrue(ids.contains("discovered-37291-python-5555"))
    }
}
