import Foundation

public enum LogLevel: String, CaseIterable, Equatable, Sendable {
    case info, warn, error
}

/// One rendered log line. `id` is the line's position in the loaded
/// tail — stable for the lifetime of one load, which is all SwiftUI's
/// ForEach/ScrollViewReader need.
public struct LogLine: Equatable, Identifiable, Sendable {
    public let id: Int
    public let text: String
    public let level: LogLevel?

    public static func parse(_ raw: String) -> [LogLine] {
        raw.split(omittingEmptySubsequences: false, whereSeparator: \.isNewline)
            .map(String.init)
            .filter { !$0.trimmingCharacters(in: .whitespaces).isEmpty }
            .enumerated()
            .map { i, text in LogLine(id: i, text: text, level: detectLevel(text)) }
    }

    private static func detectLevel(_ text: String) -> LogLevel? {
        let l = text.lowercased()
        if l.contains("error") || l.contains("traceback")
            || l.contains("exception") || l.contains("critical")
            || l.contains("failed") || l.contains("fatal") {
            return .error
        }
        if l.contains("warn") || l.contains("timeout") || l.contains("retry") {
            return .warn
        }
        if l.contains("info") || l.contains("debug") {
            return .info
        }
        return nil
    }
}

/// Level chips + search box state, applied as a pure function so the
/// view never re-implements filtering. Untagged lines always pass the
/// level filter — hiding them would make a "show errors only" view
/// drop the stack-trace body lines that follow an ERROR header.
public struct LogFilter: Equatable, Sendable {
    public var enabledLevels: Set<LogLevel> = Set(LogLevel.allCases)
    public var query: String = ""

    public init() {}

    public func apply(to lines: [LogLine]) -> [LogLine] {
        lines.filter { line in
            if let level = line.level, !enabledLevels.contains(level) {
                return false
            }
            guard !query.isEmpty else { return true }
            return line.text.localizedCaseInsensitiveContains(query)
        }
    }
}
