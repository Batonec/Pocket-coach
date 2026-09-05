import SwiftUI

// MARK: - Settings / Sign-in / Loading / Error / Toast / Empty

struct SettingsSheet: View {
    @EnvironmentObject private var store: TrainerStore
    @Environment(\.dismiss) private var dismiss
    @State private var draftURL = ""

    var body: some View {
        NavigationStack {
            Form {
                Section("Backend") {
                    TextField("URL", text: $draftURL)
                        .keyboardType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    Text(
                        "Production: https://trainer.superbatonec.org. Локально: http://127.0.0.1:8080."
                    )
                    .font(.caption)
                    .foregroundStyle(.secondary)
                }

                if let user = store.currentUser {
                    Section("Аккаунт") {
                        LabeledContent("Пользователь", value: user.displayName ?? "Trainer")
                        Button("Выйти", role: .destructive) {
                            Task {
                                await store.signOut()
                                dismiss()
                            }
                        }
                    }
                }

                Section {
                    Button("Сохранить и переподключиться") {
                        store.apiBaseURLString = draftURL
                        Task { await store.reconnect() }
                        dismiss()
                    }
                    Button("Обновить данные") {
                        Task { await store.refreshServerData() }
                        dismiss()
                    }
                }
            }
            .navigationTitle("Настройки")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Готово") { dismiss() }
                }
            }
            .onAppear { draftURL = store.apiBaseURLString }
        }
    }
}

struct SignInScreen: View {
    @EnvironmentObject private var store: TrainerStore
    @State private var showSettings = false
    var message: String?

    var body: some View {
        VStack(spacing: 18) {
            Spacer()
            ZStack {
                Circle().fill(DesignPalette.accent.opacity(0.12)).frame(width: 110, height: 110)
                GlyphIcon(glyph: .delts, size: 56, lineWidth: 2.4, tint: DesignPalette.accent)
            }
            VStack(spacing: 6) {
                Text("Trainer").display(size: 36, weight: .heavy)
                Text("Подключаемся к серверу")
                    .font(.jbm(14))
                    .foregroundStyle(DesignPalette.ink3)
            }
            if let message, !message.isEmpty {
                Text(message)
                    .font(.jbm(12))
                    .foregroundStyle(DesignPalette.ink3)
                    .multilineTextAlignment(.center)
                    .padding(.horizontal, 32)
            }
            Button {
                Task { await store.reconnect() }
            } label: {
                Text("Повторить")
                    .font(.jbm(16, weight: .heavy))
                    .foregroundStyle(.white)
                    .padding(.horizontal, 32)
                    .frame(height: 52)
                    .background(DesignPalette.accent, in: Capsule())
                    .shadow(color: DesignPalette.accent.opacity(0.4), radius: 16, y: 6)
            }
            .buttonStyle(.pressable(scale: 0.96))

            Button {
                showSettings = true
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: "gear")
                    Text("Backend")
                }
                .font(.jbm(14, weight: .semibold))
                .foregroundStyle(DesignPalette.ink2)
                .padding(.horizontal, 18)
                .frame(height: 42)
                .chipBackground()
            }
            .buttonStyle(.plain)
            Spacer()
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .sheet(isPresented: $showSettings) {
            SettingsSheet().environmentObject(store)
        }
    }
}

struct LoadingScreen: View {
    var body: some View {
        VStack(spacing: 14) {
            GlyphIcon(glyph: .delts, size: 48, lineWidth: 2.2, tint: DesignPalette.accent)
            Text("Trainer").display(size: 32, weight: .heavy)
            ProgressView().tint(DesignPalette.accent)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

struct ErrorScreen: View {
    @EnvironmentObject private var store: TrainerStore
    var message: String

    var body: some View {
        VStack(spacing: 14) {
            Image(systemName: "wifi.exclamationmark")
                .font(.jbm(40, weight: .heavy))
                .foregroundStyle(DesignPalette.warn)
            Text("Не удалось загрузить Trainer").display(size: 22, weight: .heavy)
                .multilineTextAlignment(.center)
            Text(message)
                .font(.jbm(13))
                .foregroundStyle(DesignPalette.ink3)
                .multilineTextAlignment(.center)
                .padding(.horizontal)
            Button {
                Task { await store.reconnect() }
            } label: {
                Text("Повторить")
                    .font(.jbm(16, weight: .heavy))
                    .foregroundStyle(.white)
                    .padding(.horizontal, 28)
                    .frame(height: 50)
                    .background(DesignPalette.accent, in: Capsule())
            }
            .buttonStyle(.pressable(scale: 0.96))
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(24)
    }
}

struct ToastView: View {
    var message: String

    var body: some View {
        Text(message)
            .font(.jbm(14, weight: .heavy))
            .foregroundStyle(.white)
            .lineLimit(2)
            .multilineTextAlignment(.center)
            .padding(.horizontal, 16)
            .padding(.vertical, 10)
            .background(DesignPalette.ink.opacity(0.92), in: Capsule())
            .padding(.horizontal, 20)
    }
}

struct EmptyStateCard: View {
    var glyph: ExerciseGlyph
    var title: String
    var subtitle: String

    var body: some View {
        VStack(spacing: 8) {
            Text(title)
                .font(.jbm(16, weight: .heavy))
                .tracking(-0.3)
                .foregroundStyle(DesignPalette.ink)
            Text(subtitle)
                .mono(13)
                .foregroundStyle(DesignPalette.ink3)
                .multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity)
        .padding(24)
        .glassCard(radius: 22)
    }
}

// MARK: - Helpers

extension Collection {
    subscript(safe index: Index) -> Element? {
        indices.contains(index) ? self[index] : nil
    }
}
