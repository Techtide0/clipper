import { useState } from 'react'
import { api } from './api'
import { Empty, useApp, useLoad } from './ui'

const NAMES = { audio: 'Loudness', chat: 'Chat activity', keywords: 'Keywords' }
const word = (r) => (r >= 0.5 ? 'strong' : r >= 0.2 ? 'some' : r > -0.2 ? 'little' : 'negative')

function Views({ row, reload }) {
  const { toast } = useApp()
  const [v, setV] = useState(row.views == null ? '' : String(row.views))
  const save = async () => {
    const n = v.trim() === '' ? null : Number(v.replace(/[,\s]/g, ''))
    if (n !== null && (!Number.isInteger(n) || n < 0)) { toast('Enter a whole number, 0 or more'); return }
    if (n === row.views) return
    try { await api(`/candidates/${row.id}`, { method: 'PATCH', body: JSON.stringify({ views: n }) }); reload() } catch (e) { toast(e.message) }
  }
  return <input className="f num" aria-label={`Views for ${row.title}`} inputMode="numeric" placeholder="enter" style={{ width: 84, minHeight: 0, padding: '5px 7px', fontSize: 13 }}
    value={v} onChange={(e) => setV(e.target.value)} onBlur={save} onKeyDown={(e) => e.key === 'Enter' && e.currentTarget.blur()} />
}

export default function Performance() {
  const { toast } = useApp()
  const { data: p, loaded, reload } = useLoad('/performance')
  const { data: ready } = useLoad('/candidates?status=approved')
  if (!loaded || !p) return <p className="muted">Loading…</p>

  const act = async (path, msg) => { try { await api(path, { method: 'POST' }); toast(msg); reload() } catch (e) { toast(e.message) } }
  const rows = p.posted || []
  const max = Math.max(1, ...rows.map((r) => r.views || 0))
  const postedNoViews = (ready || []).filter((c) => c.posted_at && c.views == null).length
  const signals = Object.keys(p.current_weights || {})
  const best = Object.entries(p.correlations || {}).sort((a, b) => b[1].correlation - a[1].correlation)[0]
  const changed = signals.some((k) => Math.abs((p.suggested_weights?.[k] ?? 0) - p.current_weights[k]) >= 0.05)

  return (
    <>
      <div className="col" style={{ gap: 6, marginBottom: 18 }}>
        <h1 className="page">Performance</h1>
        <p className="muted" style={{ margin: 0, fontSize: 15 }}>
          {p.enough_data ? `Learning from ${p.clips_with_views} posted clips.` : `${p.clips_with_views} of ${p.needs} clips with views entered.`}
        </p>
      </div>

      {!p.enough_data && <Empty>Nothing to learn from yet. Suggestions start once {p.needs} posted clips have their views entered.{postedNoViews ? ` ${postedNoViews} posted clip${postedNoViews === 1 ? ' is' : 's are'} waiting for views.` : ''}</Empty>}

      {p.enough_data && (
        <>
          {best && (
            <div className="dark-card">
              <span className="eyebrow" style={{ color: 'oklch(0.82 0.1 355)' }}>What the app learned</span>
              <p style={{ margin: 0, font: '500 clamp(20px,2.6vw,26px)/1.3 Instrument Sans', letterSpacing: '-.01em', maxWidth: 760, textWrap: 'pretty' }}>
                {best[1].correlation >= 0.2 ? `Clips with strong ${NAMES[best[0]] || best[0]} signals tended to get more views.` : 'No signal clearly predicts views yet.'}
                {p.notes?.length ? ` ${p.notes[0]}` : ''}
              </p>
            </div>
          )}
          <div className="card col" style={{ gap: 4, marginBottom: 16, padding: 18 }}>
            <div className="th" style={{ gridTemplateColumns: 'minmax(110px,1.1fr) minmax(120px,2fr) 70px 90px' }}><span>Signal</span><span>Predicted views</span><span style={{ textAlign: 'right' }}>Now</span><span style={{ textAlign: 'right' }}>Suggested</span></div>
            {signals.map((k) => {
              const c = p.correlations?.[k]
              const r = c?.correlation ?? 0
              const now = p.current_weights[k], sug = p.suggested_weights?.[k] ?? now
              return (
                <div className="tr" key={k} style={{ gridTemplateColumns: 'minmax(110px,1.1fr) minmax(120px,2fr) 70px 90px' }}>
                  <b>{NAMES[k] || k}</b>
                  <div className="row" style={{ gap: 8, flexWrap: 'nowrap' }}>
                    <div className="bar" style={{ flex: 1, height: 8 }}><i style={{ width: `${Math.max(3, Math.min(100, (r + 1) * 50))}%`, background: r >= 0.2 ? 'var(--acc)' : 'var(--mut2)' }} /></div>
                    <span className="mono muted2" style={{ fontSize: 12, width: 64 }}>{c ? word(r) : 'not enough'}</span>
                  </div>
                  <span className="mono muted" style={{ textAlign: 'right', fontSize: 13 }}>{now.toFixed(1)}</span>
                  <b className="mono" style={{ textAlign: 'right', fontSize: 13 }}>{sug > now + 0.04 ? '↑' : sug < now - 0.04 ? '↓' : '→'} {sug.toFixed(1)}</b>
                </div>
              )
            })}
            <div className="row" style={{ paddingTop: 12 }}>
              <button className="btn primary" style={{ minHeight: 42, fontSize: 14 }} disabled={!changed} onClick={() => act('/performance/apply', 'Suggested weights saved')}>{changed ? 'Use the suggestions' : 'Already using them'}</button>
              <button className="btn" style={{ minHeight: 42, fontSize: 14 }} onClick={() => act('/performance/reset', 'Back to the defaults')}>Reset to defaults</button>
              <span className="muted2" style={{ flex: '1 1 260px', fontSize: 13 }}>Applies to videos analyzed from now on. Clips already found keep their scores.</span>
            </div>
          </div>
          <details className="how">
            <summary>How suggestions work</summary>
            <ul>
              <li>Nothing is suggested until at least {p.needs} clips have views.</li>
              <li>Each signal needs about 10 clips that have it before it counts.</li>
              <li>Weights stay between 0.1 and 3, so one lucky clip can’t take over.</li>
              <li>Enter views after a similar wait for every clip (for example, 3 days after posting).</li>
            </ul>
          </details>
        </>
      )}

      {rows.length > 0 && (
        <div className="card" style={{ padding: '6px 18px 10px', overflowX: 'auto' }}>
          <div style={{ minWidth: 560 }}>
            <div className="th" style={{ gridTemplateColumns: 'minmax(200px,2.4fr) 70px 1.4fr 90px' }}><span>Clip</span><span>AI</span><span>Views</span><span>Likes</span></div>
            {rows.map((r) => (
              <div className="tr" key={r.id} style={{ gridTemplateColumns: 'minmax(200px,2.4fr) 70px 1.4fr 90px' }}>
                <span style={{ fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{r.title}</span>
                <span className="mono" style={{ fontSize: 13 }}>{r.ai_score == null ? '—' : r.ai_score.toFixed(1)}</span>
                <div className="row" style={{ gap: 8, flexWrap: 'nowrap' }}>
                  <Views row={r} reload={reload} />
                  <div className="bar" style={{ flex: 1, minWidth: 30, height: 6 }}><i style={{ width: `${Math.max(2, ((r.views || 0) / max) * 100)}%`, background: 'var(--acc)' }} /></div>
                </div>
                <span className="mono muted2" style={{ fontSize: 13 }}>{r.likes ?? '—'}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </>
  )
}
