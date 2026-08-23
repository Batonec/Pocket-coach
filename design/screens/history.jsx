// Screen 4: History — vertical list of workouts (date-rail card variant).

const HistoryCard = ({ workout, accent, canNote = false }) => {
  const [day, month, dow] = workout.date.split(/\s*·\s*| /);
  return (
    <div style={{
      background: '#FBFAF7',
      border: '0.5px solid rgba(14,15,18,0.08)',
      borderRadius: 20, overflow: 'hidden',
      display: 'flex',
      boxShadow: '0 1px 0 rgba(14,15,18,0.02), 0 8px 22px -14px rgba(14,15,18,0.10)',
    }}>
      {/* date rail — light, paper tone so the screen reads as a calm list */}
      <div style={{
        flex: '0 0 64px',
        background: 'rgba(14,15,18,0.045)',
        borderRight: '0.5px solid rgba(14,15,18,0.08)',
        padding: '14px 6px',
        display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'space-between',
      }}>
        <div style={{ textAlign: 'center' }}>
          <div className="t-display t-num" style={{ fontSize: 28, lineHeight: 1, color: '#0E0F12', letterSpacing: -0.04 }}>{day}</div>
          <div className="t-label" style={{ marginTop: 2 }}>{month}</div>
        </div>
        <div style={{ width: 22, height: 0.5, background: 'rgba(14,15,18,0.10)', margin: '4px 0' }}/>
        <div style={{ textAlign: 'center' }}>
          <div className="t-label" style={{ color: accent }}>{dow.toUpperCase()}</div>
          <div className="t-label-xs" style={{ marginTop: 2 }}>{workout.dur}</div>
        </div>
      </div>
      {/* exercises */}
      <div style={{ flex: 1, padding: '10px 14px', minWidth: 0 }}>
        {workout.items.map((it, i) => {
          const ex = EXERCISES[it.ex];
          return (
            <div key={i} style={{
              padding: '6.5px 0',
              borderBottom: i < workout.items.length - 1 ? '0.5px solid rgba(14,15,18,0.07)' : 'none',
            }}>
              <div style={{ display: 'flex', alignItems: 'baseline', gap: 10 }}>
                <div style={{ fontSize: 13, fontWeight: 600, color: '#0E0F12', flex: '0 0 72px', letterSpacing: -0.15, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                  {ex.short}
                </div>
                <div className="t-num" style={{ flex: 1, fontSize: 12, color: '#2E3138', textAlign: 'right', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                  {it.sets}
                </div>
              </div>
              {/* заметка к подходу — сводка уже умеет её показывать */}
              {it.note && (
                <div style={{ display: 'flex', gap: 6, marginTop: 3 }}>
                  <div style={{ width: 2, borderRadius: 1, background: 'rgba(14,15,18,0.14)', flexShrink: 0 }}/>
                  <div style={{ fontSize: 11, lineHeight: 1.35, color: '#A8ACB4', letterSpacing: -0.1, textWrap: 'pretty' }}>{it.note}</div>
                </div>
              )}
            </div>
          );
        })}
        {/* поздний вход: заметку можно дописать с карточки */}
        {canNote && !workout.note && (
          <button className="row-tap" style={{
            width: '100%', marginTop: 6, height: 34, borderRadius: 12,
            border: '1px dashed rgba(14,15,18,0.16)', background: 'transparent',
            display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 7,
            fontSize: 11.5, color: '#6E727B', fontWeight: 600,
          }}>
            <svg width="12" height="12" viewBox="0 0 16 16">
              <path d="M11.4 2.6l2 2L6 12l-2.6.6L4 10l7.4-7.4z" stroke="#6E727B" strokeWidth="1.5" fill="none" strokeLinejoin="round"/>
            </svg>
            Как прошло, что мешало
          </button>
        )}
        {/* заметка к тренировке */}
        {workout.note && (
          <div style={{ marginTop: 4, paddingTop: 8, borderTop: '0.5px solid rgba(14,15,18,0.07)', display: 'flex', gap: 8 }}>
            <svg width="13" height="13" viewBox="0 0 16 16" style={{ flexShrink: 0, marginTop: 1 }}>
              <path d="M11.4 2.6l2 2L6 12l-2.6.6L4 10l7.4-7.4z" stroke="#A8ACB4" strokeWidth="1.5" fill="none" strokeLinejoin="round"/>
            </svg>
            <div style={{ fontSize: 12, lineHeight: 1.4, color: '#6E727B', letterSpacing: -0.1, textWrap: 'pretty' }}>{workout.note}</div>
          </div>
        )}
      </div>
    </div>
  );
};

const HistoryScreen = ({ accent = '#FF4D1F', signals = [], feed = null, gap = false, overlay = null, dim = false, noteAffordance = false }) => {
  const items = feed || HISTORY.map(w => ({ type: 'workout', ...w }));
  // поздний вход показываем на свежайшей тренировке БЕЗ заметки
  const lateIdx = noteAffordance ? items.findIndex(x => x.type === 'workout' && !x.note) : -1;
  // critical/warn поднимаются выше плашки статистики; остальные — под ней.
  const top = signals.filter(id => ['critical', 'warn'].includes(SIGNALS[id].sev));
  const rest = signals.filter(id => !top.includes(id));
  return (
  <PhoneShell dim={dim}>
    <StatusBar />
    <TopBar
      title="История"
      sub="Тренировки · 24 за 90 дней"
      pills={[
        { icon: <span style={{ width:6, height:6, borderRadius:3, background:'#1F9D6B', display:'inline-block' }}/>, label: 'UID 3' },
      ]}
      accent={accent}
    />
    <div style={{ position: 'absolute', inset: '170px 14px 96px 14px', overflow: 'hidden' }}>
      {top.length > 0 && <SignalStack ids={top} accent={accent} />}

      {/* Tap-to-Progress strip. This is the only entry point to the Progress screen
          now that it's been removed from the bottom tab bar. */}
      <button className="liquid-glass row-tap" style={{
        width: '100%', textAlign: 'left',
        borderRadius: 20, padding: '12px 14px', marginBottom: 10,
        display: 'flex', alignItems: 'center', gap: 12,
      }}>
        <div>
          <div className="t-display" style={{ fontSize: 26, lineHeight: 1, color: '#0E0F12' }}>
            12 <span style={{ fontSize: 13, color: '#6E727B', fontWeight: 600 }}>тренировок</span>
          </div>
          <div style={{ fontSize: 12, color: '#6E727B', marginTop: 2 }}>За последние 4 недели</div>
        </div>
        <div style={{ flex: 1 }}/>
        {/* 28 day dots */}
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(7, 8px)', gap: 3 }}>
          {Array.from({ length: 28 }).map((_, i) => {
            const on = [0,2,4,5,7,9,11,12,14,16,18,20].includes(i);
            return <div key={i} style={{ width: 8, height: 8, borderRadius: 2, background: on ? accent : 'rgba(14,15,18,0.08)' }}/>;
          })}
        </div>
        {/* Affordance — chevron + tiny "Прогресс" label */}
        <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 3, marginLeft: 2 }}>
          <svg width="14" height="14" viewBox="0 0 14 14" style={{ color: accent }}>
            <path d="M5 2l5 5-5 5" stroke="currentColor" strokeWidth="2" fill="none" strokeLinecap="round" strokeLinejoin="round"/>
          </svg>
          <div className="t-label-xs" style={{ color: accent, fontSize: 8.5 }}>Прогресс</div>
        </div>
      </button>

      {/* Сигналы тренера — между плашкой статистики и карточкой плана.
          Дефолт — пусто; максимум 1, второй слот только при critical. */}
      {rest.length > 0 && <SignalStack ids={rest} accent={accent} />}

      {/* AI recommendation for the next workout — styled as a future workout in
          the same family as the history cards below. */}
      <div style={{ marginBottom: 12 }}>
        <CoachCompact accent={accent} state="ready" />
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
        {items.map((it, i) => it.type === 'event'
          ? <EventCard key={i} ev={it} accent={accent} />
          : (
            <React.Fragment key={i}>
              <HistoryCard workout={it} accent={accent} canNote={i === lateIdx} />
              {/* подсказка живёт ровно в разрыве — на дате с тренировкой её нет */}
              {gap && i === 0 && <GapPrompt days={9} accent={accent} />}
            </React.Fragment>
          )
        )}
      </div>
    </div>
    <TabBar active="history" accent={accent} />
    {overlay}
  </PhoneShell>
  );
};

Object.assign(window, { HistoryScreen });
