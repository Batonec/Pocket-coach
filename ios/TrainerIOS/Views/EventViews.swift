import SwiftUI

// MARK: - События (периоды без тренировок)
//
// Событие — это текст с датами и ничего больше: ни одного числа из него не
// считается, ни один график его не видит. В интерфейсе оно живёт ровно там,
// где объясняет дырку в ленте, плюс плашкой на «Сегодня», пока идёт.

/// Что открыло композер: подсказка из разрыва (даты уже известны) или правка
/// существующего события.
enum EventComposerMode: Identifiable {
    case new(start: String, end: String?)
    case edit(TrainingEvent)

    var id: String {
        switch self {
        case .new(let start, let end): "new-\(start)-\(end ?? "open")"
        case .edit(let event): "edit-\(event.id)"
        }
    }
}

/// Карточка события в ленте «Истории». Та же порода, что карточка тренировки:
/// тот же радиус, та же рельса 64 pt. Но не бумага, а незалитый блок с
/// пунктиром — в ленте это буквально дырка, на которой оставили подпись.
/// Тренировки остаются главным содержимым.
struct EventCard: View {
    var event: TrainingEvent
    var today: String
    var onClose: () -> Void

    private var isOpen: Bool { event.isOpen }

    private var strokeColor: Color {
        isOpen ? DesignPalette.accent.opacity(0.35) : DesignPalette.ink.opacity(0.20)
    }

    var body: some View {
        HStack(spacing: 0) {
            rail
            VStack(alignment: .leading, spacing: 8) {
                // Длинный текст режется: лента рассчитана на короткие строки.
                // Целиком его видно в правке — тап по карточке её и открывает.
                Text(event.text)
                    .font(.jbm(13.5))
                    .lineSpacing(4)
                    .foregroundStyle(DesignPalette.ink2)
                    .lineLimit(4)
                    .multilineTextAlignment(.leading)
                    .frame(maxWidth: .infinity, alignment: .leading)

                if isOpen {
                    Button(action: onClose) {
                        Text("Закончилось")
                            .font(.jbm(11.5, weight: .bold))
                            .foregroundStyle(DesignPalette.ink)
                            .padding(.horizontal, 12)
                            .padding(.vertical, 6)
                            .chipBackground()
                    }
                    .buttonStyle(.pressable(scale: 0.96))
                }
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 12)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(
            RoundedRectangle(cornerRadius: 20, style: .continuous)
                .fill(isOpen ? DesignPalette.accent.opacity(0.04) : Color.clear)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 20, style: .continuous)
                .strokeBorder(style: StrokeStyle(lineWidth: 1, dash: [4, 4]))
                .foregroundStyle(strokeColor)
        )
    }

    private var rail: some View {
        let labels = TrainerLogic.eventRailLabels(event, today: today)
        let isRange = labels.day.contains("–")

        return VStack(spacing: 0) {
            VStack(spacing: 3) {
                if isOpen {
                    HStack(alignment: .firstTextBaseline, spacing: 0) {
                        Text("с ")
                            .font(.jbm(12))
                            .foregroundStyle(DesignPalette.ink3)
                        Text(labels.day)
                            .font(.jbm(19, weight: .bold))
                            .tracking(-0.5)
                            .foregroundStyle(DesignPalette.ink)
                    }
                } else {
                    Text(labels.day)
                        .font(.jbm(isRange ? 16 : 28, weight: .bold))
                        .tracking(isRange ? -0.6 : -0.04)
                        .foregroundStyle(DesignPalette.ink)
                }

                Text(labels.month)
                    .tLabel(size: labels.month.contains("–") ? 9 : 10.5)
            }

            Spacer(minLength: 4)

            Rectangle()
                .fill(DesignPalette.ink.opacity(0.10))
                .frame(width: 22, height: 0.5)
                .padding(.vertical, 4)

            HStack(spacing: 4) {
                if isOpen {
                    Circle()
                        .fill(DesignPalette.accent)
                        .frame(width: 5, height: 5)
                        .overlay(
                            Circle()
                                .stroke(DesignPalette.accent.opacity(0.15), lineWidth: 3)
                        )
                }
                Text(isOpen ? "идёт" : "\(event.dayCount(today: today)) дн.")
                    .tLabel(
                        size: 9,
                        color: isOpen ? DesignPalette.accent : DesignPalette.ink4
                    )
            }
        }
        .padding(.horizontal, 5)
        .padding(.vertical, 13)
        .frame(width: 64)
        .frame(maxHeight: .infinity)
        .overlay(alignment: .trailing) {
            VerticalDashedLine(color: DesignPalette.ink.opacity(0.18))
        }
    }
}

/// Подсказка в самой дырке — единственная точка входа в событие. Свайп влево
/// на «Истории» занят удалением, свободных целей на экране нет, поэтому вход
/// контекстный: строка появляется ровно там, где разрыв между тренировками
/// длиннее порога. Побочный эффект и есть главное свойство — интерфейс
/// физически не может предложить событие на дату, где уже есть тренировка.
struct EventGapPromptRow: View {
    var gap: TrainerLogic.HistoryGap
    var onTap: () -> Void

    var body: some View {
        Button(action: onTap) {
            HStack(spacing: 8) {
                Text("\(gap.days) дн. без тренировок")
                    .tLabel(size: 9.5, color: DesignPalette.ink4)

                Spacer(minLength: 8)

                Text("отметить событие")
                    .font(.jbm(11.5, weight: .bold))
                    .foregroundStyle(DesignPalette.accent)

                Image(systemName: "chevron.right")
                    .font(.jbm(9, weight: .bold))
                    .foregroundStyle(DesignPalette.accent)
            }
            .padding(.horizontal, 14)
            .frame(maxWidth: .infinity)
            .frame(height: 42)
            .background(
                RoundedRectangle(cornerRadius: 16, style: .continuous)
                    .strokeBorder(style: StrokeStyle(lineWidth: 1, dash: [4, 4]))
                    .foregroundStyle(DesignPalette.ink.opacity(0.16))
            )
        }
        .buttonStyle(.pressable(scale: 0.98))
    }
}

/// Плашка открытого события на «Сегодня». Состояние, а не упрёк: нейтральные
/// чернила, без красного и без слова «пропущено».
struct TodayEventStrip: View {
    var event: TrainingEvent
    var today: String
    var onTap: () -> Void
    var onClose: () -> Void

    private var headline: String {
        let firstLine = event.text.split(separator: "\n").first.map(String.init) ?? event.text
        return firstLine.trimmingCharacters(in: .whitespaces)
    }

    var body: some View {
        HStack(spacing: 10) {
            Circle()
                .fill(DesignPalette.accent)
                .frame(width: 7, height: 7)
                .overlay(Circle().stroke(DesignPalette.accent.opacity(0.15), lineWidth: 3))

            VStack(alignment: .leading, spacing: 1) {
                HStack(spacing: 4) {
                    Text(headline)
                        .font(.jbm(13.5, weight: .bold))
                        .tracking(-0.2)
                        .foregroundStyle(DesignPalette.ink)
                        .lineLimit(1)
                    Text("· \(event.dayCount(today: today)) дн.")
                        .mono(13.5, weight: .semibold)
                        .foregroundStyle(DesignPalette.ink3)
                        .lineLimit(1)
                        .layoutPriority(1)
                }
                Text("План сегодня легче обычного")
                    .font(.jbm(11))
                    .foregroundStyle(DesignPalette.ink3)
                    .lineLimit(1)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .contentShape(Rectangle())
            .onTapGesture(perform: onTap)

            Button(action: onClose) {
                Text("Закончилась?")
                    .font(.jbm(11.5, weight: .bold))
                    .foregroundStyle(DesignPalette.ink)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 7)
                    .chipBackground()
            }
            .buttonStyle(.pressable(scale: 0.96))
        }
        .padding(EdgeInsets(top: 9, leading: 13, bottom: 9, trailing: 10))
        .liquidGlass(radius: 18)
    }
}

/// Переключатель «ещё идёт». Системный Toggle в моно-язык не приведён, а этот
/// нужен ровно в одном месте — поэтому он и живёт рядом с композером.
private struct EventRunningSwitch: View {
    var isOn: Bool

    var body: some View {
        Capsule()
            .fill(isOn ? DesignPalette.accent : DesignPalette.ink.opacity(0.14))
            .frame(width: 46, height: 28)
            .overlay(alignment: isOn ? .trailing : .leading) {
                Circle()
                    .fill(Color.white)
                    .frame(width: 22, height: 22)
                    .shadow(color: DesignPalette.ink.opacity(0.28), radius: 1.5, y: 1)
                    .padding(.horizontal, 3)
            }
            .animation(.spring(response: 0.24, dampingFraction: 0.8), value: isOn)
    }
}

private struct EventDateField: View {
    var label: String
    var value: String
    var muted: Bool
    var onTap: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(label)
                .tLabel(size: 9)

            Button(action: onTap) {
                Text(value)
                    .font(.jbm(15, weight: .bold))
                    .tracking(-0.2)
                    .foregroundStyle(muted ? DesignPalette.ink4 : DesignPalette.ink)
                    .frame(maxWidth: .infinity)
                    .frame(height: 46)
                    .background(fieldBackground)
            }
            .buttonStyle(.pressable(scale: 0.97))
        }
        .frame(maxWidth: .infinity)
    }

    @ViewBuilder
    private var fieldBackground: some View {
        if muted {
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .strokeBorder(style: StrokeStyle(lineWidth: 1, dash: [4, 4]))
                .foregroundStyle(DesignPalette.ink.opacity(0.14))
        } else {
            ZStack {
                RoundedRectangle(cornerRadius: 14, style: .continuous)
                    .fill(Color.white.opacity(0.6))
                RoundedRectangle(cornerRadius: 14, style: .continuous)
                    .stroke(DesignPalette.ink.opacity(0.10), lineWidth: 0.5)
            }
        }
    }
}

struct EventComposerSheet: View {
    var mode: EventComposerMode

    @EnvironmentObject private var store: TrainerStore
    @Environment(\.dismiss) private var dismiss

    @State private var startDate: Date
    @State private var endDate: Date
    @State private var isRunning: Bool
    @State private var text: String
    @State private var openPicker: PickedField?
    @State private var isConfirmingDelete = false
    @State private var detent: PresentationDetent = .height(560)

    private enum PickedField { case start, end }

    /// Пресеты подставляют текст, а не категорию: категории в данных нет и не
    /// будет — это миграция и ещё одно место синхронизации ради иконки.
    private static let presets = ["Болезнь", "Травма", "Поездка", "Не спал", "Зал закрыт"]

    init(mode: EventComposerMode) {
        self.mode = mode
        switch mode {
        case .new(let start, let end):
            _startDate = State(initialValue: DateTools.date(from: start))
            _endDate = State(initialValue: DateTools.date(from: end ?? start))
            _isRunning = State(initialValue: end == nil)
            _text = State(initialValue: "")
        case .edit(let event):
            _startDate = State(initialValue: DateTools.date(from: event.startDate))
            _endDate = State(initialValue: DateTools.date(from: event.endDate ?? event.startDate))
            _isRunning = State(initialValue: event.isOpen)
            _text = State(initialValue: event.text)
        }
    }

    private var isEditing: Bool {
        if case .edit = mode { return true }
        return false
    }

    private var editedEvent: TrainingEvent? {
        if case .edit(let event) = mode { return event }
        return nil
    }

    /// Открытое событие одно. Если оно уже есть и правим мы не его — «ещё идёт»
    /// недоступно: иначе backend откажет уже на сохранении.
    private var canRun: Bool {
        guard let open = store.openEvent else { return true }
        return open.id == editedEvent?.id
    }

    private var dayCount: Int {
        let start = DateTools.iso(from: startDate)
        let end = isRunning ? DateTools.localTodayISO() : DateTools.iso(from: endDate)
        return max(1, DateTools.daysBetween(start, max(end, start)) + 1)
    }

    private var canSave: Bool {
        text.nilIfBlank != nil && !store.isSavingEvent
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header

            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    datesRow
                        .padding(.top, 14)

                    if let openPicker {
                        datePicker(for: openPicker)
                            .padding(.top, 10)
                    }

                    runningRow
                        .padding(.top, 10)

                    presetsRow
                        .padding(.top, 12)

                    MonoTextArea(placeholder: "болел, температура", text: $text)
                        .padding(.top, 10)

                    // Отказ сервера — второе открытое событие, будущая дата,
                    // нет связи — строкой под полем: тост рисуется ПОД шитом,
                    // и на этом экране его физически не видно.
                    if let eventError = store.eventError {
                        Text(eventError)
                            .font(.jbm(11.5, weight: .semibold))
                            .foregroundStyle(DesignPalette.bad)
                            .multilineTextAlignment(.leading)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.top, 8)
                    }
                }
            }
            .scrollIndicators(.hidden)

            actionsRow
                .padding(.top, 14)

            Text("Тренер перечитает план — как после нового замера")
                .font(.jbm(11))
                .foregroundStyle(DesignPalette.ink4)
                .multilineTextAlignment(.center)
                .frame(maxWidth: .infinity)
                .padding(.top, 9)
        }
        .padding(.horizontal, 16)
        .padding(.top, 16)
        .padding(.bottom, 26)
        .background(WarmWallpaper())
        .presentationDetents([.height(560), .large], selection: $detent)
        .presentationDragIndicator(.visible)
        .interactiveDismissDisabled(store.isSavingEvent)
        .onAppear { store.eventError = nil }
        .onChange(of: startDate) { _, newValue in
            // Конец раньше начала невозможен по построению: поле конца
            // подтягивается за началом, а его пикер ограничен снизу.
            if endDate < newValue { endDate = newValue }
        }
        .confirmationDialog(
            "Удалить событие?",
            isPresented: $isConfirmingDelete,
            titleVisibility: .visible
        ) {
            Button("Удалить", role: .destructive) {
                guard let event = editedEvent else { return }
                Task {
                    if await store.deleteEvent(event) { dismiss() }
                }
            }
            Button("Отмена", role: .cancel) {}
        }
    }

    private var header: some View {
        HStack(alignment: .top) {
            VStack(alignment: .leading, spacing: 2) {
                Text(isEditing ? "Правка события" : "Новое событие")
                    .tLabel()
                Text("Дни без тренировок")
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
    }

    private var datesRow: some View {
        HStack(alignment: .bottom, spacing: 8) {
            EventDateField(
                label: "Начало",
                value: DateTools.short(DateTools.iso(from: startDate)),
                muted: false,
                onTap: { toggle(.start) }
            )

            Text("—")
                .font(.jbm(13))
                .foregroundStyle(DesignPalette.ink4)
                .padding(.bottom, 15)

            EventDateField(
                label: "Конец",
                value: isRunning ? "идёт" : DateTools.short(DateTools.iso(from: endDate)),
                muted: isRunning,
                onTap: {
                    // Пока событие «идёт», конца нет и выбирать нечего —
                    // сначала переключатель.
                    guard !isRunning else { return }
                    toggle(.end)
                }
            )

            VStack(spacing: 2) {
                Text("\(dayCount)")
                    .display(size: 20, weight: .bold)
                    .foregroundStyle(isRunning ? DesignPalette.accent : DesignPalette.ink)
                Text("дн.")
                    .tLabel(size: 9)
            }
            .frame(width: 52)
            .padding(.bottom, 4)
        }
    }

    @ViewBuilder
    private func datePicker(for field: PickedField) -> some View {
        // Системный DatePicker — уже принятый прецедент (композер замеров);
        // будущее он запрещает так же, как backend.
        DatePicker(
            "",
            selection: field == .start ? $startDate : $endDate,
            in: (field == .start ? Date.distantPast : startDate)...Date(),
            displayedComponents: .date
        )
        .datePickerStyle(.graphical)
        .labelsHidden()
        .tint(DesignPalette.accent)
        .padding(.horizontal, 6)
        .padding(.vertical, 4)
        .glassCard(radius: 18)
    }

    private var runningRow: some View {
        Button {
            guard canRun else { return }
            withAnimation(.spring(response: 0.26, dampingFraction: 0.85)) {
                isRunning.toggle()
                if isRunning { openPicker = nil }
            }
        } label: {
            HStack(spacing: 12) {
                VStack(alignment: .leading, spacing: 1) {
                    Text("Ещё идёт")
                        .font(.jbm(14, weight: .bold))
                        .tracking(-0.2)
                        .foregroundStyle(DesignPalette.ink)
                    Text(
                        canRun
                            ? "Закроется само, когда запишешь тренировку"
                            : "Одно открытое событие уже есть"
                    )
                    .font(.jbm(11))
                    .foregroundStyle(DesignPalette.ink3)
                    .multilineTextAlignment(.leading)
                }
                .frame(maxWidth: .infinity, alignment: .leading)

                EventRunningSwitch(isOn: isRunning)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
            .glassCard(radius: 16)
        }
        .buttonStyle(.plain)
        .opacity(canRun ? 1 : 0.55)
    }

    private var presetsRow: some View {
        WrapLayout(spacing: 6) {
            ForEach(Self.presets, id: \.self) { preset in
                Button {
                    apply(preset: preset)
                } label: {
                    presetChip(
                        preset,
                        isPicked: text.trimmingCharacters(in: .whitespacesAndNewlines) == preset
                    )
                }
                .buttonStyle(.pressable(scale: 0.96))
            }
        }
    }

    @ViewBuilder
    private func presetChip(_ preset: String, isPicked: Bool) -> some View {
        let label =
            Text(preset)
            .font(.jbm(12.5, weight: .bold))
            .tracking(-0.15)
            .foregroundStyle(isPicked ? Color.white : DesignPalette.ink2)
            .padding(.horizontal, 13)
            .padding(.vertical, 8)

        if isPicked {
            label.background(DesignPalette.ink, in: Capsule())
        } else {
            label.chipBackground()
        }
    }

    private var actionsRow: some View {
        HStack(spacing: 8) {
            if isEditing {
                Button {
                    isConfirmingDelete = true
                } label: {
                    Image(systemName: "trash")
                        .font(.jbm(16, weight: .semibold))
                        .foregroundStyle(DesignPalette.bad)
                        .frame(width: 56, height: 54)
                        .background(
                            RoundedRectangle(cornerRadius: 27, style: .continuous)
                                .fill(DesignPalette.bad.opacity(0.06))
                        )
                        .overlay(
                            RoundedRectangle(cornerRadius: 27, style: .continuous)
                                .stroke(DesignPalette.bad.opacity(0.20), lineWidth: 0.5)
                        )
                }
                .buttonStyle(.pressable(scale: 0.96))
                .accessibilityLabel("Удалить событие")
            }

            Button {
                Task { await save() }
            } label: {
                HStack(spacing: 8) {
                    if store.isSavingEvent {
                        ProgressView().tint(.white)
                    }
                    Text(isEditing ? "Сохранить" : "Добавить событие")
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
            .disabled(!canSave)
            .opacity(canSave ? 1 : 0.5)
        }
    }

    private func toggle(_ field: PickedField) {
        withAnimation(.spring(response: 0.28, dampingFraction: 0.86)) {
            openPicker = openPicker == field ? nil : field
            // Календарь высокий — на маленьком детенте он не помещается.
            if openPicker != nil { detent = .large }
        }
    }

    /// Пресет — это подстановка текста. Пустое поле и поле ровно с другим
    /// пресетом заменяются целиком; написанное руками не затирается — пресет
    /// дописывается в конец. Повторный тап по выбранному очищает поле, так что
    /// промах всегда обратим.
    private func apply(preset: String) {
        let current = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if current == preset {
            text = ""
        } else if current.isEmpty || Self.presets.contains(current) {
            text = preset
        } else {
            text = "\(current), \(preset.lowercased())"
        }
    }

    private func save() async {
        let start = DateTools.iso(from: startDate)
        let end = isRunning ? nil : DateTools.iso(from: endDate)
        let saved: Bool
        if let event = editedEvent {
            saved = await store.updateEvent(event, startDate: start, endDate: end, text: text)
        } else {
            saved = await store.saveEvent(startDate: start, endDate: end, text: text)
        }
        if saved { dismiss() }
    }
}
