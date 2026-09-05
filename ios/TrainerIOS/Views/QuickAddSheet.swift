import SwiftUI

// MARK: - Quick Add Sheet

struct SetEditorState: Identifiable, Equatable {
    let id = UUID()
    var exerciseID: Int
    var exerciseName: String
    var setIndex: Int?
    var reps: Int
    var weight: Double
    var effort: SetEffort?
    var previousLabel: String
    var targetLabel: String
    var currentSetIndex: Int
    /// Заметка к подходу: «канат вместо прямой ручки». Вес сопоставим только
    /// внутри одной постановки, и объясняет её этот текст, а не число.
    var notes: String = ""
}

struct QuickAddSheet: View {
    @Environment(\.dismiss) private var dismiss
    @State private var state: SetEditorState
    @State private var isNoteExpanded: Bool
    @State private var detent: PresentationDetent
    var onApply: (SetEditorState) -> Void

    private static let compactDetent = PresentationDetent.height(520)

    init(state: SetEditorState, onApply: @escaping (SetEditorState) -> Void) {
        _state = State(initialValue: state)
        _isNoteExpanded = State(initialValue: !state.notes.isEmpty)
        _detent = State(
            initialValue: state.notes.isEmpty ? Self.compactDetent : .large
        )
        self.onApply = onApply
    }

    var body: some View {
        ZStack {
            WarmWallpaper()

            VStack(spacing: 0) {
                exerciseHeader

                VStack(spacing: 0) {
                    Text("Вес, кг")
                        .tLabel()
                        .padding(.top, 6)

                    Stepper(
                        value: TrainerLogic.formatWeight(state.weight),
                        suffix: "",
                        big: true,
                        onMinus: { state.weight = max(0, state.weight - 2.5) },
                        onPlus: { state.weight += 2.5 }
                    )

                    Rectangle()
                        .fill(DesignPalette.sep)
                        .frame(height: 0.5)
                        .padding(.horizontal, 8)
                        .padding(.vertical, 4)

                    Text("Повторений")
                        .tLabel()
                        .padding(.top, 4)

                    Stepper(
                        value: "\(state.reps)",
                        suffix: "",
                        big: false,
                        onMinus: { state.reps = max(1, state.reps - 1) },
                        onPlus: { state.reps += 1 }
                    )

                    Rectangle()
                        .fill(DesignPalette.sep)
                        .frame(height: 0.5)
                        .padding(.horizontal, 8)
                        .padding(.vertical, 8)

                    Text("Как ощущения?")
                        .tLabel()
                        .padding(.bottom, 4)

                    HStack(spacing: 14) {
                        ForEach(SetEffort.allCases) { effort in
                            Button {
                                state.effort = state.effort == effort ? nil : effort
                            } label: {
                                EffortBubble(
                                    effort: effort, size: 60, selected: state.effort == effort)
                            }
                            .buttonStyle(.plain)
                        }
                    }
                    .padding(.top, 4)

                    noteBlock
                        .padding(.top, 16)
                }
                .padding(.horizontal, 24)
                .padding(.top, 8)

                Spacer(minLength: 12)

                Button {
                    onApply(state)
                    dismiss()
                } label: {
                    Text("Сохранить сет")
                        .font(.jbm(17, weight: .heavy))
                        .foregroundStyle(.white)
                        .frame(maxWidth: .infinity)
                        .frame(height: 56)
                        .background(
                            DesignPalette.ink,
                            in: RoundedRectangle(cornerRadius: 28, style: .continuous)
                        )
                        .shadow(color: DesignPalette.ink.opacity(0.35), radius: 18, y: 8)
                }
                .buttonStyle(.pressable(scale: 0.96))
                .padding(.horizontal, 24)
                .padding(.bottom, 28)
            }
            .padding(.top, 8)
        }
        // Раскрытая заметка означает клавиатуру поверх CTA, поэтому шит
        // переезжает на полный детент, а не сжимает содержимое.
        .presentationDetents([Self.compactDetent, .large], selection: $detent)
        .presentationDragIndicator(.visible)
    }

    /// Свёрнутая заметка — пунктирная строка: она не занимает место в колонке
    /// шита и не мешает основному сценарию «вес, повторы, ощущения».
    @ViewBuilder
    private var noteBlock: some View {
        if isNoteExpanded {
            MonoTextArea(
                placeholder: "канат, узкий хват, другая скамья",
                text: $state.notes,
                minHeight: 44,
                radius: 16,
                label: "Заметка к подходу"
            )
        } else {
            Button {
                expandNote()
            } label: {
                HStack(spacing: 8) {
                    Image(systemName: "pencil")
                        .font(.jbm(11, weight: .semibold))
                    Text("канат, узкий хват, другая скамья")
                        .font(.jbm(12.5, weight: .semibold))
                        .lineLimit(1)
                }
                .foregroundStyle(DesignPalette.ink3)
                .frame(maxWidth: .infinity)
                .frame(height: 42)
                .background(
                    RoundedRectangle(cornerRadius: 14, style: .continuous)
                        .strokeBorder(style: StrokeStyle(lineWidth: 1, dash: [4, 4]))
                        .foregroundStyle(DesignPalette.ink.opacity(0.16))
                )
            }
            .buttonStyle(.pressable(scale: 0.98))
        }
    }

    private func expandNote() {
        withAnimation(.spring(response: 0.3, dampingFraction: 0.86)) {
            isNoteExpanded = true
            detent = .large
        }
    }

    private var exerciseHeader: some View {
        VStack(spacing: 0) {
            HStack(spacing: 10) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(state.exerciseName)
                        .font(.jbm(16, weight: .heavy))
                        .tracking(-0.3)
                        .foregroundStyle(DesignPalette.ink)
                        .lineLimit(1)
                    if !state.previousLabel.isEmpty && state.previousLabel != "—" {
                        HStack(spacing: 4) {
                            Text(state.previousLabel)
                                .mono(12)
                                .foregroundStyle(DesignPalette.ink3)
                            Text("→ \(state.targetLabel)")
                                .mono(12, weight: .heavy)
                                .foregroundStyle(DesignPalette.accent)
                        }
                        .lineLimit(1)
                    }
                }

                Spacer()

                Text("СЕТ \(state.currentSetIndex)")
                    .font(.jbm(10.5, weight: .heavy))
                    .tracking(0.4)
                    .foregroundStyle(DesignPalette.ink3)
                    .padding(.horizontal, 9)
                    .padding(.vertical, 4)
                    .chipBackground()

                // Второй вход в заметку: из шапки видно, есть она уже или нет.
                Button {
                    if isNoteExpanded {
                        withAnimation(.spring(response: 0.3, dampingFraction: 0.86)) {
                            isNoteExpanded = false
                        }
                    } else {
                        expandNote()
                    }
                } label: {
                    Image(systemName: "pencil")
                        .font(.jbm(12, weight: .semibold))
                        .foregroundStyle(
                            state.notes.isEmpty ? DesignPalette.ink2 : Color.white
                        )
                        .frame(width: 30, height: 30)
                        .background(
                            Circle()
                                .fill(
                                    state.notes.isEmpty
                                        ? DesignPalette.ink.opacity(0.05)
                                        : DesignPalette.ink
                                )
                        )
                }
                .buttonStyle(.pressable(scale: 0.92))
                .accessibilityLabel("Заметка к подходу")
            }
            .padding(.horizontal, 24)
            .padding(.top, 12)
            .padding(.bottom, 14)

            Rectangle()
                .fill(DesignPalette.sep)
                .frame(height: 0.5)
                .padding(.horizontal, 16)
        }
    }
}

private struct Stepper: View {
    var value: String
    var suffix: String
    var big: Bool
    var onMinus: () -> Void
    var onPlus: () -> Void

    var body: some View {
        HStack(spacing: 14) {
            HoldRepeatButton(action: onMinus) {
                ZStack {
                    Circle().fill(Color.black.opacity(0.06))
                    Image(systemName: "minus")
                        .font(.jbm(18, weight: .heavy))
                        .foregroundStyle(DesignPalette.ink)
                }
                .frame(width: 62, height: 62)
            }

            HStack(alignment: .firstTextBaseline, spacing: 4) {
                Text(value)
                    .display(size: big ? 72 : 56, weight: .heavy)
                    .foregroundStyle(DesignPalette.ink)
                Text(suffix)
                    .font(.jbm(18, weight: .semibold))
                    .foregroundStyle(DesignPalette.ink3)
            }
            .frame(maxWidth: .infinity)

            HoldRepeatButton(action: onPlus) {
                ZStack {
                    Circle().fill(DesignPalette.accent)
                    Image(systemName: "plus")
                        .font(.jbm(22, weight: .bold))
                        .foregroundStyle(.white)
                }
                .frame(width: 62, height: 62)
                .shadow(color: DesignPalette.accent.opacity(0.35), radius: 16, y: 6)
            }
        }
        .padding(.vertical, 6)
    }
}
