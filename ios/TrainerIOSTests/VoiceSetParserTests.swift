import XCTest

@testable import TrainerIOS

final class VoiceSetParserTests: XCTestCase {
    private let catalog = TestFixtures.catalog

    private func parse(_ phrase: String) -> ParsedVoiceSet {
        VoiceSetParser.parse(phrase, catalog: catalog)
    }

    // MARK: - Базовая фраза

    func testParsesCanonicalPhrase() {
        let parsed = parse("Жим ногами 80 кг на 10 раз, тяжело")

        XCTAssertEqual(parsed.exerciseID, 8)
        XCTAssertEqual(parsed.weight, 80)
        XCTAssertEqual(parsed.reps, 10)
        XCTAssertEqual(parsed.effort, .hard)
    }

    func testParsesPhraseWithoutUnits() {
        let parsed = parse("жим ногами 80 на 10")

        XCTAssertEqual(parsed.exerciseID, 8)
        XCTAssertEqual(parsed.weight, 80)
        XCTAssertEqual(parsed.reps, 10)
        XCTAssertNil(parsed.effort)
    }

    func testParsesCommandWrapperAndAppName() {
        let parsed = parse("Добавь в покет коуч подход жим ногами 80 на 10")

        XCTAssertEqual(parsed.exerciseID, 8)
        XCTAssertEqual(parsed.weight, 80)
        XCTAssertEqual(parsed.reps, 10)
    }

    func testParsesMultiplicationSign() {
        let parsed = parse("тяга вертикальная 55х12")

        XCTAssertEqual(parsed.exerciseID, 9)
        XCTAssertEqual(parsed.weight, 55)
        XCTAssertEqual(parsed.reps, 12)
    }

    func testParsesReversedOrder() {
        let parsed = parse("12 повторений 55 килограмм тяга горизонтальная")

        XCTAssertEqual(parsed.exerciseID, 10)
        XCTAssertEqual(parsed.weight, 55)
        XCTAssertEqual(parsed.reps, 12)
    }

    // MARK: - Числа

    func testParsesSpelledOutNumbers() {
        let parsed = parse("жим ногами восемьдесят на десять")

        XCTAssertEqual(parsed.weight, 80)
        XCTAssertEqual(parsed.reps, 10)
    }

    func testParsesComposedSpelledOutNumber() {
        let parsed = parse("жим ногами восемьдесят два на двенадцать")

        XCTAssertEqual(parsed.weight, 82)
        XCTAssertEqual(parsed.reps, 12)
    }

    func testParsesFractionalWeight() {
        XCTAssertEqual(parse("бицепс 12,5 на 10").weight, 12.5)
        XCTAssertEqual(parse("бицепс двенадцать с половиной на 10").weight, 12.5)
    }

    func testSingleBareNumberBecomesRepsWhenSmall() {
        let parsed = parse("подтягивания 8")

        XCTAssertEqual(parsed.exerciseID, 4)
        XCTAssertEqual(parsed.reps, 8)
        XCTAssertNil(parsed.weight, "Вес должен остаться пустым и приехать из плана")
    }

    func testSingleBareNumberBecomesWeightWhenLarge() {
        let parsed = parse("жим ногами 80")

        XCTAssertEqual(parsed.weight, 80)
        XCTAssertNil(parsed.reps)
    }

    func testExplicitUnitBeatsMagnitudeHeuristic() {
        let parsed = parse("бицепс 15 килограмм")

        XCTAssertEqual(parsed.weight, 15)
        XCTAssertNil(parsed.reps)
    }

    // MARK: - Оценка тяжести

    func testEffortSynonyms() {
        XCTAssertEqual(parse("бицепс 20 на 10 тяжело").effort, .hard)
        XCTAssertEqual(parse("бицепс 20 на 10 тяжеловато").effort, .hard)
        XCTAssertEqual(parse("бицепс 20 на 10 легко").effort, .easy)
        XCTAssertEqual(parse("бицепс 20 на 10 нормально").effort, .ok)
        XCTAssertEqual(parse("бицепс 20 на 10 норм").effort, .ok)
    }

    func testEffortDoesNotEatExerciseName() {
        XCTAssertEqual(parse("тяга вертикальная 55 на 12 тяжело").exerciseID, 9)
    }

    // MARK: - Упражнения

    func testMatchesAbbreviatedCatalogNames() {
        XCTAssertEqual(parse("жим горизонтальный 60 на 12").exerciseID, 1)
        XCTAssertEqual(parse("тяга вертикальная 55 на 12").exerciseID, 9)
        XCTAssertEqual(parse("тяга горизонтальная 55 на 12").exerciseID, 10)
        XCTAssertEqual(parse("подтягивания 8 раз").exerciseID, 4)
    }

    func testMatchesColloquialSynonyms() {
        XCTAssertEqual(parse("жим лежа 60 на 12").exerciseID, 1)
        XCTAssertEqual(parse("верхний блок 55 на 12").exerciseID, 9)
        XCTAssertEqual(parse("нижний блок 55 на 12").exerciseID, 10)
        XCTAssertEqual(parse("гравитрон 8 раз").exerciseID, 4)
        XCTAssertEqual(parse("плечи 12 на 15").exerciseID, 13)
    }

    func testDistinguishesFlexionFromExtension() {
        XCTAssertEqual(parse("разгибания ног 45 на 12").exerciseID, 16)
        XCTAssertEqual(parse("сгибания ног 45 на 12").exerciseID, 15)
    }

    func testDistinguishesBicepsFromHamstrings() {
        XCTAssertEqual(parse("бицепс 20 на 10").exerciseID, 11)
        XCTAssertEqual(parse("бицепс бедра 45 на 12").exerciseID, 15)
    }

    func testAmbiguousPhraseReturnsCandidatesInsteadOfGuessing() {
        let parsed = parse("тяга 55 на 12")

        XCTAssertNil(parsed.exerciseID)
        XCTAssertEqual(Set(parsed.ambiguousIDs), [9, 10])
    }

    func testUnknownExerciseKeepsSpokenFragmentForReprompt() {
        let parsed = parse("становая тяга штанги 100 на 5")

        XCTAssertNil(parsed.exerciseID)
        XCTAssertTrue(parsed.spokenExercise.contains("становая"))
    }

    func testEmptyPhraseResolvesToNothing() {
        let parsed = parse("   ")

        XCTAssertNil(parsed.exerciseID)
        XCTAssertNil(parsed.weight)
        XCTAssertNil(parsed.reps)
    }

    // MARK: - Произносимые названия и склонения

    func testSpokenNamesExpandCatalogAbbreviations() {
        XCTAssertEqual(ExerciseVoiceNames.spoken(for: "Жим гор."), "Жим горизонтальный")
        XCTAssertEqual(ExerciseVoiceNames.spoken(for: "Тяга верт."), "Тяга вертикальная")
        XCTAssertEqual(ExerciseVoiceNames.spoken(for: "Бицепс"), "Бицепс")
    }

    func testSpokenNamesTranslateForEnglishSiri() {
        XCTAssertEqual(ExerciseVoiceNames.spoken(for: "Жим ногами", in: .en), "Leg press")
        XCTAssertEqual(ExerciseVoiceNames.spoken(for: "Жим гор.", in: .en), "Bench press")
        XCTAssertEqual(ExerciseVoiceNames.spoken(for: "Тяга горизонт.", in: .en), "Seated row")
    }

    func testWeightPhrasingUsesTheLanguageDecimalSeparator() {
        XCTAssertEqual(VoicePhrasing.weight(80, in: .ru), "80")
        XCTAssertEqual(VoicePhrasing.weight(82.5, in: .ru), "82,5")
        XCTAssertEqual(VoicePhrasing.weight(82.5, in: .en), "82.5")
    }

    func testRussianPluralsForSets() {
        XCTAssertEqual(VoicePhrasing.sets(1, in: .ru), "1 подход")
        XCTAssertEqual(VoicePhrasing.sets(3, in: .ru), "3 подхода")
        XCTAssertEqual(VoicePhrasing.sets(5, in: .ru), "5 подходов")
        XCTAssertEqual(VoicePhrasing.sets(11, in: .ru), "11 подходов")
        XCTAssertEqual(VoicePhrasing.sets(21, in: .ru), "21 подход")
    }

    func testEnglishPluralsForSets() {
        XCTAssertEqual(VoicePhrasing.sets(1, in: .en), "1 set")
        XCTAssertEqual(VoicePhrasing.sets(3, in: .en), "3 sets")
        XCTAssertEqual(VoicePhrasing.exercises(1, in: .en), "1 exercise")
        XCTAssertEqual(VoicePhrasing.exercises(4, in: .en), "4 exercises")
    }

    // MARK: - Английская фраза

    func testParsesEnglishCanonicalPhrase() {
        let parsed = parse("Leg press 80 kilos for 10 reps, hard")

        XCTAssertEqual(parsed.exerciseID, 8)
        XCTAssertEqual(parsed.weight, 80)
        XCTAssertEqual(parsed.reps, 10)
        XCTAssertEqual(parsed.effort, .hard)
    }

    func testParsesEnglishShorthand() {
        let parsed = parse("leg press 80 by 10")

        XCTAssertEqual(parsed.exerciseID, 8)
        XCTAssertEqual(parsed.weight, 80)
        XCTAssertEqual(parsed.reps, 10)
    }

    func testParsesEnglishCommandWrapper() {
        let parsed = parse("Add a set to pocket coach: bench press 60 by 12")

        XCTAssertEqual(parsed.exerciseID, 1)
        XCTAssertEqual(parsed.weight, 60)
        XCTAssertEqual(parsed.reps, 12)
    }

    func testMatchesEnglishExerciseNames() {
        XCTAssertEqual(parse("lat pulldown 55 by 12").exerciseID, 9)
        XCTAssertEqual(parse("seated row 55 by 12").exerciseID, 10)
        XCTAssertEqual(parse("leg extension 45 by 12").exerciseID, 16)
        XCTAssertEqual(parse("leg curl 45 by 12").exerciseID, 15)
        XCTAssertEqual(parse("pull ups 8 reps").exerciseID, 4)
        XCTAssertEqual(parse("lateral raises 12 by 15").exerciseID, 13)
        XCTAssertEqual(parse("machine chest press 50 by 10").exerciseID, 18)
        XCTAssertEqual(parse("chest fly 40 by 12").exerciseID, 17)
    }

    func testEnglishBicepsCurlIsNotALegCurl() {
        XCTAssertEqual(parse("biceps curl 20 by 10").exerciseID, 11)
        XCTAssertEqual(parse("curls 20 by 10").exerciseID, 11)
        XCTAssertEqual(parse("leg curls 45 by 12").exerciseID, 15)
    }

    func testEnglishEffortSynonyms() {
        XCTAssertEqual(parse("biceps 20 by 10 hard").effort, .hard)
        XCTAssertEqual(parse("biceps 20 by 10 heavy").effort, .hard)
        XCTAssertEqual(parse("biceps 20 by 10 easy").effort, .easy)
        XCTAssertEqual(parse("biceps 20 by 10 okay").effort, .ok)
    }

    func testParsesEnglishSpelledOutNumbers() {
        let parsed = parse("leg press eighty by ten")

        XCTAssertEqual(parsed.weight, 80)
        XCTAssertEqual(parsed.reps, 10)
    }

    func testEnglishHundredsAreMultiplicative() {
        XCTAssertEqual(parse("leg press two hundred by 5").weight, 200)
        XCTAssertEqual(parse("leg press one hundred twenty by 5").weight, 120)
    }

    func testEnglishHalfSuffix() {
        XCTAssertEqual(parse("biceps twelve and a half by 10").weight, 12.5)
    }

    func testPoundsAreConvertedToKilograms() {
        let parsed = parse("bench press 100 pounds by 10")

        XCTAssertEqual(parsed.weight, 45.5, "45.359… округляется до ближайшего полкило")
        XCTAssertTrue(parsed.convertedFromPounds)
        XCTAssertEqual(parsed.reps, 10)
    }

    func testKilogramsAreNotTreatedAsPounds() {
        let parsed = parse("bench press 100 kg by 10")

        XCTAssertEqual(parsed.weight, 100)
        XCTAssertFalse(parsed.convertedFromPounds)
    }

    func testAmbiguousEnglishPhraseReturnsCandidates() {
        let parsed = parse("press 80 by 10")

        XCTAssertNil(parsed.exerciseID)
        XCTAssertGreaterThan(parsed.ambiguousIDs.count, 1)
    }

    func testMixedScriptPhraseStillParses() {
        let parsed = parse("жим ногами 80 kg by 10")

        XCTAssertEqual(parsed.exerciseID, 8)
        XCTAssertEqual(parsed.weight, 80)
        XCTAssertEqual(parsed.reps, 10)
    }

    // MARK: - Определение языка

    func testLanguageFollowsTheScriptOfThePhrase() {
        XCTAssertEqual(VoiceLanguage.detected(in: "жим ногами 80 на 10", fallback: .en), .ru)
        XCTAssertEqual(VoiceLanguage.detected(in: "leg press 80 by 10", fallback: .ru), .en)
    }

    func testLanguageFallsBackWhenThereAreNoLetters() {
        XCTAssertEqual(VoiceLanguage.detected(in: "80 10", fallback: .en), .en)
        XCTAssertEqual(VoiceLanguage.detected(in: "80 10", fallback: .ru), .ru)
    }
}
