import SwiftUI

// MARK: - Coach recommendation card ("Совет тренера")

// CoachCard now renders ONLY the transient states (pending / failed / none).
// The ready recommendation is no longer a separate card — its content lives in
// the "План от тренера" section: per-exercise notes on each plan card, and the
// rationale behind a "?" in the section header. So the ready branch is empty.
struct CoachCard: View {
    @EnvironmentObject private var store: TrainerStore

    var body: some View {
        if let rec = store.recommendation {
            card(for: rec)
        }
    }

    @ViewBuilder
    private func card(for rec: RecommendationResponse) -> some View {
        let status = rec.status ?? "none"
        let busy = store.isRefreshingRecommendation
        if busy || status == "pending" {
            pendingCard(hasPreviousPlan: rec.recommendation != nil)
        } else if status == "failed" {
            failedCard(rec)
        } else if rec.recommendation != nil {
            EmptyView()  // ready → shown inline in the plan section
        } else {
            noneCard
        }
    }
    // MARK: pending

    private func pendingCard(hasPreviousPlan: Bool) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            header(basedOn: nil)
            HStack(spacing: 11) {
                ProgressView().tint(DesignPalette.accent)
                VStack(alignment: .leading, spacing: 1) {
                    Text(hasPreviousPlan ? "Обновляю план…" : "ИИ составляет план…")
                        .font(.jbm(13.5, weight: .bold))
                        .foregroundStyle(DesignPalette.ink)
                    Text(hasPreviousPlan ? "старый план временно скрыт" : "обычно 15–20 секунд")
                        .font(.jbm(10.5))
                        .foregroundStyle(DesignPalette.ink3)
                }
            }
            .padding(.top, 16)
        }
        .padding(16)
        .liquidGlass(radius: 26)
    }

    // MARK: none / empty

    private var noneCard: some View {
        VStack(alignment: .leading, spacing: 0) {
            header(basedOn: nil)
            VStack(spacing: 0) {
                ZStack {
                    Circle().fill(DesignPalette.accent.opacity(0.12)).frame(width: 52, height: 52)
                        .overlay(
                            Circle().stroke(DesignPalette.accent.opacity(0.20), lineWidth: 0.5))
                    Image(systemName: "sparkles").font(.system(size: 22)).foregroundStyle(
                        DesignPalette.accent)
                }
                .padding(.bottom, 14)
                Text("Совет ещё не сгенерирован")
                    .font(.jbm(15, weight: .bold)).tracking(-0.3)
                    .foregroundStyle(DesignPalette.ink).multilineTextAlignment(.center)
                Text(
                    "Построю план следующей тренировки по твоей истории — с весами, повторами и обоснованием."
                )
                .font(.jbm(12)).foregroundStyle(DesignPalette.ink3)
                .multilineTextAlignment(.center).lineSpacing(3)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.top, 6)
                Button {
                    Task { await store.refreshRecommendation() }
                } label: {
                    HStack(spacing: 9) {
                        Image(systemName: "sparkles").font(.system(size: 16, weight: .semibold))
                        Text("Сгенерировать совет").font(.jbm(14.5, weight: .bold))
                    }
                    .foregroundStyle(.white)
                    .frame(maxWidth: .infinity).frame(height: 48)
                    .background(DesignPalette.accent, in: Capsule())
                }
                .buttonStyle(.pressable(scale: 0.96))
                .padding(.top, 16)
            }
            .frame(maxWidth: .infinity)
            .padding(.top, 14)
            .padding(.horizontal, 6)
        }
        .padding(16)
        .liquidGlass(radius: 26)
    }

    // MARK: failed

    private func failedCard(_ rec: RecommendationResponse) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            header(basedOn: nil)
            HStack(alignment: .top, spacing: 12) {
                ZStack {
                    Circle().fill(DesignPalette.bad.opacity(0.10)).frame(width: 40, height: 40)
                        .overlay(Circle().stroke(DesignPalette.bad.opacity(0.22), lineWidth: 0.5))
                    Image(systemName: "exclamationmark.triangle").font(.system(size: 18))
                        .foregroundStyle(DesignPalette.bad)
                }
                VStack(alignment: .leading, spacing: 4) {
                    Text("Не удалось подготовить план")
                        .font(.jbm(14.5, weight: .bold)).tracking(-0.3).foregroundStyle(
                            DesignPalette.ink)
                    Text(failureMessage(rec.error))
                        .font(.jbm(12)).foregroundStyle(DesignPalette.ink3)
                        .lineSpacing(2).fixedSize(horizontal: false, vertical: true)
                }
            }
            .padding(.top, 14)
            Button {
                Task { await store.refreshRecommendation() }
            } label: {
                HStack(spacing: 9) {
                    Image(systemName: "arrow.triangle.2.circlepath").font(
                        .system(size: 14, weight: .semibold))
                    Text("Повторить").font(.jbm(14, weight: .bold))
                }
                .foregroundStyle(DesignPalette.ink2)
                .frame(maxWidth: .infinity).frame(height: 46)
                .background(DesignPalette.ink.opacity(0.05), in: Capsule())
                .overlay(Capsule().stroke(DesignPalette.ink.opacity(0.10), lineWidth: 0.5))
            }
            .buttonStyle(.plain)
            .padding(.top, 16)
        }
        .padding(16)
        .liquidGlass(radius: 26)
    }

    private func failureMessage(_ error: String?) -> String {
        guard let error, !error.isEmpty else {
            return "Старый план скрыт. Попробуй ещё раз."
        }
        if error.localizedCaseInsensitiveContains("ограничения методики") {
            return
                "План не прошёл автоматическую проверку нагрузки. Старый план скрыт — попробуй ещё раз."
        }
        return "\(error)\nСтарый план скрыт — попробуй ещё раз."
    }

    // MARK: shared bits

    private func header(basedOn: Int?) -> some View {
        HStack {
            HStack(spacing: 8) {
                Image(systemName: "sparkles").font(.system(size: 14)).foregroundStyle(
                    DesignPalette.accent)
                Text("Совет тренера")
                    .font(.jbm(10.5, weight: .semibold)).tracking(0.6)
                    .textCase(.uppercase).foregroundStyle(DesignPalette.ink)
            }
            Spacer()
            if let basedOn {
                Text("по \(basedOn) трен.")
                    .font(.jbm(10.5, weight: .semibold))
                    .foregroundStyle(DesignPalette.ink4)
            }
        }
    }
}

// Compact preparation-phase chip built from the recommendation's server-side
// coach context: «ДЕФИЦИТ · Н2», «НАБОР · Н5» or a warn-tinted «РАЗГРУЗКА».
struct CoachPhaseChip: View {
    var label: String
    var tint: Color

    var body: some View {
        Text(label)
            .font(.jbm(9.5, weight: .bold)).tracking(0.5)
            .foregroundStyle(tint)
            .padding(.horizontal, 8).padding(.vertical, 4)
            .background(tint.opacity(0.12), in: Capsule())
            .overlay(Capsule().stroke(tint.opacity(0.22), lineWidth: 0.5))
            .lineLimit(1)
            .fixedSize()
    }

    static func make(_ context: CoachContext?) -> CoachPhaseChip? {
        guard let context else { return nil }
        if context.deloadWeek == true {
            return CoachPhaseChip(label: "РАЗГРУЗКА", tint: DesignPalette.warn)
        }
        // The athlete names the stage in coach_state (phase_params.title) — e.g.
        // «Ф0 · возврат», where the engine code is still cut_recomp. Rendering
        // the engine code would print «ДЕФИЦИТ» on a stage whose whole point is
        // NOT to lose weight. The switch stays as the fallback for a plan
        // generated before the title existed.
        let name: String
        if let title = context.phaseTitle?.trimmingCharacters(in: .whitespacesAndNewlines),
            !title.isEmpty
        {
            name = title.uppercased()
        } else {
            guard let phase = context.phase else { return nil }
            switch phase {
            case "cut_recomp": name = "ДЕФИЦИТ"
            case "lean_bulk": name = "НАБОР"
            case "maintenance": name = "ПОДДЕРЖАНИЕ"
            default: name = phase.uppercased()
            }
        }
        if let week = context.blockWeek {
            return CoachPhaseChip(label: "\(name) · Н\(week)", tint: DesignPalette.ink3)
        }
        return CoachPhaseChip(label: name, tint: DesignPalette.ink3)
    }
}

// The "почему так" sheet behind the "?" in the plan header — focus + load + the
// full rationale text that used to live (collapsed) inside the expanded card.
struct CoachRationaleSheet: View {
    var focus: String?
    var loadType: String?
    var rationale: String
    var coachContext: CoachContext?

    var body: some View {
        ZStack {
            WarmWallpaper()
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    HStack(spacing: 8) {
                        Image(systemName: "sparkles")
                            .font(.system(size: 15))
                            .foregroundStyle(DesignPalette.accent)
                        Text("Почему так")
                            .font(.jbm(11, weight: .bold)).tracking(0.6)
                            .textCase(.uppercase).foregroundStyle(DesignPalette.ink2)
                        Spacer()
                    }
                    if let focus, !focus.isEmpty {
                        Text(focus)
                            .font(.jbm(18, weight: .bold)).tracking(-0.4)
                            .foregroundStyle(DesignPalette.ink)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    HStack(spacing: 8) {
                        if let loadType {
                            let chip = historyLoadChip(loadType)
                            HStack(spacing: 6) {
                                Circle().fill(chip.color).frame(width: 6, height: 6)
                                Text("\(chip.label) нагрузка".uppercased())
                                    .font(.jbm(10, weight: .bold)).tracking(0.6)
                                    .foregroundStyle(chip.color)
                            }
                            .padding(.horizontal, 10).padding(.vertical, 5)
                            .background(chip.color.opacity(0.13), in: Capsule())
                            .overlay(Capsule().stroke(chip.color.opacity(0.24), lineWidth: 0.5))
                        }
                        if let phaseChip = CoachPhaseChip.make(coachContext) {
                            phaseChip
                        }
                    }
                    VStack(alignment: .leading, spacing: 10) {
                        ForEach(Array(paragraphs.enumerated()), id: \.offset) { _, para in
                            Text(markdown(para))
                                .font(.jbm(13))
                                .foregroundStyle(DesignPalette.ink2)
                                .lineSpacing(4)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    .padding(.top, 2)
                }
                .padding(20)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }

    // Split the rationale into readable paragraphs (the model emits one logical
    // point per line); blank lines are dropped.
    private var paragraphs: [String] {
        rationale
            .components(separatedBy: "\n")
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
    }

    // Render **bold** inline; fall back to plain text if markdown can't parse.
    private func markdown(_ line: String) -> AttributedString {
        (try? AttributedString(
            markdown: line,
            options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        )) ?? AttributedString(line)
    }
}
