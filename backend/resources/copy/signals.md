<!--
Тексты баннеров «Совет тренера» — то, что видит атлет в приложении. Это НЕ
промпт: сюда модель не заглядывает, отсюда читает iOS-клиент.

Один текст = один фрагмент «## имя», слоты {{name}} подставляет coach_prompts.
Пороги, ранги severity, снузы и построение instance_key остаются в
coach_signals.py: правка текста здесь не меняет ни схлопывание, ни дисмиссы —
ключ эпизода собирается из фактов, а не из копирайта.

Нормативный дом порогов и жизненного цикла — docs/COACH_SIGNALS.md.

Три заголовка «обнови замеры» намеренно лежат тремя отдельными фрагментами, а
не одним шаблоном со слотом: это три разных предложения с разной структурой, и
подстановка существительного их бы сломала.

Про «дн.»: сокращение стоит намеренно. Склонялки числительных в проекте нет, а
«через 3 дня / через 5 дней» без неё не написать. Меняешь на полное слово —
сначала заводи склонение.
-->

## waist_limit_title
Талия {{waist}} см — у лимита {{limit}}

## waist_limit_body
Тренер предлагает мини-кат

## measurements_overdue_title
Советы по калориям на паузе

## measurements_overdue_body
Внеси {{what}} — вернутся

## measurements_due_title_both
Обнови замеры: вес и талия

## measurements_due_title_waist
Обнови талию — вес свежий

## measurements_due_title_weight
Обнови вес — талия свежая

## measurements_due_body
Через {{days}} советы по калориям встанут на паузу

## measurements_due_note
ещё пара точек — тренд оживёт

## weight_trend_stale_title
Взвесься — тренд веса не считается

## weight_trend_stale_body
Фаза {{goal}} рулит калориями по тренду: нужен замер каждые 3–4 дня

## weight_trend_collapsed_note
И взвесься: тренд веса не считается

## return_soon_title
Потренируйся до {{deadline}}

## return_soon_body
Иначе следующая сессия — возвратная, с облегчёнными весами

## return_mode_ready_title
Возвратная тренировка готова, облегчённый вход

## return_mode_pending_title
После перерыва нужен облегчённый старт

## return_mode_failed_body
План пока не готов — повтори генерацию

## return_mode_outdated_body
Текущий план не учитывает перерыв — обнови его

## return_mode_none_body
План пока не готов — сгенерируй его

## deload_title
Разгрузочная неделя

## deload_body
−30–40% объёма, веса рабочие

## deload_note
со следующей недели набор объёма заново

## report_title
Готов отчёт недели

## report_body_default
Итоги, ПР, вес и питание

## week_done_title
Неделя закрыта: {{pct}}% плана

## week_done_body
Так держать

## action_measurements
Замеры

## action_plan
План

## action_report
Отчёт

## action_retry
Повторить

## action_refresh
Обновить

## action_create
Создать

## noun_accusative_weight
вес

## noun_accusative_waist
талию

## joiner_and
 и 
