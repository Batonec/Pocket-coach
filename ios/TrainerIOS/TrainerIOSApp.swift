import AppIntents
import SwiftUI

@main
struct TrainerIOSApp: App {
    // Siri-интенты работают с TrainerStore.shared; UI обязан быть тем же
    // инстансом, иначе голосовой подход не появится на экране.
    @StateObject private var store = TrainerStore.shared

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(store)
                .task {
                    await store.boot()
                    // Каталог упражнений приезжает с backend; после загрузки
                    // Siri переиндексирует их как параметры голосовых команд.
                    TrainerAppShortcuts.updateAppShortcutParameters()
                }
        }
    }
}
