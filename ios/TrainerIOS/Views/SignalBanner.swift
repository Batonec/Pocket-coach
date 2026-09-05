import SwiftUI

// MARK: - Coach signal banner (the История banner)

// Coach-signal glyphs use native SF Symbols only. The server sends semantic
// tokens; CoachSignal maps every supported token to a system image and unknown
// future tokens to info.circle.
struct SignalGlyph: View {
    var systemName: String
    var color: Color
    var size: CGFloat = 17

    var body: some View {
        Image(systemName: systemName)
            .font(.system(size: size, weight: .semibold))
            .symbolRenderingMode(.monochrome)
            .foregroundStyle(color)
            .frame(width: size, height: size)
    }
}

// One coach signal banner, styled after the Claude Design mockups
// (screens/signals.jsx). History owns the visibility cap so every banner is an
// independent List row and gets the native trailing swipe action.
struct SignalBannerView: View {
    var signal: CoachSignal
    var onAction: (CoachSignal) -> Void

    var body: some View {
        banner(signal)
    }

    private struct Tone {
        var glyph: Color
        var glyphBackground: Color
        var edge: Color
    }

    private static func tone(for severity: String) -> Tone {
        switch severity {
        case "warn":
            return Tone(
                glyph: Color(red: 0.72, green: 0.48, blue: 0.07),  // #B87A12
                glyphBackground: DesignPalette.warn.opacity(0.16),
                edge: DesignPalette.warn.opacity(0.38)
            )
        case "accent":
            return Tone(
                glyph: DesignPalette.accent,
                glyphBackground: DesignPalette.accent.opacity(0.12),
                edge: DesignPalette.accent.opacity(0.24)
            )
        case "positive":
            return Tone(
                glyph: DesignPalette.ok,
                glyphBackground: DesignPalette.ok.opacity(0.15),
                edge: DesignPalette.ok.opacity(0.34)
            )
        default:  // info + unknown severities
            return Tone(
                glyph: DesignPalette.ink3,
                glyphBackground: DesignPalette.ink.opacity(0.055),
                edge: DesignPalette.ink.opacity(0.10)
            )
        }
    }

    private func banner(_ signal: CoachSignal) -> some View {
        let critical = signal.severity == "critical"
        let tone = Self.tone(for: signal.severity)
        let ink: Color = critical ? .white : DesignPalette.ink
        let sub: Color = critical ? .white.opacity(0.80) : DesignPalette.ink3
        let hasAction = signal.action != nil && signal.action?.type != "none"
        let ctaColor: Color =
            critical
            ? .white
            : (signal.severity == "info" ? DesignPalette.ink2 : tone.glyph)

        return Button {
            onAction(signal)
        } label: {
            HStack(alignment: .top, spacing: 10) {
                RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .fill(critical ? Color.white.opacity(0.20) : tone.glyphBackground)
                    .frame(width: 30, height: 30)
                    .overlay(
                        SignalGlyph(
                            systemName: signal.systemImage,
                            color: critical ? .white : tone.glyph
                        )
                    )
                    .padding(.top, 1)
                VStack(alignment: .leading, spacing: 2) {
                    Text(signal.title)
                        // Regular weight on purpose: the banner should read as
                        // a coach's line, not shout — hierarchy comes from the
                        // size and ink vs the muted body.
                        .font(.jbm(13)).tracking(-0.2)
                        .foregroundStyle(ink)
                        .multilineTextAlignment(.leading)
                        .fixedSize(horizontal: false, vertical: true)
                    if !signal.body.isEmpty {
                        Text(signal.body)
                            .font(.jbm(11.5))
                            .foregroundStyle(sub)
                            .lineSpacing(2.5)
                            .multilineTextAlignment(.leading)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let note = signal.note, !note.isEmpty {
                        Text(note)
                            .font(.jbm(10.5))
                            .italic()
                            .foregroundStyle(critical ? .white.opacity(0.62) : DesignPalette.ink4)
                            .padding(.top, 1)
                    }
                }
                Spacer(minLength: 2)
                if hasAction {
                    VStack(spacing: 3) {
                        Image(systemName: "chevron.right")
                            .font(.system(size: 12, weight: .bold))
                            .foregroundStyle(ctaColor)
                        if let label = signal.action?.label, !label.isEmpty {
                            Text(label.uppercased())
                                .font(.jbm(8.5, weight: .bold)).tracking(0.4)
                                .foregroundStyle(ctaColor)
                        }
                    }
                    // Match the natural width of the 8-letter «ПРОГРЕСС» CTA
                    // below, so shorter labels keep their chevron on the same
                    // vertical guide instead of drifting toward the edge.
                    .frame(width: 45)
                    .frame(maxHeight: .infinity, alignment: .center)
                    .padding(.leading, 2)
                }
            }
            .padding(.leading, 12)
            .padding(.trailing, 14)
            .padding(.vertical, 11)
            .frame(maxWidth: .infinity, minHeight: 58, alignment: .leading)
            .background {
                if critical {
                    RoundedRectangle(cornerRadius: 20, style: .continuous)
                        .fill(
                            LinearGradient(
                                colors: [
                                    Color(red: 0.878, green: 0.322, blue: 0.322),  // #E05252
                                    Color(red: 0.824, green: 0.247, blue: 0.247),  // #D23F3F
                                ],
                                startPoint: .top, endPoint: .bottom
                            )
                        )
                        .overlay(
                            RoundedRectangle(cornerRadius: 20, style: .continuous)
                                .stroke(Color.white.opacity(0.22), lineWidth: 0.5)
                        )
                        .shadow(color: DesignPalette.bad.opacity(0.55), radius: 15, y: 8)
                }
            }
            .modifier(SignalCardChrome(critical: critical, edge: tone.edge))
        }
        .buttonStyle(.pressable(scale: 0.985))
    }
}

// Non-critical banners sit on liquid glass with a severity-tinted hairline;
// the critical one paints its own red gradient instead.
private struct SignalCardChrome: ViewModifier {
    var critical: Bool
    var edge: Color

    func body(content: Content) -> some View {
        if critical {
            content
        } else {
            content
                .glassCard(radius: 20)
                .overlay(
                    RoundedRectangle(cornerRadius: 20, style: .continuous)
                        .stroke(edge, lineWidth: 0.5)
                )
        }
    }
}
