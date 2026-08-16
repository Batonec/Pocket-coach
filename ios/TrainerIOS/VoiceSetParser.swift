import Foundation

/// Голосовой ввод подхода: разбор одной продиктованной фразы
/// («жим ногами 80 на 10, тяжело» / «leg press 80 by 10, hard») в поля черновика.
///
/// Здесь только value-логика — ни AppIntents, ни стора, — чтобы вся грамматика
/// покрывалась unit-тестами без Siri и без сети.
///
/// Русский и английский разбираются **одной** грамматикой, а не двумя ветками:
/// словари маркеров и синонимов просто объединены. Скрипты не пересекаются, так
/// что смешанная фраза («жим ногами 80 kg») тоже читается.

// MARK: - Язык

/// На каком языке говорит пользователь и на каком ему отвечать.
enum VoiceLanguage: String, Codable, Equatable {
    case ru
    case en

    /// Язык устройства. Берётся из глобального `AppleLanguages`, а не из
    /// `Locale.current`: последний фильтруется локализациями бандла и на
    /// англоязычном телефоне может схлопнуться в язык разработки.
    static var device: VoiceLanguage {
        let preferred =
            UserDefaults.standard.stringArray(forKey: "AppleLanguages")?.first
            ?? Locale.preferredLanguages.first
            ?? "en"
        return preferred.lowercased().hasPrefix("ru") ? .ru : .en
    }

    /// Язык самой фразы — сигнал сильнее системной настройки: язык Siri
    /// задаётся отдельно от языка телефона, и говорят именно на нём.
    static func detected(in text: String, fallback: VoiceLanguage) -> VoiceLanguage {
        var cyrillic = 0
        var latin = 0
        for scalar in text.unicodeScalars where CharacterSet.letters.contains(scalar) {
            if (0x0400...0x04FF).contains(scalar.value) {
                cyrillic += 1
            } else if scalar.isASCII {
                latin += 1
            }
        }
        guard cyrillic + latin > 0 else { return fallback }
        return cyrillic >= latin ? .ru : .en
    }
}

// MARK: - Результат разбора

struct ParsedVoiceSet: Equatable {
    /// Единственное уверенно распознанное упражнение.
    var exerciseID: Int?
    /// Кандидаты, когда фраза одинаково подходит нескольким («тяга», «row»).
    var ambiguousIDs: [Int]
    /// Что осталось от фразы после вычитания чисел, единиц и стоп-слов —
    /// нужно, чтобы переспросить пользователя его же словами.
    var spokenExercise: String
    var weight: Double?
    var reps: Int?
    var effort: SetEffort?
    /// Вес назвали в фунтах и пересчитали в килограммы.
    var convertedFromPounds: Bool

    var hasExercise: Bool { exerciseID != nil }
}

enum VoiceSetParser {
    /// Числа без явной единицы: одиночное значение до этого порога читается как
    /// повторы («подтягивания 8»), выше — как вес («жим ногами 80»).
    static let bareNumberRepsCeiling: Double = 30
    static let poundsInKilogram = 0.45359237

    static func parse(_ spoken: String, catalog: [ExerciseDefinition]) -> ParsedVoiceSet {
        let tokens = VoiceText.foldNumerals(VoiceText.tokens(spoken))

        var effort: SetEffort?
        var rest: [String] = []
        for token in tokens {
            if effort == nil, let value = effortValue(token) {
                effort = value
                continue
            }
            rest.append(token)
        }

        var readings: [(value: Double, unit: VoiceUnit?)] = []
        var phrase: [String] = []
        for (index, token) in rest.enumerated() {
            if let value = Double(token) {
                let following = index + 1 < rest.count ? unitMarker(rest[index + 1]) : nil
                let preceding = index > 0 ? leadingUnitMarker(rest[index - 1]) : nil
                readings.append((value, following ?? preceding))
                continue
            }
            if unitMarker(token) != nil || leadingUnitMarker(token) != nil || isStopWord(token) {
                continue
            }
            phrase.append(token)
        }

        let assigned = assign(readings)
        let match = ExerciseVoiceMatcher.match(phrase: phrase, in: catalog)

        return ParsedVoiceSet(
            exerciseID: match.exerciseID,
            ambiguousIDs: match.ambiguous,
            spokenExercise: phrase.joined(separator: " "),
            weight: assigned.weight,
            reps: assigned.reps.map { Int($0.rounded()) },
            effort: effort,
            convertedFromPounds: assigned.fromPounds
        )
    }

    /// Раскладка чисел по слотам: сначала те, у кого единица названа явно,
    /// затем свободные числа добивают недостающее.
    private static func assign(
        _ readings: [(value: Double, unit: VoiceUnit?)]
    ) -> (weight: Double?, reps: Double?, fromPounds: Bool) {
        var weight = readings.first(where: { $0.unit == .kilograms })?.value
        var reps = readings.first(where: { $0.unit == .repetitions })?.value
        var fromPounds = false

        // Фунты пересчитываются в килограммы: приложение хранит только кг, и
        // записать «80» вместо «80 pounds» значит тихо испортить историю.
        if weight == nil, let pounds = readings.first(where: { $0.unit == .pounds })?.value {
            weight = (pounds * poundsInKilogram * 2).rounded() / 2
            fromPounds = true
        }

        let free = readings.filter { $0.unit == nil }.map(\.value)
        if weight == nil, reps != nil {
            weight = free.first
        } else if reps == nil, weight != nil {
            reps = free.first
        } else if weight == nil, reps == nil {
            if free.count >= 2 {
                weight = free[0]
                reps = free[1]
            } else if let single = free.first {
                if single <= bareNumberRepsCeiling {
                    reps = single
                } else {
                    weight = single
                }
            }
        }

        return (weight, reps, fromPounds)
    }

    private static func effortValue(_ token: String) -> SetEffort? {
        if token.hasPrefix("тяжел") || token.hasPrefix("тяжк") || token.hasPrefix("жестк")
            || token == "отказ" || token == "предел" || token == "еле"
            || hardWords.contains(token)
        {
            return .hard
        }
        if token.hasPrefix("легк") || token == "легко" || token == "изи"
            || token == "просто" || easyWords.contains(token)
        {
            return .easy
        }
        if token.hasPrefix("норм") || token.hasPrefix("средн") || okWords.contains(token) {
            return .ok
        }
        return nil
    }

    private static let hardWords: Set<String> = [
        "hard", "heavy", "tough", "brutal", "killer", "grinder", "max", "maxed",
    ]
    private static let easyWords: Set<String> = ["easy", "light", "smooth", "breeze"]
    private static let okWords: Set<String> = [
        "ок", "окей", "ok", "okay", "normal", "fine", "medium", "moderate", "alright",
    ]

    /// Единица, названная ПОСЛЕ числа: «80 кг», «10 раз», «80 kilos», «10 reps».
    private static func unitMarker(_ token: String) -> VoiceUnit? {
        if token == "кг" || token.hasPrefix("килограм") || token == "кило"
            || kilogramWords.contains(token)
        {
            return .kilograms
        }
        if poundWords.contains(token) {
            return .pounds
        }
        // «раз» перечислен формами, а не префиксом: иначе «разгибания» уезжает
        // в единицы измерения и упражнение теряется.
        if repetitionWords.contains(token) || token.hasPrefix("повтор") {
            return .repetitions
        }
        return nil
    }

    /// Единица, названная ДО числа: «на 10», «весом 80», «by 10», «for 10».
    private static func leadingUnitMarker(_ token: String) -> VoiceUnit? {
        if token == "на" || token == "по" || token == "х" || token == "x"
            || leadingRepetitionWords.contains(token)
        {
            return .repetitions
        }
        if token.hasPrefix("вес") || token == "weight" {
            return .kilograms
        }
        return nil
    }

    private static let repetitionWords: Set<String> = [
        "раз", "раза", "разов", "реп", "репов", "рипов",
        "rep", "reps", "repetition", "repetitions",
    ]
    private static let leadingRepetitionWords: Set<String> = ["by", "for", "times"]
    private static let kilogramWords: Set<String> = [
        "kg", "kgs", "kilo", "kilos", "kilogram", "kilograms",
    ]
    private static let poundWords: Set<String> = ["lb", "lbs", "pound", "pounds"]

    private static let stopWords: Set<String> = [
        "добавь", "добавить", "запиши", "записать", "засчитай", "засчитать",
        "подход", "подхода", "подходов", "сет", "сета", "сетов", "выполнил", "сделал",
        "сделала", "было", "мне", "это", "пожалуйста", "плиз", "давай", "и", "в", "во",
        "с", "со", "у", "а", "же", "там", "короче", "ещё", "еще", "один", "новый",
        "тренировку", "тренировка", "тренировке", "упражнение", "упражнения",
        "покет", "коуч", "коуча", "коучу", "коуче", "тренер", "тренера", "тренеру",
        "add", "log", "record", "save", "put", "set", "sets", "did", "done", "just",
        "the", "a", "an", "to", "in", "on", "of", "my", "i", "was", "it", "and",
        "please", "new", "last", "workout", "exercise", "reps",
        "pocket", "coach", "trainer",
    ]

    private static func isStopWord(_ token: String) -> Bool {
        stopWords.contains(token)
    }
}

private enum VoiceUnit {
    case kilograms
    case pounds
    case repetitions
}

// MARK: - Нормализация и числительные

enum VoiceText {
    /// Нижний регистр, ё→е, пунктуация в пробелы. Десятичный разделитель
    /// выживает только между цифрами («82,5» → «82.5»), «80х10» разрезается.
    static func normalize(_ raw: String) -> String {
        let lowered = raw.lowercased().replacingOccurrences(of: "ё", with: "е")
        let characters = Array(lowered)
        var out = ""
        out.reserveCapacity(characters.count)

        for (index, character) in characters.enumerated() {
            let previous = index > 0 ? characters[index - 1] : " "
            let next = index + 1 < characters.count ? characters[index + 1] : " "
            let betweenDigits = previous.isNumber && next.isNumber

            switch character {
            case ",", ".":
                out.append(betweenDigits ? "." : " ")
            case "x", "х", "×", "*":
                out.append(betweenDigits ? " " : character)
            default:
                out.append(character.isLetter || character.isNumber ? character : " ")
            }
        }

        return out.split(separator: " ").joined(separator: " ")
    }

    static func tokens(_ raw: String) -> [String] {
        normalize(raw).split(separator: " ").map(String.init)
    }

    /// Свёртка числительных прописью в цифры: «восемьдесят два» → «82»,
    /// «eighty two» → «82», «two hundred» → «200», «десять с половиной» → «10.5».
    /// Siri обычно диктует цифрами, но не всегда.
    static func foldNumerals(_ tokens: [String]) -> [String] {
        var out: [String] = []
        var accumulator: Double?
        var lastComponent: Double?

        func flush() {
            guard let value = accumulator else { return }
            out.append(format(value))
            accumulator = nil
            lastComponent = nil
        }

        func addHalf() {
            if accumulator != nil {
                accumulator? += 0.5
            } else if let last = out.last, let value = Double(last) {
                out[out.count - 1] = format(value + 0.5)
            }
        }

        var index = 0
        while index < tokens.count {
            let token = tokens[index]

            // Цифры складывать нельзя: «55х12» — это два числа, а не 67.
            // Складываются только слова: «восемьдесят» + «два» = 82.
            if let digits = Double(token) {
                flush()
                out.append(format(digits))
                index += 1
                continue
            }

            // Английские сотни мультипликативны: «two hundred» = 200, а не 102.
            if token == "hundred", let accumulated = accumulator, accumulated < 100 {
                accumulator = accumulated * 100
                lastComponent = 100
                index += 1
                continue
            }

            if let value = numeralValue(token) {
                if let previous = lastComponent, value >= previous {
                    flush()
                }
                accumulator = (accumulator ?? 0) + value
                lastComponent = value
                index += 1
                continue
            }

            if let consumed = halfSuffixLength(tokens, at: index) {
                addHalf()
                index += consumed
                continue
            }

            flush()
            out.append(token)
            index += 1
        }

        flush()
        return out
    }

    /// «с половиной» / «and a half» / «a half» — относится к уже названному числу.
    private static func halfSuffixLength(_ tokens: [String], at index: Int) -> Int? {
        func token(_ offset: Int) -> String? {
            let position = index + offset
            return position < tokens.count ? tokens[position] : nil
        }

        if token(0) == "с", token(1)?.hasPrefix("половин") == true { return 2 }
        if token(0) == "and", token(1) == "a", token(2) == "half" { return 3 }
        if token(0) == "and", token(1) == "half" { return 2 }
        if token(0) == "a", token(1) == "half" { return 2 }
        return nil
    }

    private static func format(_ value: Double) -> String {
        value == value.rounded() ? String(Int(value)) : String(value)
    }

    private static func numeralValue(_ token: String) -> Double? {
        if let digits = Double(token) { return digits }
        return numerals[token]
    }

    private static let numerals: [String: Double] = [
        "ноль": 0,
        "один": 1, "одна": 1, "одного": 1, "полтора": 1.5,
        "два": 2, "две": 2, "двух": 2,
        "три": 3, "трех": 3,
        "четыре": 4, "четырех": 4,
        "пять": 5, "пяти": 5,
        "шесть": 6, "шести": 6,
        "семь": 7, "семи": 7,
        "восемь": 8, "восьми": 8,
        "девять": 9, "девяти": 9,
        "десять": 10, "десяти": 10,
        "одиннадцать": 11, "одиннадцати": 11,
        "двенадцать": 12, "двенадцати": 12,
        "тринадцать": 13, "тринадцати": 13,
        "четырнадцать": 14, "четырнадцати": 14,
        "пятнадцать": 15, "пятнадцати": 15,
        "шестнадцать": 16, "шестнадцати": 16,
        "семнадцать": 17, "семнадцати": 17,
        "восемнадцать": 18, "восемнадцати": 18,
        "девятнадцать": 19, "девятнадцати": 19,
        "двадцать": 20, "двадцати": 20,
        "тридцать": 30, "тридцати": 30,
        "сорок": 40, "сорока": 40,
        "пятьдесят": 50, "пятидесяти": 50,
        "шестьдесят": 60, "шестидесяти": 60,
        "семьдесят": 70, "семидесяти": 70,
        "восемьдесят": 80, "восьмидесяти": 80,
        "девяносто": 90, "девяноста": 90,
        "сто": 100, "ста": 100,
        "двести": 200, "двухсот": 200,
        "триста": 300, "трехсот": 300,
        "четыреста": 400, "пятьсот": 500,

        "zero": 0,
        "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
        "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
        "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
        "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
        "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
        "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
        "hundred": 100,
    ]
}

// MARK: - Сопоставление упражнения

enum ExerciseVoiceMatcher {
    /// Ниже порога фраза считается нераспознанной — лучше переспросить, чем
    /// записать подход не в то упражнение.
    static let acceptThreshold = 0.55
    /// Разрыв между лидером и вторым местом, ниже которого выбор неоднозначен.
    static let ambiguityMargin = 0.06

    static func match(
        phrase tokens: [String],
        in catalog: [ExerciseDefinition]
    ) -> (exerciseID: Int?, ambiguous: [Int]) {
        guard !tokens.isEmpty, !catalog.isEmpty else { return (nil, []) }

        let scored =
            catalog
            .map { (id: $0.id, score: score(tokens: tokens, for: $0.name)) }
            .sorted { $0.score > $1.score }

        guard let best = scored.first, best.score >= acceptThreshold else {
            return (nil, [])
        }

        let tied = scored.filter { best.score - $0.score < ambiguityMargin }
        if tied.count > 1 {
            return (nil, tied.map(\.id))
        }
        return (best.id, [])
    }

    static func score(tokens: [String], for exerciseName: String) -> Double {
        keys(for: exerciseName).map { score(tokens: tokens, against: $0) }.max() ?? 0
    }

    /// Разговорные варианты названия на нужном языке — их же получает Siri как
    /// synonyms сущности упражнения, чтобы «нижний блок» и «seated row»
    /// матчились её собственным движком.
    static func spokenSynonyms(for exerciseName: String, in language: VoiceLanguage) -> [String] {
        let all = synonyms[VoiceText.tokens(exerciseName).joined(separator: " ")] ?? []
        return all.filter { VoiceLanguage.detected(in: $0, fallback: language) == language }
    }

    private static func score(tokens: [String], against key: [String]) -> Double {
        guard !key.isEmpty, !tokens.isEmpty else { return 0 }
        let keyHits = key.filter { keyToken in tokens.contains { matches($0, keyToken) } }.count
        let spokenHits = tokens.filter { spoken in key.contains { matches(spoken, $0) } }.count
        // Покрытие названия важнее: лишние слова во фразе («жим ногами сделал»)
        // не должны топить верный матч.
        return Double(keyHits) / Double(key.count) * 0.7
            + Double(spokenHits) / Double(tokens.count) * 0.3
    }

    /// Морфология без стеммера: «ногами»/«ноги», «горизонт.»/«горизонтальная»,
    /// «curl»/«curls» сходятся по общему префиксу. Короткие слова сравниваются
    /// целиком.
    static func matches(_ lhs: String, _ rhs: String) -> Bool {
        if lhs == rhs { return true }
        if lhs.count < 3 || rhs.count < 3 { return false }
        return commonPrefixLength(lhs, rhs) >= 3
    }

    private static func commonPrefixLength(_ lhs: String, _ rhs: String) -> Int {
        var count = 0
        var left = lhs.startIndex
        var right = rhs.startIndex
        while left < lhs.endIndex, right < rhs.endIndex, lhs[left] == rhs[right] {
            count += 1
            left = lhs.index(after: left)
            right = rhs.index(after: right)
        }
        return count
    }

    /// Ключи упражнения: собственное название каталога плюс разговорные
    /// варианты на обоих языках. Односимвольные слова («в», «a») выбрасываются —
    /// они ничего не различают и только разбавляют покрытие.
    private static func keys(for exerciseName: String) -> [[String]] {
        let canonical = VoiceText.tokens(exerciseName)
        let extra = synonyms[canonical.joined(separator: " ")] ?? []
        return ([canonical] + extra.map { VoiceText.tokens($0) })
            .map { $0.filter { $0.count > 1 } }
            .filter { !$0.isEmpty }
    }

    /// Ключ — нормализованное название из каталога, значение — как это
    /// произносят вслух по-русски и по-английски. Каталог приходит с backend,
    /// поэтому упражнение без записи здесь матчится по собственному названию.
    private static let synonyms: [String: [String]] = [
        "жим ногами": [
            "ноги", "платформа", "жим ног", "ножной жим",
            "leg press", "legs", "leg presses",
        ],
        "жим гор": [
            "жим лежа", "жим горизонтальный", "горизонтальный жим",
            "жим от груди", "жим на грудь", "грудь",
            "bench press", "bench", "flat bench", "barbell bench", "chest",
        ],
        "тяга верт": [
            "тяга вертикальная", "вертикальная тяга", "верхний блок",
            "тяга сверху", "тяга к груди", "широчайшие",
            "lat pulldown", "pulldown", "pull down", "lat pull down", "lats",
        ],
        "тяга горизонт": [
            "тяга горизонтальная", "горизонтальная тяга", "нижний блок",
            "тяга к поясу", "тяга к животу",
            "seated row", "cable row", "row", "horizontal row", "rows",
        ],
        "дельты": [
            "плечи", "махи", "дельта", "дельтовидные", "средняя дельта",
            "delts", "shoulders", "lateral raises", "side raises", "laterals",
        ],
        "задняя дельта": [
            "задняя дельта", "задние дельты", "обратная бабочка", "обратные махи",
            "махи в наклоне", "задний пучок",
            "rear delt", "rear delts", "reverse fly", "reverse flyes", "rear fly",
        ],
        "бицепс": [
            "бицуха", "подъем на бицепс", "сгибания на бицепс",
            "biceps", "biceps curl", "bicep curl", "curls", "arm curl",
        ],
        "трицепс": [
            "трицуха", "разгибания на трицепс", "французский жим",
            "triceps", "tricep", "pushdown", "triceps pushdown", "triceps extension",
        ],
        "разгибания ног": [
            "квадрицепс", "квадры", "передняя поверхность бедра",
            "leg extension", "leg extensions", "quads", "quad extension",
        ],
        "сгибания ног": [
            "бицепс бедра", "задняя поверхность бедра",
            "leg curl", "leg curls", "hamstrings", "hamstring curl",
        ],
        "бабочка": [
            "пек дек", "сведения", "сведение рук", "разводка",
            "pec deck", "chest fly", "flys", "flyes", "butterfly", "pec fly",
        ],
        "жим в тренажере": [
            "жим сидя", "грудной тренажер", "жим тренажер",
            "chest press", "machine press", "machine chest press", "seated chest press",
        ],
        "подтягивания грав": [
            "подтягивания", "гравитрон", "гравитон", "подтяги",
            "pull ups", "pull up", "pullups", "assisted pull ups", "chin ups",
        ],
    ]
}

// MARK: - Произносимые названия

enum ExerciseVoiceNames {
    /// Каталог хранит русские сокращения («Жим гор.»), которые Siri слышит
    /// плохо, а на англоязычном телефоне ещё и не понимает. Голосовой слой
    /// показывает и слушает развёрнутый вариант на нужном языке.
    static func spoken(for name: String, in language: VoiceLanguage = .ru) -> String {
        let key = VoiceText.tokens(name).joined(separator: " ")
        switch language {
        case .ru: return expandedRU[key] ?? name
        case .en: return english[key] ?? expandedRU[key] ?? name
        }
    }

    private static let expandedRU: [String: String] = [
        "жим гор": "Жим горизонтальный",
        "тяга верт": "Тяга вертикальная",
        "тяга горизонт": "Тяга горизонтальная",
        "подтягивания грав": "Подтягивания в гравитроне",
    ]

    private static let english: [String: String] = [
        "жим ногами": "Leg press",
        "жим гор": "Bench press",
        "тяга верт": "Lat pulldown",
        "тяга горизонт": "Seated row",
        "дельты": "Lateral raises",
        "задняя дельта": "Rear delt fly",
        "бицепс": "Biceps curl",
        "трицепс": "Triceps pushdown",
        "разгибания ног": "Leg extension",
        "сгибания ног": "Leg curl",
        "бабочка": "Chest fly",
        "жим в тренажере": "Machine chest press",
        "подтягивания грав": "Assisted pull ups",
    ]
}

// MARK: - Проговаривание ответа

enum VoicePhrasing {
    /// «80», «82,5» / «82.5» — целые без хвоста, дробные с разделителем языка
    /// (его синтез речи читает как дробь).
    static func weight(_ value: Double, in language: VoiceLanguage) -> String {
        if value == value.rounded() {
            return String(Int(value))
        }
        let formatted = String(format: "%.1f", value)
        return language == .ru ? formatted.replacingOccurrences(of: ".", with: ",") : formatted
    }

    static func set(weight: Double, reps: Int, in language: VoiceLanguage) -> String {
        let value = self.weight(weight, in: language)
        return language == .ru ? "\(value) на \(reps)" : "\(value) by \(reps)"
    }

    static func ordinal(_ number: Int, in language: VoiceLanguage) -> String {
        let words =
            language == .ru
            ? [
                "первый", "второй", "третий", "четвертый", "пятый", "шестой",
                "седьмой", "восьмой", "девятый", "десятый", "одиннадцатый", "двенадцатый",
            ]
            : [
                "first", "second", "third", "fourth", "fifth", "sixth",
                "seventh", "eighth", "ninth", "tenth", "eleventh", "twelfth",
            ]
        guard number >= 1, number <= words.count else {
            return language == .ru ? "\(number)-й" : "\(number)"
        }
        return words[number - 1]
    }

    /// Счётное слово после «из» / «of»: синтез речи читает цифру в этой позиции
    /// именительным падежом, и русская фраза звучит сломанной.
    static func countWord(_ number: Int, in language: VoiceLanguage) -> String {
        let words =
            language == .ru
            ? [
                "одного", "двух", "трех", "четырех", "пяти", "шести",
                "семи", "восьми", "девяти", "десяти", "одиннадцати", "двенадцати",
            ]
            : [
                "one", "two", "three", "four", "five", "six",
                "seven", "eight", "nine", "ten", "eleven", "twelve",
            ]
        guard number >= 1, number <= words.count else { return "\(number)" }
        return words[number - 1]
    }

    static func capitalized(_ text: String) -> String {
        guard let first = text.first else { return text }
        return first.uppercased() + text.dropFirst()
    }

    static func plural(_ count: Int, _ one: String, _ few: String, _ many: String) -> String {
        let mod100 = abs(count) % 100
        let mod10 = abs(count) % 10
        if (11...14).contains(mod100) { return many }
        if mod10 == 1 { return one }
        if (2...4).contains(mod10) { return few }
        return many
    }

    static func sets(_ count: Int, in language: VoiceLanguage) -> String {
        language == .ru
            ? "\(count) \(plural(count, "подход", "подхода", "подходов"))"
            : "\(count) \(count == 1 ? "set" : "sets")"
    }

    static func exercises(_ count: Int, in language: VoiceLanguage) -> String {
        language == .ru
            ? "\(count) \(plural(count, "упражнение", "упражнения", "упражнений"))"
            : "\(count) \(count == 1 ? "exercise" : "exercises")"
    }

    /// Короткая дата для реплики: русский формат берётся из общего `DateTools`,
    /// английский собирается здесь, чтобы не тащить локаль в весь клиент.
    static func date(_ iso: String, in language: VoiceLanguage) -> String {
        guard language == .en else { return DateTools.short(iso) }
        return englishDateFormatter.string(from: DateTools.date(from: iso))
    }

    private static let englishDateFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.setLocalizedDateFormatFromTemplate("d MMM")
        return formatter
    }()
}
