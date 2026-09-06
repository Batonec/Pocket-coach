import SwiftUI

// MARK: - Typography helpers
//
// All-mono direction: JetBrains Mono everywhere, matching the design mockups.
// The TTFs are bundled under TrainerIOS/Resources and registered through
// Info.plist's UIAppFonts. AppearanceFonts.bootstrap() runs at app launch and
// logs an assertion if any PS name is missing so we catch a broken bundle.

enum AppFont {
    static let regular = "JetBrainsMono-Regular"
    static let medium = "JetBrainsMono-Medium"
    static let semibold = "JetBrainsMono-SemiBold"
    static let bold = "JetBrainsMono-Bold"

    static func name(for weight: Font.Weight) -> String {
        switch weight {
        case .ultraLight, .thin, .light, .regular: return regular
        case .medium: return medium
        case .semibold: return semibold
        case .bold, .heavy, .black: return bold
        default: return regular
        }
    }
}

extension Font {
    static func jbm(_ size: CGFloat, weight: Font.Weight = .regular) -> Font {
        .custom(AppFont.name(for: weight), size: size)
    }
}

extension View {
    func display(size: CGFloat, weight: Font.Weight = .bold) -> some View {
        font(.jbm(size, weight: weight))
            .tracking(-size * 0.04)
            .monospacedDigit()
    }

    func mono(_ size: CGFloat, weight: Font.Weight = .regular) -> some View {
        font(.jbm(size, weight: weight))
            .monospacedDigit()
    }

    /// Цвет — параметр, а не второй `.foregroundStyle` поверх: у Text выигрывает
    /// БЛИЖАЙШИЙ к нему стиль, поэтому `.tLabel().foregroundStyle(accent)`
    /// молча оставляет надпись серой.
    func tLabel(size: CGFloat = 10.5, color: Color = DesignPalette.ink3) -> some View {
        font(.jbm(size, weight: .semibold))
            .tracking(0.6)
            .textCase(.uppercase)
            .foregroundStyle(color)
    }
}

// MARK: - Pieces shared across screens

struct TopPills: View {
    struct Pill: Identifiable {
        let id = UUID()
        var icon: AnyView?
        var label: String
        var tone: Tone = .neutral
        var action: (() -> Void)?

        enum Tone {
            case neutral
            case accent
        }
    }

    var pills: [Pill]
    var trailing: AnyView?

    var body: some View {
        HStack(spacing: 6) {
            ForEach(pills) { pill in
                HStack(spacing: 6) {
                    if let icon = pill.icon { icon }
                    Text(pill.label)
                }
                .font(.jbm(13.5, weight: .semibold))
                .foregroundStyle(pill.tone == .accent ? DesignPalette.accent : DesignPalette.ink2)
                .padding(.horizontal, 12)
                .padding(.vertical, 7)
                .chipBackground()
            }

            Spacer(minLength: 0)

            if let trailing { trailing }
        }
    }
}

struct TopTitle: View {
    var sub: String?
    var title: String

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            if let sub {
                Text(sub.uppercased())
                    .font(.jbm(13, weight: .semibold))
                    .tracking(0.4)
                    .foregroundStyle(DesignPalette.ink.opacity(0.5))
            }
            Text(title)
                .display(size: 34, weight: .heavy)
                .foregroundStyle(DesignPalette.ink)
        }
    }
}
