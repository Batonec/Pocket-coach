// Signal banners on «История» — server-state driven, max 1 (2 only with critical).

const SIG_TONES = (accent) => ({
  info:     { fg: '#2E3138', glyph: '#6E727B', bg: 'rgba(14,15,18,0.055)', edge: 'rgba(14,15,18,0.10)' },
  accent:   { fg: '#2E3138', glyph: accent,    bg: accent + '1F',          edge: accent + '3D' },
  warn:     { fg: '#2E3138', glyph: '#B87A12', bg: 'rgba(216,147,36,0.16)', edge: 'rgba(216,147,36,0.38)' },
  positive: { fg: '#2E3138', glyph: '#1F9D6B', bg: 'rgba(31,157,107,0.15)', edge: 'rgba(31,157,107,0.34)' },
});

const SigGlyph = ({ name, color, size = 17 }) => {
  const p = { stroke: color, strokeWidth: 1.9, fill: 'none', strokeLinecap: 'round', strokeLinejoin: 'round' };
  const g = {
    // весы: дуга + стрелка
    scale: <g><path d="M2.5 13.5a7 7 0 0 1 13 0" {...p}/><path d="M9 12.5 12.4 7.6" {...p}/></g>,
    // рулетка: линейка с насечками
    tape: <g><rect x="1.6" y="5.4" width="14.8" height="7.2" rx="2" {...p}/><path d="M5.4 5.4v2.6M9 5.4v3.6M12.6 5.4v2.6" {...p}/></g>,
    // возврат: стрелка по кругу
    back: <g><path d="M3 9a6 6 0 1 0 6-6 6 6 0 0 0-4.6 2.2" {...p}/><path d="M2.2 1.9 2.6 5.6l3.6-.5" {...p}/></g>,
    // разгрузка: волна
    wave: <g><path d="M1.6 10.6c1.6-3.4 3.2-3.4 4.8 0s3.2 3.4 4.8 0 3.2-3.4 4.8 0" {...p}/></g>,
    // отчёт: лист со строками
    doc: <g><rect x="3.2" y="2" width="11.6" height="14" rx="2" {...p}/><path d="M6.2 6.4h5.6M6.2 9.4h5.6M6.2 12.4h3.2" {...p}/></g>,
    check: <g><path d="M2.6 9.4 6.6 13.4 15 4.6" {...p} strokeWidth="2.2"/></g>,
  }[name];
  return <svg width={size} height={size} viewBox="0 0 18 18">{g}</svg>;
};

// One banner. `sev`: info | accent | warn | critical | positive
const SignalBanner = ({ sev = 'info', glyph = 'scale', title, body, note, cta, dismissable = true, accent = '#FF4D1F' }) => {
  const crit = sev === 'critical';
  const tone = SIG_TONES(accent)[crit ? 'info' : sev];
  const ink = crit ? '#fff' : '#0E0F12';
  const sub = crit ? 'rgba(255,255,255,0.80)' : '#6E727B';
  return (
    <div
      className={crit ? 'row-tap' : 'liquid-glass row-tap'}
      style={{
        position: 'relative', borderRadius: 20,
        padding: dismissable ? '11px 34px 11px 12px' : '11px 12px',
        minHeight: 58,
        display: 'flex', alignItems: 'flex-start', gap: 10,
        ...(crit ? {
          background: 'linear-gradient(180deg,#E05252 0%,#D23F3F 100%)',
          border: '0.5px solid rgba(255,255,255,0.22)',
          boxShadow: '0 1px 0 rgba(14,15,18,0.04), 0 16px 30px -14px rgba(220,72,72,0.55)',
        } : { border: `0.5px solid ${tone.edge}` }),
      }}>
      {/* glyph */}
      <div style={{
        flex: '0 0 30px', width: 30, height: 30, borderRadius: 10,
        background: crit ? 'rgba(255,255,255,0.20)' : tone.bg,
        display: 'flex', alignItems: 'center', justifyContent: 'center', marginTop: 1,
      }}>
        <SigGlyph name={glyph} color={crit ? '#fff' : tone.glyph} />
      </div>
      {/* copy */}
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: 13, fontWeight: 700, color: ink, letterSpacing: -0.2, lineHeight: 1.25, textWrap: 'pretty' }}>{title}</div>
        <div style={{ fontSize: 11.5, color: sub, lineHeight: 1.35, marginTop: 2, textWrap: 'pretty' }}>{body}</div>
        {note && (
          <div style={{ fontSize: 10.5, color: crit ? 'rgba(255,255,255,0.62)' : '#A8ACB4', lineHeight: 1.3, marginTop: 3, fontStyle: 'italic' }}>{note}</div>
        )}
      </div>
      {/* CTA */}
      {cta && (
        <div style={{ flex: '0 0 auto', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 3, alignSelf: 'center', paddingLeft: 2 }}>
          <svg width="14" height="14" viewBox="0 0 14 14" style={{ color: crit ? '#fff' : (sev === 'info' ? '#2E3138' : tone.glyph) }}>
            <path d="M5 2l5 5-5 5" stroke="currentColor" strokeWidth="2" fill="none" strokeLinecap="round" strokeLinejoin="round"/>
          </svg>
          <div className="t-label-xs" style={{ fontSize: 8.5, color: crit ? 'rgba(255,255,255,0.85)' : (sev === 'info' ? '#2E3138' : tone.glyph) }}>{cta}</div>
        </div>
      )}
      {/* dismiss — крестик, не свайп */}
      {dismissable && (
        <button style={{
          position: 'absolute', top: 4, right: 4, width: 26, height: 26, borderRadius: 13,
          display: 'flex', alignItems: 'center', justifyContent: 'center',
        }}>
          <svg width="9" height="9" viewBox="0 0 10 10">
            <path d="M1 1l8 8M9 1l-8 8" stroke={crit ? 'rgba(255,255,255,0.7)' : '#A8ACB4'} strokeWidth="1.6" strokeLinecap="round"/>
          </svg>
        </button>
      )}
    </div>
  );
};

// Утверждённая таксономия — тексты приходят с сервера, здесь для макета.
const SIGNALS = {
  measurements_due: { sev: 'info', glyph: 'scale', cta: 'Замеры',
    title: 'Обнови замеры: вес и талия',
    body: 'Через 3 дн. советы по калориям встанут на паузу',
    note: 'ещё пара точек — тренд оживёт' },
  measurements_due_long: { sev: 'info', glyph: 'scale', cta: 'Замеры',
    title: 'Обнови талию — вес свежий',
    body: 'Талия не обновлялась 12 дней. Через 2 дн. советы по калориям встанут на паузу: без талии не видно, куда идёт набор',
    note: 'ещё пара точек — тренд оживёт' },
  measurements_overdue: { sev: 'warn', glyph: 'scale', cta: 'Замеры',
    title: 'Советы по калориям на паузе',
    body: 'Внеси вес и талию — вернутся' },
  return_soon: { sev: 'warn', glyph: 'back', cta: 'План',
    title: 'Потренируйся до 16 августа',
    body: 'Иначе следующая сессия — возвратная, ~85–90% весов' },
  return_mode: { sev: 'accent', glyph: 'back', cta: 'План',
    title: 'Возвратная сессия уже облегчена',
    body: '~85–90% весов. Просто приди, догонять ничего не надо' },
  deload_week: { sev: 'accent', glyph: 'wave', cta: 'План',
    title: 'Разгрузочная неделя',
    body: '−30–40% объёма, веса рабочие' },
  weekly_report_ready: { sev: 'info', glyph: 'doc', cta: 'Отчёт',
    title: 'Готов отчёт недели',
    body: '4–10 августа · 92% плана' },
  week_done: { sev: 'positive', glyph: 'check',
    title: 'Неделя закрыта: 92% плана',
    body: 'Так держать' },
  waist_limit: { sev: 'critical', glyph: 'tape', cta: 'Замеры', dismissable: false,
    title: 'Талия 88.5 см — у лимита 88',
    body: 'Тренер предлагает мини-кат' },
};

const SignalStack = ({ ids = [], accent = '#FF4D1F' }) => (
  <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginBottom: 10 }}>
    {ids.map(id => <SignalBanner key={id} accent={accent} {...SIGNALS[id]} />)}
  </div>
);

// Появление / гашение: вставка сверху, fade+collapse по действию.
const SignalMotionDemo = ({ accent = '#FF4D1F' }) => {
  const [shown, setShown] = React.useState(true);
  return (
    <div style={{ width: 364, display: 'flex', flexDirection: 'column', gap: 14 }}>
      <div style={{
        overflow: 'hidden',
        maxHeight: shown ? 120 : 0,
        opacity: shown ? 1 : 0,
        transform: shown ? 'translateY(0)' : 'translateY(-8px)',
        transition: 'max-height 260ms cubic-bezier(.22,.61,.36,1), opacity 180ms ease, transform 260ms cubic-bezier(.22,.61,.36,1)',
      }}>
        <SignalBanner accent={accent} {...SIGNALS.measurements_due} />
      </div>
      <div style={{ display: 'flex', gap: 8 }}>
        <button onClick={() => setShown(false)} className="chip row-tap" style={{
          flex: 1, height: 44, borderRadius: 22, fontSize: 12.5, fontWeight: 700, color: '#0E0F12',
        }}>внести вес → гаснет</button>
        <button onClick={() => setShown(true)} className="row-tap" style={{
          flex: 1, height: 44, borderRadius: 22, fontSize: 12.5, fontWeight: 700,
          background: '#0E0F12', color: '#fff',
        }}>новый эпизод → вставка</button>
      </div>
      <div style={{ fontSize: 11, color: '#6E727B', lineHeight: 1.45, textWrap: 'pretty' }}>
        Гаснет по действию (запись веса), а не по переходу на экран: optimistic hide в тот же кадр,
        затем refetch за истиной. Вставка — сверху, лента под баннером сдвигается тем же движением.
      </div>
    </div>
  );
};

Object.assign(window, { SignalBanner, SignalStack, SignalMotionDemo, SIGNALS, SigGlyph });
