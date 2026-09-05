import SwiftUI

// MARK: - Glyphs

enum ExerciseGlyph: String {
    case bench, legs, lat, delts, biceps, triceps, row, fly, legext, legcurl, pullup
    case other

    static func resolve(id: Int) -> ExerciseGlyph {
        switch id {
        case 1, 18: return .bench
        case 8: return .legs
        case 9: return .lat
        case 13: return .delts
        case 11: return .biceps
        case 12: return .triceps
        case 10: return .row
        case 17: return .fly
        case 16: return .legext
        case 15: return .legcurl
        case 4: return .pullup
        default: return .other
        }
    }

    static func muscle(id: Int) -> String {
        switch id {
        case 1, 17, 18: return "Грудь"
        case 8, 15, 16: return "Ноги"
        case 9, 10, 4: return "Спина"
        case 13: return "Плечи"
        case 11, 12: return "Руки"
        default: return "Другое"
        }
    }

    static func short(name: String) -> String {
        if name.count <= 9 { return name }
        return String(name.prefix(8)) + "."
    }
}

struct ExerciseGlyphView: View {
    var glyph: ExerciseGlyph
    var size: CGFloat = 28
    var lineWidth: CGFloat = 1.6

    var body: some View {
        Canvas { ctx, _ in
            let s: CGFloat = 36
            let path = Self.path(for: glyph, in: CGRect(x: 0, y: 0, width: s, height: s))
            ctx.stroke(
                path,
                with: .color(.primary),
                style: StrokeStyle(
                    lineWidth: lineWidth * (36 / size), lineCap: .round, lineJoin: .round)
            )
        }
        .frame(width: size, height: size)
        .scaleEffect(size / 36 * (36 / size))
    }

    static func path(for glyph: ExerciseGlyph, in rect: CGRect) -> Path {
        var p = Path()
        switch glyph {
        case .bench:
            p.addRoundedRect(
                in: CGRect(x: 13, y: 9, width: 10, height: 3),
                cornerSize: CGSize(width: 1.2, height: 1.2))
            p.move(to: CGPoint(x: 18, y: 12))
            p.addLine(to: CGPoint(x: 18, y: 21))
            p.move(to: CGPoint(x: 8, y: 21))
            p.addLine(to: CGPoint(x: 28, y: 21))
            p.move(to: CGPoint(x: 11, y: 21))
            p.addLine(to: CGPoint(x: 11, y: 27))
            p.move(to: CGPoint(x: 25, y: 21))
            p.addLine(to: CGPoint(x: 25, y: 27))
            p.addEllipse(in: CGRect(x: 4, y: 14, width: 4, height: 4))
            p.addEllipse(in: CGRect(x: 28, y: 14, width: 4, height: 4))
        case .legs:
            p.move(to: CGPoint(x: 9, y: 8))
            p.addLine(to: CGPoint(x: 17, y: 19))
            p.addLine(to: CGPoint(x: 15, y: 28))
            p.move(to: CGPoint(x: 18, y: 8))
            p.addLine(to: CGPoint(x: 15, y: 19))
            p.addLine(to: CGPoint(x: 20, y: 28))
            p.move(to: CGPoint(x: 26, y: 8))
            p.addLine(to: CGPoint(x: 23, y: 17))
            p.addLine(to: CGPoint(x: 24, y: 28))
            p.addEllipse(in: CGRect(x: 3.5, y: 3.5, width: 5, height: 5))
        case .lat:
            p.move(to: CGPoint(x: 6, y: 7))
            p.addLine(to: CGPoint(x: 30, y: 7))
            for x in [10, 16, 22, 28] {
                p.move(to: CGPoint(x: CGFloat(x), y: 7))
                p.addLine(to: CGPoint(x: CGFloat(x), y: x == 16 || x == 28 ? 16 : 13))
            }
            p.move(to: CGPoint(x: 14, y: 16))
            p.addLine(to: CGPoint(x: 24, y: 16))
            p.addLine(to: CGPoint(x: 24, y: 20))
            p.addLine(to: CGPoint(x: 19, y: 28))
            p.addLine(to: CGPoint(x: 14, y: 20))
            p.closeSubpath()
        case .delts:
            p.addEllipse(in: CGRect(x: 15, y: 6, width: 6, height: 6))
            p.move(to: CGPoint(x: 11, y: 16))
            p.addLine(to: CGPoint(x: 18, y: 14))
            p.addLine(to: CGPoint(x: 25, y: 16))
            p.move(to: CGPoint(x: 9, y: 22))
            p.addLine(to: CGPoint(x: 12, y: 16))
            p.move(to: CGPoint(x: 27, y: 22))
            p.addLine(to: CGPoint(x: 24, y: 16))
            p.move(to: CGPoint(x: 11, y: 22))
            p.addLine(to: CGPoint(x: 25, y: 22))
            p.addLine(to: CGPoint(x: 23, y: 29))
            p.addLine(to: CGPoint(x: 13, y: 29))
            p.closeSubpath()
        case .biceps:
            p.move(to: CGPoint(x: 7, y: 24))
            p.addCurve(
                to: CGPoint(x: 16, y: 15),
                control1: CGPoint(x: 7, y: 18), control2: CGPoint(x: 12, y: 15))
            p.addCurve(
                to: CGPoint(x: 21, y: 9),
                control1: CGPoint(x: 20, y: 15), control2: CGPoint(x: 21, y: 12))
            p.move(to: CGPoint(x: 16, y: 15))
            p.addCurve(
                to: CGPoint(x: 23, y: 19),
                control1: CGPoint(x: 17, y: 18), control2: CGPoint(x: 20, y: 19))
            p.move(to: CGPoint(x: 21, y: 9))
            p.addLine(to: CGPoint(x: 25, y: 6))
            p.move(to: CGPoint(x: 28, y: 17))
            p.addLine(to: CGPoint(x: 30, y: 17))
        case .triceps:
            p.move(to: CGPoint(x: 28, y: 12))
            p.addCurve(
                to: CGPoint(x: 19, y: 21),
                control1: CGPoint(x: 28, y: 18), control2: CGPoint(x: 23, y: 21))
            p.addCurve(
                to: CGPoint(x: 14, y: 27),
                control1: CGPoint(x: 15, y: 21), control2: CGPoint(x: 14, y: 24))
            p.move(to: CGPoint(x: 19, y: 21))
            p.addCurve(
                to: CGPoint(x: 12, y: 17),
                control1: CGPoint(x: 18, y: 18), control2: CGPoint(x: 15, y: 17))
            p.move(to: CGPoint(x: 15, y: 27))
            p.addLine(to: CGPoint(x: 11, y: 30))
            p.move(to: CGPoint(x: 7, y: 19))
            p.addLine(to: CGPoint(x: 5, y: 19))
        case .row:
            p.move(to: CGPoint(x: 4, y: 18))
            p.addLine(to: CGPoint(x: 32, y: 18))
            p.addEllipse(in: CGRect(x: 4.5, y: 15.5, width: 5, height: 5))
            p.addEllipse(in: CGRect(x: 26.5, y: 15.5, width: 5, height: 5))
            p.move(to: CGPoint(x: 14, y: 12))
            p.addLine(to: CGPoint(x: 12, y: 18))
            p.addLine(to: CGPoint(x: 14, y: 24))
            p.move(to: CGPoint(x: 22, y: 12))
            p.addLine(to: CGPoint(x: 24, y: 18))
            p.addLine(to: CGPoint(x: 22, y: 24))
        case .fly:
            p.move(to: CGPoint(x: 18, y: 8))
            p.addLine(to: CGPoint(x: 18, y: 28))
            p.move(to: CGPoint(x: 18, y: 14))
            p.addCurve(
                to: CGPoint(x: 9, y: 12),
                control1: CGPoint(x: 15, y: 11), control2: CGPoint(x: 12, y: 11))
            p.move(to: CGPoint(x: 18, y: 14))
            p.addCurve(
                to: CGPoint(x: 27, y: 12),
                control1: CGPoint(x: 21, y: 11), control2: CGPoint(x: 24, y: 11))
            p.move(to: CGPoint(x: 18, y: 22))
            p.addCurve(
                to: CGPoint(x: 9, y: 23),
                control1: CGPoint(x: 15, y: 24), control2: CGPoint(x: 12, y: 24))
            p.move(to: CGPoint(x: 18, y: 22))
            p.addCurve(
                to: CGPoint(x: 27, y: 23),
                control1: CGPoint(x: 21, y: 24), control2: CGPoint(x: 24, y: 24))
        case .legext:
            p.move(to: CGPoint(x: 8, y: 26))
            p.addLine(to: CGPoint(x: 14, y: 26))
            p.addLine(to: CGPoint(x: 14, y: 18))
            p.addLine(to: CGPoint(x: 24, y: 18))
            p.addLine(to: CGPoint(x: 28, y: 26))
            p.addEllipse(in: CGRect(x: 19, y: 11, width: 6, height: 6))
            p.move(to: CGPoint(x: 14, y: 18))
            p.addLine(to: CGPoint(x: 11, y: 14))
        case .legcurl:
            p.move(to: CGPoint(x: 8, y: 12))
            p.addLine(to: CGPoint(x: 22, y: 12))
            p.addLine(to: CGPoint(x: 22, y: 20))
            p.addLine(to: CGPoint(x: 28, y: 20))
            p.addLine(to: CGPoint(x: 24, y: 26))
            p.addEllipse(in: CGRect(x: 19, y: 23, width: 6, height: 6))
            p.move(to: CGPoint(x: 22, y: 20))
            p.addLine(to: CGPoint(x: 22, y: 23))
        case .pullup:
            p.move(to: CGPoint(x: 5, y: 7))
            p.addLine(to: CGPoint(x: 31, y: 7))
            p.move(to: CGPoint(x: 11, y: 7))
            p.addLine(to: CGPoint(x: 11, y: 11))
            p.move(to: CGPoint(x: 25, y: 7))
            p.addLine(to: CGPoint(x: 25, y: 11))
            p.addEllipse(in: CGRect(x: 15.5, y: 11.5, width: 5, height: 5))
            p.move(to: CGPoint(x: 18, y: 16))
            p.addLine(to: CGPoint(x: 18, y: 24))
            p.move(to: CGPoint(x: 14, y: 19))
            p.addLine(to: CGPoint(x: 18, y: 17))
            p.addLine(to: CGPoint(x: 22, y: 19))
            p.move(to: CGPoint(x: 14, y: 27))
            p.addLine(to: CGPoint(x: 18, y: 24))
            p.addLine(to: CGPoint(x: 22, y: 27))
        case .other:
            p.addEllipse(in: CGRect(x: 8, y: 8, width: 20, height: 20))
        }
        // Path is drawn in canvas; scale to bounds is implicit when 36x36.
        return p
    }
}

// Wrapper that scales the 36-unit canvas to the requested size.
struct GlyphIcon: View {
    var glyph: ExerciseGlyph
    var size: CGFloat
    var lineWidth: CGFloat = 1.6
    var tint: Color = DesignPalette.ink

    var body: some View {
        let scale = size / 36
        Canvas { ctx, _ in
            ctx.scaleBy(x: scale, y: scale)
            ctx.stroke(
                ExerciseGlyphView.path(for: glyph, in: CGRect(x: 0, y: 0, width: 36, height: 36)),
                with: .color(tint),
                style: StrokeStyle(lineWidth: lineWidth, lineCap: .round, lineJoin: .round)
            )
        }
        .frame(width: size, height: size)
    }
}
