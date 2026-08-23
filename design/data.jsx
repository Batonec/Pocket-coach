// Exercise catalog (Russian names matching the original app) + sample workout history.
// All values are static for the design — no real backend.

const MAIN_SIX = ['bench', 'legpress', 'pulldown', 'shoulders', 'curl', 'tricep'];

const EXERCISES = {
  bench:    { id: 'bench',    name: 'Жим в тренажере', short: 'Жим тр.',   muscle: 'Грудь',  glyph: 'bench' },
  legpress: { id: 'legpress', name: 'Жим ногами',       short: 'Ноги',     muscle: 'Ноги',   glyph: 'legs' },
  pulldown: { id: 'pulldown', name: 'Тяга верт.',       short: 'Тяга в.',  muscle: 'Спина',  glyph: 'lat' },
  shoulders:{ id: 'shoulders',name: 'Дельты',           short: 'Дельты',   muscle: 'Плечи',  glyph: 'delts' },
  curl:     { id: 'curl',     name: 'Бицепс',           short: 'Бицепс',   muscle: 'Руки',   glyph: 'biceps' },
  tricep:   { id: 'tricep',   name: 'Трицепс',          short: 'Трицепс',  muscle: 'Руки',   glyph: 'triceps' },
  row:      { id: 'row',      name: 'Тяга горизонт.',   short: 'Тяга г.',  muscle: 'Спина',  glyph: 'row' },
  fly:      { id: 'fly',      name: 'Бабочка',          short: 'Бабочка',  muscle: 'Грудь',  glyph: 'fly' },
  legext:   { id: 'legext',   name: 'Разгибания ног',   short: 'Разг. н.', muscle: 'Ноги',   glyph: 'legext' },
  legcurl:  { id: 'legcurl',  name: 'Сгибания ног',     short: 'Сгиб. н.', muscle: 'Ноги',   glyph: 'legcurl' },
  pullup:   { id: 'pullup',   name: 'Подтягивания гр.', short: 'Подтяг.',  muscle: 'Спина',  glyph: 'pullup' },
};

// Today's planned + completed sets. `target` is what the app suggests next.
const TODAY = [
  { ex: 'bench',     prev: '65кг ×12×2, 8', sets: [{ w: 65, r: 12, e: 1 }, { w: 65, r: 12, e: 1 }, { w: 65, r: 8, e: 2 }], target: '13×2, 9', status: 'planned' },
  { ex: 'legpress',  prev: '110кг ×10, 8, 7', sets: [{ w: 120, r: 11, e: 1 }, { w: 120, r: 9, e: 2 }, { w: 120, r: 8, e: 2 }], target: '12, 10, 9', status: 'planned' },
  { ex: 'pulldown',  prev: '70кг ×12, 10, 6', sets: [{ w: 75, r: 12, e: 1 }, { w: 75, r: 12, e: 1 }, { w: 75, r: 6, e: 2 }], target: '13×2, 7', status: 'active' },
  { ex: 'shoulders', prev: '30кг ×12, 25×18', sets: [{ w: 30, r: 12, e: 1 }, { w: 25, r: 19, e: 0 }], target: '13 · 20', status: 'planned' },
  { ex: 'curl',      prev: '20кг ×13', sets: [{ w: 20, r: 14, e: 2 }], target: '15', status: 'planned' },
  { ex: 'tricep',    prev: '15кг ×18', sets: [{ w: 15, r: 19, e: 0 }], target: '20', status: 'planned' },
];

const HISTORY = [
  { date: '05 мая · Вт', dur: '52 мин', items: [
    { ex: 'legpress', sets: '120кг × 11, 9, 8' },
    { ex: 'bench',    sets: '65кг × 12, 12, 8' },
    { ex: 'pulldown', sets: '75кг × 12, 12, 6 😣' },
    { ex: 'shoulders',sets: '30×12 · 25×19' },
    { ex: 'tricep',   sets: '15кг × 19' },
  ]},
  { date: '02 мая · Сб', dur: '1ч 04', items: [
    { ex: 'pulldown', sets: '60×12 · 75×12, 8 😣' },
    { ex: 'bench',    sets: '65кг × 12, 10 😣' },
    { ex: 'legpress', sets: '120кг × 10, 8, 8 😣' },
    { ex: 'tricep',   sets: '15кг × 18 😣' },
    { ex: 'shoulders',sets: '30×8 · 20×8' },
  ]},
  { date: '25 апр · Сб', dur: '58 мин', items: [
    { ex: 'legpress', sets: '100кг × 15, 15, 15 😣' },
    { ex: 'shoulders',sets: '17,5×18 · 20×11, 11, 9 😣' },
    { ex: 'pulldown', sets: '60×12, 12 · 75×12 · 70×10 😣' },
    { ex: 'tricep',   sets: '15кг × 15' },
  ]},
];

// ── События: период без тренировок + свободный текст. Типа в данных нет.
// end === null → событие открыто («идёт прямо сейчас»).
const EVENTS = {
  ill:    { start: '13 авг', end: '19 авг', days: 7, text: 'Болел, температура под 38, лежал всю неделю' },
  shoulder:{ start: '10 авг', end: '10 авг', days: 1, text: 'Плечо после жима стоя, решил не рисковать' },
  trip:   { start: '05 авг', end: '09 авг', days: 5, text: 'Командировка, зала не было' },
  open:   { start: '21 авг', end: null,     days: 3, text: 'Простуда' },
  short:  { start: '24 июл', end: '24 июл', days: 1, text: 'Болезнь' },
  long:   { start: '01 авг', end: '11 авг', days: 11, text: 'Сначала отравился на выезде, потом сразу командировка — в отеле был только кардио-зал, штанги не было вообще. Вернулся разбитым, спина ныла всю неделю' },
};

const PRESETS = ['Болезнь', 'Травма', 'Поездка', 'Не спал', 'Зал закрыт'];

// Августовская лента: тренировки + события вперемешку, новые сверху.
const mkW = (date, dur, items, note) => ({ type: 'workout', date, dur, items, note });
const mkE = (k) => ({ type: 'event', ...EVENTS[k] });

const AUG_SETS = {
  a: [ { ex: 'bench', sets: '65кг × 12, 12, 8' }, { ex: 'pulldown', sets: '75кг × 12, 12, 6 😣' }, { ex: 'tricep', sets: '15кг × 19', note: 'канат вместо прямой ручки, на прямой болят локти' } ],
  b: [ { ex: 'legpress', sets: '120кг × 11, 9, 8' }, { ex: 'shoulders', sets: '30×12 · 25×19' }, { ex: 'bench', sets: '65кг × 12, 10 😣' } ],
  c: [ { ex: 'pulldown', sets: '60×12 · 75×12, 8 😣' }, { ex: 'legpress', sets: '100кг × 15, 15, 15 😣' } ],
};

const FEED_AUG = [
  mkW('21 авг · Чт', '48 мин', AUG_SETS.a, 'Спал 4 часа, всё шло тяжело — веса ниже обычного'),
  mkE('ill'),        // 13–19 авг
  mkW('12 авг · Ср', '1ч 02', AUG_SETS.c),
  mkE('shoulder'),   // 10 авг
  mkE('trip'),       // 05–09 авг
  mkW('04 авг · Пн', '51 мин', AUG_SETS.b),
  mkW('01 авг · Пт', '44 мин', AUG_SETS.c),
];

// Открытое событие сверху ленты
const FEED_OPEN = [ mkE('open'), mkW('20 авг · Чт', '48 мин', AUG_SETS.b), mkE('shoulder'), mkW('12 авг · Ср', '1ч 02', AUG_SETS.c) ];

// Два события подряд
const FEED_CHAIN = [
  mkW('27 авг · Чт', '54 мин', AUG_SETS.a),
  { type: 'event', start: '20 авг', end: '26 авг', days: 7, text: 'Командировка сразу после больничного, зала не было' },
  mkE('ill'),
  mkW('12 авг · Ср', '1ч 02', AUG_SETS.c),
];

// Долгий перерыв: событий больше, чем тренировок
const FEED_SPARSE = [
  mkE('open'),
  mkE('ill'),
  mkW('12 авг · Ср', '1ч 02', AUG_SETS.c),
  mkE('trip'),
  { type: 'event', start: '02 авг', end: '03 авг', days: 2, text: 'Зал закрыт на профилактику' },
];

// Крайние случаи текста
const FEED_EDGE = [
  mkW('27 авг · Чт', '54 мин', AUG_SETS.a),
  mkE('long'),
  mkW('31 июл · Чт', '49 мин', AUG_SETS.b),
  mkE('short'),
];

// Per-exercise progress points (week index, top set weight×reps "load score")
const PROGRESS_BENCH = [
  { d: '12.03', w: 55, r: 10, score: 5.5 },
  { d: '19.03', w: 55, r: 12, score: 6.6 },
  { d: '26.03', w: 60, r: 10, score: 6.0 },
  { d: '02.04', w: 60, r: 12, score: 7.2 },
  { d: '09.04', w: 60, r: 14, score: 8.4 },
  { d: '18.04', w: 65, r: 12, score: 7.8 },
  { d: '25.04', w: 65, r: 13, score: 8.45 },
  { d: '02.05', w: 65, r: 12, score: 7.8 },
  { d: '11.05', w: 65, r: 13, score: 8.45 },
];

// Waist history (cm) — второй контур замеров, фаза набора, жёсткий лимит 88.0
const WAISTS = [
  { d: '09.06', w: 85.4 },
  { d: '16.06', w: 85.8 },
  { d: '23.06', w: 86.0 },
  { d: '30.06', w: 86.4 },
  { d: '07.07', w: 87.2 },
  { d: '14.07', w: 87.6 },
  { d: '21.07', w: 88.2 },
  { d: '02.08', w: 88.5 },
];

// Body weight history (last 12 weeks)
const WEIGHTS = [
  { d: '17.02', w: 84.2 },
  { d: '24.02', w: 84.0 },
  { d: '03.03', w: 83.6 },
  { d: '10.03', w: 83.4 },
  { d: '17.03', w: 83.1 },
  { d: '24.03', w: 82.8 },
  { d: '31.03', w: 82.9 },
  { d: '07.04', w: 82.4 },
  { d: '14.04', w: 82.1 },
  { d: '21.04', w: 81.9 },
  { d: '28.04', w: 81.6 },
  { d: '05.05', w: 81.4 },
  { d: '11.05', w: 81.2 },
];

// Main-six ring progress (% of weekly target hit this week)
const RING_PROGRESS = {
  bench: 0.85, legpress: 1.0, pulldown: 0.62,
  shoulders: 0.4, curl: 0.12, tricep: 0.08,
};

// ── Weekly working-set volume per muscle group (last 7 days) ──
// `sets` = working sets counted on the exercise's primary muscle.
// `min`/`max` = trainer's target range ("enough" .. "too much").
// Status: sets<min = недобор · min..max = в диапазоне · sets>max = перебор
const VOLUME_GROUPS = [
  { name: 'Грудь',            sets: 9,  min: 10, max: 16 },
  { name: 'Спина',            sets: 12, min: 10, max: 16 },
  { name: 'Квадрицепс/ягод.', sets: 8,  min: 10, max: 16 },
  { name: 'Дельты',           sets: 7,  min: 6,  max: 12 },
  { name: 'Бицепс',           sets: 5,  min: 4,  max: 8  },
  { name: 'Трицепс',          sets: 6,  min: 4,  max: 8  },
  { name: 'Бицепс бедра',     sets: 2,  min: 5,  max: 10 },
];

// ── Discipline: plan adherence for the selected period (fact vs plan) ──
// pct = выполненные/запланированные подходы (done capped at planned, so
// "extra" sets never push above 100%). Color: ≥80 ok · 50–79 warn · <50 bad.
const DISCIPLINE = {
  data: { pct: 82,  doneSets: 31, planSets: 38, workouts: 3, skipped: 2 },
  low:  { pct: 44,  doneSets: 12, planSets: 27, workouts: 2, skipped: 5 },
  high: { pct: 100, doneSets: 34, planSets: 34, workouts: 3, skipped: 0 },
};

// Short 2–3 letter tags for the muscle-map equalizer on the Week card.
const GROUP_TAG = {
  'Грудь': 'ГРД', 'Спина': 'СПН', 'Квадрицепс/ягод.': 'КВД',
  'Дельты': 'ДЛТ', 'Бицепс': 'БЦП', 'Трицепс': 'ТРЦ', 'Бицепс бедра': 'ЗБ',
};

// "Good week" variant — every group nudged into its target range.
const VOLUME_GROUPS_GOOD = VOLUME_GROUPS.map(g => (
  g.sets < g.min ? { ...g, sets: g.min + 2 } : g
));

// ── Week meta: the training week the Week screen summarizes ──
const WEEK = {
  range: '09–15 ИЮНЯ',
  rangeShort: '9–15 июн',
  prevRange: '2–8 июн',
};

// ── recommendation_log — the coach's generation journal ──
// Every time the AI builds/updates the next-workout plan we write a row:
// what triggered it, the focus + load it chose, which model, how many
// workouts it was built on, and — once that session is logged — how the
// plan held up against reality (fact vs plan). This is the "invisible work"
// the brief wants surfaced. Most recent first; the top row is the live plan.
const RECOMMENDATION_LOG = [
  {
    id: 'g4', date: '11 ИЮН', dow: 'СР', time: '21:14',
    trigger: 'после тренировки', triggerNote: 'добавлена сессия 11 июн',
    focus: 'Верх + низ · мягкий вход после перерыва', load: 'medium',
    basedOn: 56, model: 'claude-opus-4-8',
    plan: { sets: 17, ex: 6 }, result: null, status: 'active',
    rationale: 'С последней тренировки прошло 13 дней — большой перерыв, поэтому не стал поднимать веса до майских пиков (120 кг жим ногами, 80 кг тяга). Выбрал среднюю нагрузку: после простоя резкий скачок повышает риск травмы. Сначала качественная средняя сессия, затем возврат к прогрессии.',
  },
  {
    id: 'g3', date: '05 ИЮН', dow: 'ЧТ', time: '20:02',
    trigger: 'после тренировки', triggerNote: 'добавлена сессия 05 июн',
    focus: 'Спина + дельты, рабочая нагрузка', load: 'medium',
    basedOn: 54, model: 'claude-opus-4-8',
    plan: { sets: 19, ex: 6 }, result: { done: 17, adherence: 89, note: 'в основном легко' }, status: 'done',
    rationale: 'Спина и дельты отставали по недельному объёму — добавил по подходу на каждую. Грудь и ноги оставил на месте, чтобы не копить усталость перед выходными.',
  },
  {
    id: 'g2', date: '02 ИЮН', dow: 'ПН', time: '08:40',
    trigger: 'ручное обновление', triggerNote: 'вы нажали «Обновить»',
    focus: 'Ноги + грудь, лёгкая после разгрузки', load: 'light',
    basedOn: 53, model: 'claude-opus-4-8',
    plan: { sets: 16, ex: 5 }, result: { done: 12, adherence: 75, note: '1 упр. пропущено' }, status: 'done',
    rationale: 'Запросили лёгкую сессию. Снизил веса на 10–15 % от рабочих и сократил число подходов — разгрузка перед новым циклом.',
  },
  {
    id: 'g1', date: '29 МАЯ', dow: 'ЧТ', time: '19:30',
    trigger: 'после тренировки', triggerNote: 'добавлена сессия 29 мая',
    focus: 'Полный объём, средняя нагрузка', load: 'medium',
    basedOn: 52, model: 'claude-opus-4-8',
    plan: { sets: 18, ex: 6 }, result: { done: 18, adherence: 100, note: 'выполнено полностью' }, status: 'done',
    rationale: 'Стандартная прогрессия: +2.5 кг там, где прошлые подходы дались легко (≤ RPE 8). Объём по группам держался в диапазоне — структуру не менял.',
  },
];

Object.assign(window, {
  MAIN_SIX, EXERCISES, TODAY, HISTORY, PROGRESS_BENCH, WEIGHTS, WAISTS, RING_PROGRESS,
  VOLUME_GROUPS, VOLUME_GROUPS_GOOD,
  EVENTS, PRESETS, FEED_AUG, FEED_OPEN, FEED_CHAIN, FEED_SPARSE, FEED_EDGE, GROUP_TAG, DISCIPLINE, WEEK, RECOMMENDATION_LOG,
});
