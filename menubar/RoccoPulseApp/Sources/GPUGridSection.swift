import SwiftUI
import RoccoPulseCore

/// Data-viz GPU block: KPI strip + 2×2 grid of ring-gauge cells.
/// Spec: design-explorations/ai-pulse-design4-final.html ("Data-Viz
/// Forward"). Rings are hand-drawn Circle.trim (no Charts dependency);
/// the sparkline is a Path over GPUHistory samples.
struct GPUGridSection: View {
    let gpus: [RoccoStatus.GPU]
    let history: GPUHistory

    private static let columns = [
        GridItem(.flexible(), spacing: 8),
        GridItem(.flexible(), spacing: 8),
    ]

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let summary = GPUSummary(gpus: gpus) {
                KPIStrip(summary: summary)
            }
            // No ScrollView wrapper: the grid is eager and width is concrete from the popover frame, so the ServicesSection collapse issue doesn't apply.
            LazyVGrid(columns: Self.columns, spacing: 8) {
                ForEach(gpus, id: \.idx) { gpu in
                    GPUCell(gpu: gpu, memSamples: history.samples(for: gpu.idx))
                }
            }
        }
    }
}

private struct KPIStrip: View {
    let summary: GPUSummary

    var body: some View {
        HStack(spacing: 8) {
            kpi(label: "AVG UTIL", value: "\(summary.avgUtilPct)%", color: .blue)
            kpi(label: "AVG MEM", value: "\(summary.avgMemPct)%", color: .purple)
            kpi(label: "MAX TEMP", value: "\(summary.maxTempC)°C",
                color: tempColor(summary.maxTempC))
        }
    }

    private func kpi(label: String, value: String, color: Color) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(label)
                .font(.system(size: 11, design: .monospaced))
                .foregroundStyle(.tertiary)
                .tracking(0.5)
            Text(value)
                .font(.system(.body, design: .monospaced).bold())
                .foregroundStyle(color)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 8)
        .padding(.vertical, 5)
        .background(
            RoundedRectangle(cornerRadius: 7, style: .continuous)
                .fill(color.opacity(0.09))
        )
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(label)
        .accessibilityValue(value)
    }
}

private struct GPUCell: View {
    let gpu: RoccoStatus.GPU
    let memSamples: [Double]

    var body: some View {
        HStack(spacing: 8) {
            RingGauge(fraction: Double(gpu.utilPct) / 100.0)
                .frame(width: 40, height: 40)
                .overlay(
                    Text("\(gpu.utilPct)")
                        .font(.system(size: 13, design: .monospaced).bold())
                )
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 4) {
                    Text("GPU \(gpu.idx)")
                        .font(.callout.bold())
                    Spacer(minLength: 0)
                    Text("\(gpu.tempC)°")
                        .font(.system(size: 12, design: .monospaced))
                        .foregroundStyle(tempColor(gpu.tempC))
                }
                Sparkline(samples: memSamples)
                    .frame(height: 12)
                Text("mem \(Int(gpu.memPctUsed))%")
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(.secondary)
            }
        }
        .padding(7)
        .background(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .fill(Color.primary.opacity(0.05))
        )
        .help("\(gpu.name) — \(gpu.utilPct)% util · \(Int(gpu.memPctUsed))% mem · \(gpu.tempC)°C")
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("GPU \(gpu.idx)")
        .accessibilityValue("util \(gpu.utilPct) percent, memory \(Int(gpu.memPctUsed)) percent, \(gpu.tempC) degrees")
    }
}

/// Utilization ring: gray track + trimmed arc, blue→orange→red as load climbs.
private struct RingGauge: View {
    let fraction: Double

    var body: some View {
        let clamped = min(1, max(0, fraction))
        ZStack {
            Circle()
                .stroke(Color.primary.opacity(0.10), lineWidth: 4)
            Circle()
                .trim(from: 0, to: clamped)
                .stroke(ringColor(clamped),
                        style: StrokeStyle(lineWidth: 4, lineCap: .round))
                .rotationEffect(.degrees(-90))
        }
    }

    private func ringColor(_ f: Double) -> Color {
        if f >= 0.95 { return .red }
        if f >= 0.80 { return .orange }
        return .blue
    }
}

/// Memory history sparkline. Empty/one-sample history draws a flat
/// baseline so cells never collapse before the second poll lands.
private struct Sparkline: View {
    let samples: [Double]

    var body: some View {
        GeometryReader { geo in
            let pts = points(in: geo.size)
            Path { p in
                guard let first = pts.first else { return }
                p.move(to: first)
                for pt in pts.dropFirst() { p.addLine(to: pt) }
            }
            .stroke(Color.purple, style: StrokeStyle(lineWidth: 1.5,
                                                     lineCap: .round,
                                                     lineJoin: .round))
        }
    }

    private func points(in size: CGSize) -> [CGPoint] {
        let values = samples.isEmpty ? [0, 0] :
            (samples.count == 1 ? [samples[0], samples[0]] : samples)
        let stepX = size.width / CGFloat(values.count - 1)
        return values.enumerated().map { i, v in
            CGPoint(x: CGFloat(i) * stepX,
                    y: size.height - CGFloat(min(1, max(0, v))) * size.height)
        }
    }
}

private func tempColor(_ c: Int) -> Color {
    if c >= 80 { return .red }
    if c >= 65 { return .orange }
    return .green
}
