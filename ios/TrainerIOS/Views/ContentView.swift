import SwiftUI

// MARK: - ContentView shell

struct ContentView: View {
    @EnvironmentObject private var store: TrainerStore
    @State private var noteWorkout: Workout?

    var body: some View {
        ZStack(alignment: .top) {
            switch store.bootState {
            case .idle, .loading:
                ZStack {
                    WarmWallpaper(dim: true)
                    LoadingScreen()
                }
            case .loaded:
                MainShellView()
            case .needsSignIn(let message):
                ZStack {
                    WarmWallpaper(dim: true)
                    SignInScreen(message: message)
                }
            case .failed(let message):
                ZStack {
                    WarmWallpaper(dim: true)
                    ErrorScreen(message: message)
                }
            }

            if let toast = store.toast {
                ToastView(message: toast)
                    .padding(.top, 60)
                    .transition(.move(edge: .top).combined(with: .opacity))
                    .frame(maxWidth: .infinity, alignment: .top)
                    .allowsHitTesting(false)
            }

            // Полоска живёт над таб-баром и поверх любого экрана: тренировку
            // могли записать голосом, а заметка предлагается к факту, а не к
            // месту в интерфейсе.
            if let finished = store.finishedWorkout {
                FinishWorkoutStrip(summary: finished.summary) {
                    noteWorkout = store.workouts.first { $0.id == finished.id }
                    store.dismissFinishedWorkout()
                }
                .padding(.horizontal, 14)
                .padding(.bottom, 96)
                .transition(.move(edge: .bottom).combined(with: .opacity))
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .bottom)
            }
        }
        .sheet(item: $noteWorkout) { workout in
            WorkoutNoteSheet(workout: workout)
                .environmentObject(store)
        }
        .animation(.spring(response: 0.28, dampingFraction: 0.86), value: store.toast)
        .animation(.spring(response: 0.30, dampingFraction: 0.86), value: store.finishedWorkout)
        .animation(.spring(response: 0.32, dampingFraction: 0.85), value: store.currentTab)
        .animation(.spring(response: 0.32, dampingFraction: 0.85), value: store.draft.hasRealSets)
        // Decimal-pad startup is surprisingly expensive on a cold process.
        // Pay that one-time cost behind the loading screen, not after the user
        // taps "+" on Measurements.
        .onAppear { DecimalKeyboardPrewarmer.warmUp() }
    }
}

private struct MainShellView: View {
    @EnvironmentObject private var store: TrainerStore
    @Environment(\.scenePhase) private var scenePhase
    @State private var isShowingSettings = false

    // Per the design refresh, the Progress tab is gone — Progress is reachable
    // only by tapping the streak strip on History. Tabs are: История, Сегодня, Замеры.
    var body: some View {
        TabView(selection: tabBinding) {
            HistoryScreen(openSettings: { isShowingSettings = true })
                .tabItem {
                    Label(TrainerTab.history.title, systemImage: TrainerTab.history.systemImage)
                }
                .tag(TrainerTab.history)

            TodayScreen(openSettings: { isShowingSettings = true })
                .tabItem {
                    Label(TrainerTab.trainings.title, systemImage: TrainerTab.trainings.systemImage)
                }
                .tag(TrainerTab.trainings)

            BodyWeightScreen()
                .tabItem {
                    Label(TrainerTab.weight.title, systemImage: TrainerTab.weight.systemImage)
                }
                .tag(TrainerTab.weight)
        }
        .tint(DesignPalette.accent)
        .sheet(isPresented: $isShowingSettings) {
            SettingsSheet()
                .environmentObject(store)
        }
        .onChange(of: scenePhase) { _, newPhase in
            if newPhase == .active {
                store.refreshCoachSignals()
                // События пишутся не только отсюда: запись из чата с тренером
                // (MCP) — штатный сценарий, а сегодняшняя тренировка закрывает
                // открытое событие на сервере молча. Запрос такой же дешёвый,
                // как за сигналами, — локальный SQLite на той же машине.
                Task { await store.reloadEvents() }
            }
        }
    }

    // Old persisted .progress value should land on History (the new entry point).
    private var tabBinding: Binding<TrainerTab> {
        Binding(
            get: { store.currentTab == .progress ? .history : store.currentTab },
            set: { store.currentTab = $0 }
        )
    }
}
