// События (периоды без тренировок) и заметки. Оба канала — чистый текст в промпт.
// Ничего не считается: никакой статистики по событиям в интерфейсе нет.

// ── Карточка события в ленте «Истории» ────────────────────────────────
// Та же порода, что карточка тренировки: тот же радиус, та же 64-px рельса.
// Но не бумага — незалитый блок с пунктиром: в ленте это буквально дырка,
// на которой оставили подпись. Тренировки остаются главным содержимым.
const EventRail = ({ ev, accent }) => {
  const open = ev.end === null;
  const range = !open && ev.start !== ev.end;
  const [d1, m1] = ev.start.split(' ');
  const d2 = open ? null : ev.end.split(' ')[0];
  return (
    <div style={{
      flex: '0 0 64px', padding: '13px 5px',
      borderRight: '1px dashed rgba(14,15,18,0.18)',
      display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'space-between',
    }}>
      <div style={{ textAlign: 'center' }}>
        {open ? (
          <div className="t-display t-num" style={{ fontSize: 19, lineHeight: 1, color: '#0E0F12', letterSpacing: -0.5 }}>
            <span style={{ fontSize: 12, color: '#6E727B' }}>с </span>{d1}
          </div>
        ) : (
          <div className="t-display t-num" style={{
            fontSize: range ? 16 : 28, lineHeight: 1, color: '#0E0F12', letterSpacing: range ? -0.6 : -0.04,
          }}>{range ? `${d1}–${d2}` : d1}</div>
        )}
        <div className="t-label" style={{ marginTop: 3 }}>{m1}</div>
      </div>
      <div style={{ width: 22, height: 0.5, background: 'rgba(14,15,18,0.10)', margin: '4px 0' }}/>
      <div className="t-label-xs" style={{
        color: open ? accent : '#A8ACB4', display: 'flex', alignItems: 'center', gap: 4, fontSize: 9,
      }}>
        {open && <span style={{ width: 5, height: 5, borderRadius: 3, background: accent, boxShadow: `0 0 0 3px ${accent}26` }}/>}
        {open ? 'ИДЁТ' : `${ev.days} ДН.`}
      </div>
    </div>
  );
};

const EventCard = ({ ev, accent = '#FF4D1F' }) => {
  const open = ev.end === null;
  return (
    <div style={{
      borderRadius: 20, overflow: 'hidden', display: 'flex',
      background: open ? `${accent}0A` : 'transparent',
      border: `1px dashed ${open ? accent + '59' : 'rgba(14,15,18,0.20)'}`,
    }}>
      <EventRail ev={ev} accent={accent} />
      <div style={{ flex: 1, minWidth: 0, padding: '12px 14px', display: 'flex', flexDirection: 'column', justifyContent: 'center', gap: 8 }}>
        <div style={{ fontSize: 13.5, lineHeight: 1.45, color: '#2E3138', letterSpacing: -0.15, textWrap: 'pretty' }}>{ev.text}</div>
        {open && (
          <div style={{ display: 'flex' }}>
            <button className="chip row-tap" style={{
              padding: '6px 12px', borderRadius: 999, fontSize: 11.5, fontWeight: 700, color: '#0E0F12',
            }}>Закончилось</button>
          </div>
        )}
      </div>
    </div>
  );
};

// ── Точка входа: подсказка в самой дырке ──────────────────────────────
// Свайп влево на «Истории» занят удалением, поэтому вход контекстный:
// строка появляется ровно там, где между тренировками разрыв ≥ 3 дн.
// Побочный эффект — интерфейс физически не может предложить событие
// на дату, где уже есть тренировка.
const GapPrompt = ({ days, accent = '#FF4D1F' }) => (
  <button className="row-tap" style={{
    width: '100%', height: 42, borderRadius: 16,
    border: '1px dashed rgba(14,15,18,0.16)', background: 'transparent',
    display: 'flex', alignItems: 'center', gap: 8, padding: '0 14px',
  }}>
    <div className="t-label-xs" style={{ color: '#A8ACB4', fontSize: 9.5 }}>{days} ДН. БЕЗ ТРЕНИРОВОК</div>
    <div style={{ flex: 1 }}/>
    <div style={{ fontSize: 11.5, fontWeight: 700, color: accent }}>отметить событие</div>
    <svg width="11" height="11" viewBox="0 0 14 14" style={{ color: accent }}>
      <path d="M5 2l5 5-5 5" stroke="currentColor" strokeWidth="2" fill="none" strokeLinecap="round" strokeLinejoin="round"/>
    </svg>
  </button>
);

// ── Композер события ──────────────────────────────────────────────────
const DateField = ({ label, value, muted, accent }) => (
  <div style={{ flex: 1, minWidth: 0 }}>
    <div className="t-label-xs" style={{ fontSize: 9, marginBottom: 4 }}>{label}</div>
    <div className="chip row-tap" style={{
      height: 46, borderRadius: 14, display: 'flex', alignItems: 'center', justifyContent: 'center',
      fontSize: 15, fontWeight: 700, letterSpacing: -0.2,
      color: muted ? '#A8ACB4' : '#0E0F12',
      border: muted ? '1px dashed rgba(14,15,18,0.14)' : '0.5px solid rgba(14,15,18,0.10)',
      background: muted ? 'transparent' : undefined,
    }}>{value}</div>
  </div>
);

const Switch = ({ on, accent }) => (
  <div style={{
    width: 46, height: 28, borderRadius: 14, flexShrink: 0, position: 'relative',
    background: on ? accent : 'rgba(14,15,18,0.14)',
    transition: 'background 160ms ease',
  }}>
    <div style={{
      position: 'absolute', top: 3, left: on ? 21 : 3, width: 22, height: 22, borderRadius: 11,
      background: '#fff', boxShadow: '0 1px 3px rgba(14,15,18,0.28)', transition: 'left 160ms cubic-bezier(.22,.61,.36,1)',
    }}/>
  </div>
);

// mode: 'empty' | 'preset' | 'open' | 'edit'
const EventComposer = ({ mode = 'empty', accent = '#FF4D1F' }) => {
  const isOpen = mode === 'open';
  const edit = mode === 'edit';
  const picked = mode === 'preset' ? 'Болезнь' : edit ? 'Поездка' : null;
  const text = mode === 'preset' ? 'Болезнь' : edit ? 'Командировка, зала не было' : '';
  return (
    <div className="glass-thick" style={{
      position: 'absolute', left: 0, right: 0, bottom: 0, zIndex: 70,
      borderRadius: '28px 28px 52px 52px', padding: '16px 16px 26px',
      borderTop: '0.5px solid rgba(255,255,255,0.8)',
      boxShadow: '0 -20px 40px -18px rgba(14,15,18,0.22)',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', marginBottom: 14 }}>
        <div>
          <div className="t-label">{edit ? 'ПРАВКА СОБЫТИЯ' : 'НОВОЕ СОБЫТИЕ'}</div>
          <div style={{ fontSize: 17, fontWeight: 700, color: '#0E0F12', letterSpacing: -0.3, marginTop: 2 }}>
            Дни без тренировок
          </div>
        </div>
        <div style={{ flex: 1 }}/>
        <button className="chip" style={{ width: 34, height: 34, borderRadius: 17, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          <svg width="11" height="11" viewBox="0 0 10 10"><path d="M1 1l8 8M9 1l-8 8" stroke="#6E727B" strokeWidth="1.7" strokeLinecap="round"/></svg>
        </button>
      </div>

      {/* даты */}
      <div style={{ display: 'flex', alignItems: 'flex-end', gap: 8 }}>
        <DateField label="НАЧАЛО" value={edit ? '05 авг' : '20 авг'} accent={accent} />
        <div style={{ paddingBottom: 15, color: '#A8ACB4', fontSize: 13 }}>—</div>
        <DateField label="КОНЕЦ" value={isOpen ? 'идёт' : (edit ? '09 авг' : '26 авг')} muted={isOpen} accent={accent} />
        <div style={{ flex: '0 0 52px', paddingBottom: 4, textAlign: 'center' }}>
          <div className="t-display t-num" style={{ fontSize: 20, lineHeight: 1, color: isOpen ? accent : '#0E0F12' }}>
            {isOpen ? '3' : (edit ? '5' : '7')}
          </div>
          <div className="t-label-xs" style={{ fontSize: 9, marginTop: 2 }}>ДН.</div>
        </div>
      </div>

      {/* ещё идёт */}
      <div className="glass row-tap" style={{
        marginTop: 10, borderRadius: 16, padding: '10px 14px',
        display: 'flex', alignItems: 'center', gap: 12,
      }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 14, fontWeight: 700, color: '#0E0F12', letterSpacing: -0.2 }}>Ещё идёт</div>
          <div style={{ fontSize: 11, color: '#6E727B', marginTop: 1, textWrap: 'pretty' }}>
            Закроется само, когда запишешь тренировку
          </div>
        </div>
        <Switch on={isOpen} accent={accent} />
      </div>

      {/* пресеты — подставляют текст, категории в данных нет */}
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 12 }}>
        {PRESETS.map(p => {
          const on = p === picked;
          return (
            <div key={p} className={on ? 'row-tap' : 'chip row-tap'} style={{
              padding: '8px 13px', borderRadius: 999, fontSize: 12.5, fontWeight: 700, letterSpacing: -0.15,
              background: on ? '#0E0F12' : undefined, color: on ? '#fff' : '#2E3138',
            }}>{p}</div>
          );
        })}
      </div>

      {/* текст */}
      <div className="glass" style={{
        marginTop: 10, borderRadius: 18, padding: '12px 14px', minHeight: 76,
        display: 'flex', alignItems: 'flex-start',
      }}>
        <div style={{ fontSize: 13.5, lineHeight: 1.45, color: text ? '#0E0F12' : '#A8ACB4', letterSpacing: -0.15, textWrap: 'pretty' }}>
          {text || 'болел, температура'}
          <span style={{ display: 'inline-block', width: 2, height: 16, background: accent, verticalAlign: -3, marginLeft: 1 }}/>
        </div>
      </div>

      <div style={{ display: 'flex', gap: 8, marginTop: 14 }}>
        {edit && (
          <button className="row-tap" style={{
            width: 56, height: 54, borderRadius: 27, flexShrink: 0,
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            background: 'rgba(220,72,72,0.06)', border: '0.5px solid rgba(220,72,72,0.20)',
          }}>
            <svg width="16" height="16" viewBox="0 0 18 18">
              <path d="M3.5 5h11M7 5V3.4h4V5M5 5l.8 10.2h6.4L13 5" stroke="#DC4848" strokeWidth="1.7" fill="none" strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
          </button>
        )}
        <button className="row-tap" style={{
          flex: 1, height: 54, borderRadius: 27, background: '#0E0F12', color: '#fff',
          fontSize: 16, fontWeight: 700, letterSpacing: -0.2, boxShadow: '0 8px 24px rgba(11,11,16,0.30)',
        }}>{edit ? 'Сохранить' : 'Добавить событие'}</button>
      </div>
      <div style={{ marginTop: 9, textAlign: 'center', fontSize: 11, color: '#A8ACB4', textWrap: 'pretty' }}>
        Тренер перечитает план — как после нового замера
      </div>
    </div>
  );
};

// ── Плашка открытого события на «Сегодня» ─────────────────────────────
// Состояние, не упрёк: нейтральные чернила, без красного и без «пропущено».
const TodayEventStrip = ({ accent = '#FF4D1F', days = 5, text = 'Болезнь' }) => (
  <div className="liquid-glass" style={{
    borderRadius: 18, padding: '9px 10px 9px 13px',
    display: 'flex', alignItems: 'center', gap: 10,
  }}>
    <span style={{ width: 7, height: 7, borderRadius: 4, background: accent, boxShadow: `0 0 0 3px ${accent}26`, flexShrink: 0 }}/>
    <div style={{ minWidth: 0, flex: 1 }}>
      <div style={{ fontSize: 13.5, fontWeight: 700, color: '#0E0F12', letterSpacing: -0.2, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
        {text} <span className="t-num" style={{ color: '#6E727B', fontWeight: 600 }}>· {days} дн.</span>
      </div>
      <div style={{ fontSize: 11, color: '#6E727B', marginTop: 1 }}>План сегодня легче обычного</div>
    </div>
    <button className="chip row-tap" style={{
      padding: '7px 12px', borderRadius: 999, fontSize: 11.5, fontWeight: 700, color: '#0E0F12', flexShrink: 0,
    }}>Закончилась?</button>
  </div>
);

// ── Заметка к тренировке: не шаг, а предложение после факта ──────────
// Завершение остаётся одним нажатием. Тренировка уже записана — заметку
// предлагает полоска, которая сама уедет; её можно просто не заметить.
const FinishToast = ({ accent = '#FF4D1F' }) => (
  <div className="glass-thick" style={{
    position: 'absolute', left: 14, right: 14, bottom: 96, zIndex: 45,
    borderRadius: 20, padding: '10px 10px 10px 14px',
    display: 'flex', alignItems: 'center', gap: 10,
    border: '0.5px solid rgba(14,15,18,0.10)',
    boxShadow: '0 14px 30px -14px rgba(14,15,18,0.30)',
  }}>
    <div style={{
      width: 26, height: 26, borderRadius: 13, flexShrink: 0, background: 'rgba(31,157,107,0.15)',
      display: 'flex', alignItems: 'center', justifyContent: 'center',
    }}>
      <svg width="13" height="13" viewBox="0 0 22 22"><path d="M5 11.5l4 4 8.5-9" stroke="#1F9D6B" strokeWidth="3" fill="none" strokeLinecap="round" strokeLinejoin="round"/></svg>
    </div>
    <div style={{ flex: 1, minWidth: 0 }}>
      <div style={{ fontSize: 13.5, fontWeight: 700, color: '#0E0F12', letterSpacing: -0.2 }}>Тренировка записана</div>
      <div style={{ fontSize: 11, color: '#6E727B', marginTop: 1 }}>6 упр. · 14 сетов · 52 мин</div>
    </div>
    <button className="chip row-tap" style={{
      padding: '8px 13px', borderRadius: 999, fontSize: 12, fontWeight: 700, color: '#0E0F12', flexShrink: 0,
      display: 'flex', alignItems: 'center', gap: 6,
    }}>
      <svg width="12" height="12" viewBox="0 0 16 16">
        <path d="M11.4 2.6l2 2L6 12l-2.6.6L4 10l7.4-7.4z" stroke="#0E0F12" strokeWidth="1.5" fill="none" strokeLinejoin="round"/>
      </svg>
      Заметка
    </button>
  </div>
);

// Открывается только если полоску тронули.
const NoteSheet = ({ accent = '#FF4D1F' }) => (
  <div className="glass-thick" style={{
    position: 'absolute', left: 0, right: 0, bottom: 0, zIndex: 70,
    borderRadius: '28px 28px 52px 52px', padding: '16px 16px 26px',
    borderTop: '0.5px solid rgba(255,255,255,0.8)',
    boxShadow: '0 -20px 40px -18px rgba(14,15,18,0.22)',
  }}>
    <div style={{ display: 'flex', alignItems: 'center', marginBottom: 12 }}>
      <div>
        <div className="t-label">ЗАМЕТКА К ТРЕНИРОВКЕ</div>
        <div style={{ fontSize: 17, fontWeight: 700, color: '#0E0F12', letterSpacing: -0.3, marginTop: 2 }}>21 авг · Грудь + спина</div>
      </div>
      <div style={{ flex: 1 }}/>
      <button className="chip" style={{ width: 34, height: 34, borderRadius: 17, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <svg width="11" height="11" viewBox="0 0 10 10"><path d="M1 1l8 8M9 1l-8 8" stroke="#6E727B" strokeWidth="1.7" strokeLinecap="round"/></svg>
      </button>
    </div>
    <div className="glass" style={{ borderRadius: 18, padding: '12px 14px', minHeight: 76 }}>
      <div style={{ fontSize: 13.5, lineHeight: 1.45, color: '#0E0F12', letterSpacing: -0.15, textWrap: 'pretty' }}>
        Спал 4 часа, всё шло тяжело — веса ниже обычного
        <span style={{ display: 'inline-block', width: 2, height: 15, background: accent, verticalAlign: -3, marginLeft: 1 }}/>
      </div>
    </div>
    <button className="row-tap" style={{
      width: '100%', marginTop: 12, height: 54, borderRadius: 27, background: '#0E0F12', color: '#fff',
      fontSize: 16, fontWeight: 700, letterSpacing: -0.2, boxShadow: '0 8px 24px rgba(11,11,16,0.30)',
    }}>Сохранить</button>
    <div style={{ marginTop: 9, textAlign: 'center', fontSize: 11, color: '#A8ACB4' }}>Уедет тренеру вместе с весами</div>
  </div>
);

// note: 'toast' | 'open' | 'late' — поздний вход с карточки в истории
const FinishNoteScreen = ({ accent = '#FF4D1F', note = 'toast' }) => (
  <HistoryScreen
    accent={accent}
    feed={FEED_AUG}
    noteAffordance={note === 'late'}
    overlay={note === 'toast' ? <FinishToast accent={accent} /> : note === 'open' ? (
      <>
        <div style={{ position: 'absolute', inset: 0, background: 'rgba(14,15,18,0.18)', backdropFilter: 'blur(2px)', zIndex: 65 }}/>
        <NoteSheet accent={accent} />
      </>
    ) : null}
  />
);

// ── Экраны-обёртки ────────────────────────────────────────────────────
const HistoryEventsScreen = ({ accent = '#FF4D1F', feed = 'aug', signals = [], composer = null, gap = false }) => {
  const feeds = { aug: FEED_AUG, open: FEED_OPEN, chain: FEED_CHAIN, sparse: FEED_SPARSE, edge: FEED_EDGE };
  const overlay = composer ? (
    <>
      <div style={{ position: 'absolute', inset: 0, background: 'rgba(14,15,18,0.18)', backdropFilter: 'blur(2px)', zIndex: 65 }}/>
      <EventComposer mode={composer} accent={accent} />
    </>
  ) : null;
  return <HistoryScreen accent={accent} signals={signals} feed={feeds[feed]} gap={gap} overlay={overlay} dim={!!composer} />;
};

Object.assign(window, {
  EventCard, EventRail, GapPrompt, EventComposer, TodayEventStrip,
  FinishToast, NoteSheet, FinishNoteScreen, HistoryEventsScreen, Switch,
});
