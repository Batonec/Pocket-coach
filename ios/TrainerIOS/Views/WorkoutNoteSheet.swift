import SwiftUI

// MARK: - Заметка к тренировке

/// Полоска после сохранения тренировки. Завершение остаётся одним нажатием:
/// тренировка уже записана, заметку предлагает полоска, которая сама уедет, —
/// её можно просто не заметить.
struct FinishWorkoutStrip: View {
    var summary: String
    var onNote: () -> Void

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: "checkmark")
                .font(.jbm(12, weight: .heavy))
                .foregroundStyle(DesignPalette.ok)
                .frame(width: 26, height: 26)
                .background(DesignPalette.ok.opacity(0.15), in: Circle())

            VStack(alignment: .leading, spacing: 1) {
                Text("Тренировка записана")
                    .font(.jbm(13.5, weight: .bold))
                    .tracking(-0.2)
                    .foregroundStyle(DesignPalette.ink)
                Text(summary)
                    .mono(11)
                    .foregroundStyle(DesignPalette.ink3)
            }
            .frame(maxWidth: .infinity, alignment: .leading)

            Button(action: onNote) {
                HStack(spacing: 6) {
                    Image(systemName: "pencil")
                        .font(.jbm(11, weight: .semibold))
                    Text("Заметка")
                        .font(.jbm(12, weight: .bold))
                }
                .foregroundStyle(DesignPalette.ink)
                .padding(.horizontal, 13)
                .padding(.vertical, 8)
                .chipBackground()
            }
            .buttonStyle(.pressable(scale: 0.96))
        }
        .padding(EdgeInsets(top: 10, leading: 14, bottom: 10, trailing: 10))
        .glassCard(radius: 20, thick: true)
        .shadow(color: DesignPalette.ink.opacity(0.18), radius: 14, y: 8)
    }
}

/// Открывается только если полоску тронули — либо поздним входом со свайпа
/// карточки в «Истории».
struct WorkoutNoteSheet: View {
    var workout: Workout

    @EnvironmentObject private var store: TrainerStore
    @Environment(\.dismiss) private var dismiss
    @State private var text: String
    @State private var isSaving = false

    init(workout: Workout) {
        self.workout = workout
        _text = State(initialValue: workout.data.notes ?? "")
    }

    private var title: String {
        let date = DateTools.short(workout.workoutDate)
        if let focus = workout.data.focus?.nilIfBlank {
            return "\(date) · \(focus)"
        }
        return "\(date) · \(DateTools.weekday(workout.workoutDate))"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Заметка к тренировке")
                        .tLabel()
                    Text(title)
                        .font(.jbm(17, weight: .bold))
                        .tracking(-0.3)
                        .foregroundStyle(DesignPalette.ink)
                }

                Spacer()

                Button {
                    dismiss()
                } label: {
                    Image(systemName: "xmark")
                        .font(.jbm(11, weight: .bold))
                        .foregroundStyle(DesignPalette.ink3)
                        .frame(width: 34, height: 34)
                        .chipBackground()
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Закрыть")
            }
            .padding(.bottom, 12)

            MonoTextArea(placeholder: "Как прошло, что мешало", text: $text)

            Button {
                Task {
                    guard let id = workout.id else { return }
                    isSaving = true
                    let saved = await store.saveWorkoutNote(workoutID: id, text: text)
                    isSaving = false
                    if saved { dismiss() }
                }
            } label: {
                HStack(spacing: 8) {
                    if isSaving {
                        ProgressView().tint(.white)
                    }
                    Text("Сохранить")
                        .font(.jbm(16, weight: .bold))
                        .tracking(-0.2)
                }
                .foregroundStyle(.white)
                .frame(maxWidth: .infinity)
                .frame(height: 54)
                .background(
                    DesignPalette.ink,
                    in: RoundedRectangle(cornerRadius: 27, style: .continuous)
                )
                .shadow(color: DesignPalette.ink.opacity(0.30), radius: 12, y: 6)
            }
            .buttonStyle(.pressable(scale: 0.97))
            .padding(.top, 12)
            .disabled(isSaving)

            Text("Уедет тренеру вместе с весами")
                .font(.jbm(11))
                .foregroundStyle(DesignPalette.ink4)
                .frame(maxWidth: .infinity)
                .padding(.top, 9)

            Spacer(minLength: 0)
        }
        .padding(.horizontal, 16)
        .padding(.top, 16)
        .padding(.bottom, 26)
        .background(WarmWallpaper())
        .presentationDetents([.height(320), .large])
        .presentationDragIndicator(.visible)
        .interactiveDismissDisabled(isSaving)
    }
}
