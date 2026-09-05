import SwiftUI

// MARK: - Weekly report archive

/// Every cached weekly report, newest first. The server keeps one row per closed
/// week and never deletes them, so this screen lists the same table the Progress
/// screen reads its latest report from. Bodies arrive with the list, so a tap
/// opens the sheet without a second request; only the newest row may mark the
/// report read (see `TrainerLogic.weeklyReportArchiveRows`).
struct ReportArchiveScreen: View {
    @EnvironmentObject private var store: TrainerStore
    @Environment(\.dismiss) private var dismiss
    @State private var rows: [WeeklyReportArchiveRow] = []
    @State private var phase: Phase = .loading
    @State private var selectedRow: WeeklyReportArchiveRow?

    private enum Phase {
        case loading
        case loaded
        case failed
    }

    var body: some View {
        ZStack {
            WarmWallpaper()
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    topBar
                    TopTitle(sub: "отчёты тренера", title: "Все отчёты")
                        .padding(.horizontal, 4)
                    content
                }
                .padding(.horizontal, 14)
                .padding(.top, 8)
                .padding(.bottom, 24)
            }
            .scrollIndicators(.hidden)
        }
        .toolbar(.hidden, for: .navigationBar)
        .swipeBackOverlay { dismiss() }
        .sheet(item: $selectedRow) { row in
            WeeklyReportSheet(
                prefetchedEntry: row.entry,
                fetchesOnAppear: false,
                marksRead: row.isLatest
            )
            .environmentObject(store)
            .presentationDetents([.medium, .large])
            .presentationDragIndicator(.visible)
        }
        .task { await load() }
    }

    private var topBar: some View {
        HStack(spacing: 6) {
            Button {
                dismiss()
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: "chevron.left")
                        .font(.jbm(12, weight: .heavy))
                    Text("Прогресс")
                        .mono(13, weight: .semibold)
                }
                .foregroundStyle(DesignPalette.ink2)
                .padding(.horizontal, 11)
                .padding(.vertical, 6)
                .chipBackground()
            }
            .buttonStyle(.plain)

            Spacer()
        }
    }

    @ViewBuilder
    private var content: some View {
        switch phase {
        case .loading:
            HStack(spacing: 11) {
                ProgressView().tint(DesignPalette.accent)
                Text("Загружаю архив…")
                    .font(.jbm(12.5))
                    .foregroundStyle(DesignPalette.ink3)
            }
            .padding(.horizontal, 4)
            .padding(.top, 8)
        case .failed:
            VStack(alignment: .leading, spacing: 10) {
                Text("Не удалось загрузить архив")
                    .font(.jbm(15, weight: .bold)).tracking(-0.3)
                    .foregroundStyle(DesignPalette.ink)
                Button {
                    Task { await load() }
                } label: {
                    Text("Повторить")
                        .font(.jbm(13, weight: .bold))
                        .foregroundStyle(DesignPalette.accent)
                        .padding(.horizontal, 12)
                        .padding(.vertical, 7)
                        .chipBackground()
                }
                .buttonStyle(.pressable(scale: 0.97))
            }
            .padding(.horizontal, 4)
            .padding(.top, 8)
        case .loaded where rows.isEmpty:
            EmptyStateCard(
                glyph: .other,
                title: "Отчётов пока нет",
                subtitle: "Тренер собирает итоги недели сам — в ночь на понедельник. Загляни утром."
            )
        case .loaded:
            LazyVStack(spacing: 8) {
                ForEach(rows) { row in
                    Button {
                        open(row)
                    } label: {
                        ReportArchiveRowView(row: row)
                    }
                    .buttonStyle(.pressable(scale: 0.98))
                    .accessibilityLabel("Открыть отчёт \(row.periodLabel)")
                }
            }
        }
    }

    private func open(_ row: WeeklyReportArchiveRow) {
        // The sheet sends the read receipt for the newest report; the list
        // reflects it at once instead of waiting for a reload.
        if row.isLatest, let index = rows.firstIndex(where: \.isLatest) {
            rows[index].isUnread = false
        }
        selectedRow = row
    }

    @MainActor
    private func load() async {
        phase = .loading
        do {
            let reports = try await store.requestWeeklyReportHistory()
            rows = TrainerLogic.weeklyReportArchiveRows(reports, today: DateTools.iso(from: Date()))
            phase = .loaded
        } catch is CancellationError {
            // Leaving the screen mid-request: nothing to show.
        } catch {
            phase = .failed
        }
    }
}

/// One archive row: the period, an accent icon while the newest report is
/// unread, and the year when the week closed outside the current one.
private struct ReportArchiveRowView: View {
    var row: WeeklyReportArchiveRow

    var body: some View {
        HStack(spacing: 11) {
            ZStack {
                Circle()
                    .fill(
                        row.isUnread
                            ? DesignPalette.accent.opacity(0.12) : DesignPalette.ink.opacity(0.05)
                    )
                    .frame(width: 36, height: 36)
                    .overlay(
                        Circle().stroke(
                            row.isUnread ? DesignPalette.accent.opacity(0.20) : DesignPalette.sep,
                            lineWidth: 0.5))
                Image(systemName: "doc.text")
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(row.isUnread ? DesignPalette.accent : DesignPalette.ink3)
            }
            VStack(alignment: .leading, spacing: 2) {
                Text(row.periodLabel)
                    .font(.jbm(13.5, weight: .bold))
                    .foregroundStyle(DesignPalette.ink)
                if let subtitle {
                    Text(subtitle)
                        .font(.jbm(10.5, weight: .semibold))
                        .foregroundStyle(row.isUnread ? DesignPalette.accent : DesignPalette.ink3)
                }
            }
            Spacer()
            if let year = row.yearLabel {
                Text(year)
                    .font(.jbm(10.5, weight: .semibold))
                    .foregroundStyle(DesignPalette.ink4)
            }
            Image(systemName: "chevron.right")
                .font(.jbm(11, weight: .heavy))
                .foregroundStyle(DesignPalette.ink4)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 11)
        .glassCard(radius: 20)
    }

    private var subtitle: String? {
        if row.isUnread { return "не прочитан" }
        return row.isLatest ? "последний отчёт" : nil
    }
}
