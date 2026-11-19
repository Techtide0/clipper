import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api'
import { Empty, Progress, mmss, useApp, useDebounced, useLoad } from './ui'

const decode = (s) => { const t = document.createElement('textarea'); t.innerHTML = s; return t.value }

// Captions arrive with HTML entities and ">>" speaker-change marks; show one line per speaker turn.
const words = (text) => decode(text || '').replace(/ /g, ' ').split(/\s*>>\s*/).map((l) => l.replace(/\s+/g, ' ').trim()).filter(Boolean)

const SIGNALS = [['audio', 'Loudness'], ['chat', 'Chat activity'], ['keywords', 'Keywords']]
const count = (n, max) => ({ text: `${n} / ${max}`, over: n > max })

function Preview({ c, loaded, onLoad }) {
  const youtube = c.platform === 'youtube'
  const cut = c.render_status === 'done' && c.vertical_url
  return (
    <div className="preview" style={!cut && !loaded && youtube ? { backgroundImage: `linear-gradient(rgba(28,27,24,.35),rgba(28,27,24,.35)), url(https://i.ytimg.com/vi/${c.youtube_id}/hqdefault.jpg)` } : undefined}>
      {cut ? (
        <div style={{ position: 'absolute', inset: 0, display: 'flex', justifyContent: 'center', background: '#1c1b18' }}>
          <video src={c.vertical_url} controls style={{ height: '100%', aspectRatio: '9/16', background: '#000' }} />
        </div>
      ) : c.render_status === 'rendering' ? (
        <div className="over" style={{ background: 'rgba(28,27,24,.72)', gap: 10 }}>
          <b style={{ fontSize: 15 }}>Cutting both formats…</b>
          <div style={{ width: 200 }}><Progress pct={null} /></div>
          <span style={{ fontSize: 13, opacity: .75 }}>Keep reviewing; it runs in the background.</span>
        </div>
      ) : !youtube ? (
        <div className="over" style={{ inset: 14, border: '1.5px dashed rgba(255,253,249,.35)', borderRadius: 8, position: 'absolute', gap: 10 }}>
          <span style={{ fontSize: 14, opacity: .75 }}>No embedded preview for {c.platform}.</span>
          {/^https?:\/\//i.test(c.video_url) && <a href={c.video_url.split('#')[0]} target="_blank" rel="noreferrer" style={{ color: '#fffdf9', fontWeight: 600 }}>Open the video ↗</a>}
          <span className="mono" style={{ fontSize: 12, opacity: .6 }}>Jump to {mmss(c.start_s)}</span>
        </div>
      ) : loaded ? (
        <iframe title="Moment preview" allow="autoplay; encrypted-media; picture-in-picture" allowFullScreen
          src={`https://www.youtube.com/embed/${c.youtube_id}?start=${Math.floor(c.start_s)}&end=${Math.ceil(c.end_s)}&autoplay=1`} />
      ) : (
        <div className="over">
          <span className="mono" style={{ fontSize: 11, opacity: .65 }}>video still · {mmss(c.start_s)}</span>
          <button className="btn" style={{ borderRadius: 999, minHeight: 44, padding: '0 18px', fontWeight: 600, fontSize: 15 }} onClick={onLoad}>▶ Preview at {mmss(c.start_s)}</button>
        </div>
      )}
    </div>
  )
}

// One moment. Its edits live here; the parent only hears about decisions.
function Moment({ c, queueSize, pos, onDecide, onSkip, onMove, shortcutsRef, setSaved }) {
  const { toast, refresh } = useApp()
  const [form, setForm] = useState({ title: c.title || '', hook: c.hook || '', caption: c.caption || '' })
  const [trim, setTrim] = useState({ start: c.start_s, end: c.end_s })
  const [trimErr, setTrimErr] = useState('')
  const [stale, setStale] = useState(false)
  const [loaded, setLoaded] = useState(false)
  const [showSig, setShowSig] = useState(false)
  const [busy, setBusy] = useState(false)
  const [trimBusy, setTrimBusy] = useState(false)
  const titleRef = useRef(null)

  const patch = useCallback(async (body) => api(`/candidates/${c.id}`, { method: 'PATCH', body: JSON.stringify(body) }), [c.id])
  const save = useDebounced(async (next) => {
    try { setSaved('saving'); await patch(next); setSaved('saved') } catch (e) { setSaved(`couldn’t save: ${e.message}`) }
  }, 600)
  const edit = (k) => (e) => { const next = { ...form, [k]: e.target.value }; setForm(next); save(next) }

  const step = async (key, d) => {
    if (trimBusy) return // one change at a time, each based on the last one that was saved
    const next = { ...trim, [key]: Math.max(0, trim[key] + d) }
    if (next.end - next.start < 3) { setTrimErr('A clip must be at least 3 seconds long.'); return }
    setTrimErr('')
    setTrimBusy(true)
    try {
      const had = c.vertical_url && c.render_status === 'done'
      await patch({ start_s: next.start, end_s: next.end })
      setTrim(next)
      if (had) setStale(true)
      setLoaded(false) // the preview window changed
    } catch (e) { setTrimErr(e.message) }
    setTrimBusy(false)
  }

  const decide = async (kind) => {
    if (busy) return
    setBusy(true)
    save.flush() // a title typed a moment ago is saved before the decision
    try { await onDecide(c, kind) } finally { setBusy(false) }
  }
  const cutFirst = async () => {
    try { await api(`/candidates/${c.id}/render`, { method: 'POST' }); toast('Cutting. Keep reviewing; it runs in the background.'); refresh() }
    catch (e) { toast(e.message) }
  }
  const scoreNow = async () => {
    try { await api(`/videos/${c.video_id}/rescore`, { method: 'POST' }); toast('Scoring…') ; refresh() } catch (e) { toast(e.message) }
  }

  // keyboard shortcuts that act on this moment
  useEffect(() => {
    shortcutsRef.current = {
      a: () => decide('approved'), r: () => decide('rejected'), s: onSkip, e: () => titleRef.current?.focus(),
      c: cutFirst, ' ': () => setLoaded(true), ArrowLeft: () => onMove(-1), ArrowRight: () => onMove(1),
    }
    return () => { shortcutsRef.current = {} } // nothing acts on a moment that is no longer shown
  })

  const t = count(form.title.length, 70), h = count(form.hook.length, 100)
  const tags = (form.caption.match(/#\w+/g) || []).length
  const lines = words(c.transcript)
  const win0 = Math.max(0, trim.start - 30), span = trim.end + 30 - win0
  const sigs = SIGNALS.map(([k, label]) => ({ label, v: Math.max(0, Math.min(1, (c.signals || {})[k] ?? 0)) }))
  const high = c.ai_score != null && c.ai_score >= 7.5

  return (
    <div className="row" style={{ gap: 24, alignItems: 'flex-start' }}>
      <div className="col" style={{ flex: '1 1 400px', minWidth: 0, maxWidth: 580, gap: 14 }}>
        <Preview c={{ ...c, start_s: trim.start, end_s: trim.end }} loaded={loaded} onLoad={() => setLoaded(true)} />
        <div className="card col" style={{ padding: 14, gap: 10 }}>
          <div className="row" style={{ justifyContent: 'space-between' }}>
            <span className="eyebrow">Trim</span><span className="mono muted2" style={{ fontSize: 12 }}>{Math.round(trim.end - trim.start)}s long</span>
          </div>
          <div className="timeline" aria-hidden="true"><i style={{ left: `${((trim.start - win0) / span) * 100}%`, width: `${((trim.end - trim.start) / span) * 100}%` }} /></div>
          <div className="row" style={{ justifyContent: 'space-between', gap: 10 }}>
            {[['start', 'Start'], ['end', 'End']].map(([key, label]) => (
              <div className="row" style={{ gap: 6 }} key={key}>
                <span className="muted" style={{ fontSize: 13 }}>{label}</span>
                <button className="step" disabled={trimBusy} aria-label={`${label} one second earlier`} onClick={() => step(key, -1)}>−</button>
                <span className="mono" style={{ fontWeight: 600, fontSize: 14, minWidth: 48, textAlign: 'center' }}>{mmss(trim[key])}</span>
                <button className="step" disabled={trimBusy} aria-label={`${label} one second later`} onClick={() => step(key, 1)}>+</button>
              </div>
            ))}
          </div>
          {trimErr && <span className="err" style={{ fontSize: 13 }}>✕ {trimErr}</span>}
          {stale && <span className="warnc" style={{ fontSize: 13 }}>! Trim changed. This clip will be cut again when you approve (about a minute).</span>}
        </div>
        <div className="card col" style={{ padding: 14 }}>
          <span className="eyebrow">Transcript</span>
          {lines.map((l, i) => <div key={i} style={{ fontSize: 14, lineHeight: 1.5 }}>{l}</div>)}
          {lines.length === 0 && (
            <span className="muted" style={{ fontSize: 14 }}>
              No captions for this video, so there’s no transcript. The AI judged this moment from its audio{c.signals?.chat ? ' and the chat' : ''}.
            </span>
          )}
        </div>
      </div>

      <div className="col" style={{ flex: '1 1 380px', minWidth: 0, gap: 18 }}>
        <div className="row" style={{ justifyContent: 'space-between', alignItems: 'flex-start', gap: 10 }}>
          <div className="col" style={{ gap: 6, minWidth: 0 }}>
            <div className="row">
              <span className="pill neutral">{c.content_type}</span>
              <span className="mono muted" style={{ fontSize: 12 }}>{c.platform} · {mmss(trim.start)}–{mmss(trim.end)}</span>
            </div>
            <span className="muted" style={{ fontSize: 13, lineHeight: 1.4 }}>{c.video_title}</span>
          </div>
          <div className="row" style={{ gap: 4, flex: 'none' }}>
            <button className="btn" style={{ width: 40, height: 40 }} aria-label="Previous moment" onClick={() => onMove(-1)}>←</button>
            <button className="btn" style={{ width: 40, height: 40 }} aria-label="Next moment" onClick={() => onMove(1)}>→</button>
          </div>
        </div>

        <div className="row" style={{ gap: 16, alignItems: 'flex-start', flexWrap: 'nowrap' }}>
          <div className={`score${high ? ' high' : ''}`}><b>{c.ai_score == null ? '—' : c.ai_score.toFixed(1)}</b><small>AI / 10</small></div>
          <div className="col" style={{ minWidth: 0 }}>
            {c.ai_reason ? <p style={{ margin: 0, fontSize: 16, lineHeight: 1.5, textWrap: 'pretty' }}>{c.ai_reason}</p> : (
              <>
                <p className="muted2" style={{ margin: 0, fontSize: 15, lineHeight: 1.5 }}>Not scored yet. The AI service was busy when this video was analyzed.</p>
                <div><button className="btn" onClick={scoreNow}>Score it now</button></div>
              </>
            )}
            <button className="btn link mono" style={{ alignSelf: 'flex-start', fontWeight: 500, fontSize: 12, color: 'var(--mut)' }}
              aria-expanded={showSig} onClick={() => setShowSig(!showSig)}>{showSig ? 'Hide why this was picked' : 'Why was this picked?'}</button>
          </div>
        </div>
        {showSig && (
          <div className="card col" style={{ padding: 14 }}>
            {sigs.map((g) => (
              <div className="trio" key={g.label}><span>{g.label}</span>
                <div className="bar" style={{ height: 6 }}><i style={{ width: `${Math.max(2, g.v * 100)}%`, background: 'var(--mut2)' }} /></div>
                <span className="mono muted" style={{ fontSize: 12, textAlign: 'right' }}>{g.v >= 0.66 ? 'strong' : g.v >= 0.33 ? 'medium' : 'low'}</span>
              </div>
            ))}
            <span className="muted" style={{ fontSize: 12, lineHeight: 1.45 }}>These signals decided which moments were picked. The AI then reads the transcript and chat and listens to the audio; it can’t see the picture.</span>
          </div>
        )}

        <div className="col" style={{ gap: 14 }}>
          <div className="col" style={{ gap: 6 }}>
            <div className="row" style={{ justifyContent: 'space-between' }}><label htmlFor="f-title" style={{ fontWeight: 600, fontSize: 13 }}>Title</label><span className={`mono ${t.over ? 'err' : 'muted'}`} style={{ fontSize: 11 }}>{t.text}</span></div>
            <input id="f-title" ref={titleRef} className="f" style={{ font: '500 16px Instrument Sans' }} value={form.title} onChange={edit('title')} />
          </div>
          <div className="col" style={{ gap: 6 }}>
            <div className="row" style={{ justifyContent: 'space-between' }}><label htmlFor="f-hook" style={{ fontWeight: 600, fontSize: 13 }}>Hook <span className="muted" style={{ fontWeight: 400 }}>· first line viewers see or hear</span></label><span className={`mono ${h.over ? 'err' : 'muted'}`} style={{ fontSize: 11 }}>{h.text}</span></div>
            <input id="f-hook" className="f" value={form.hook} onChange={edit('hook')} />
          </div>
          <div className="col" style={{ gap: 6 }}>
            <div className="row" style={{ justifyContent: 'space-between' }}><label htmlFor="f-cap" style={{ fontWeight: 600, fontSize: 13 }}>Caption</label><span className="mono muted" style={{ fontSize: 11 }}>{tags} hashtag{tags === 1 ? '' : 's'}</span></div>
            <textarea id="f-cap" className="f" rows={3} value={form.caption} onChange={edit('caption')} />
          </div>
        </div>

        <div className="actions">
          <div className="grid">
            <button className="big reject" disabled={busy} onClick={() => decide('rejected')}>✕ Reject <kbd style={{ background: 'var(--bad-bg)' }}>R</kbd></button>
            <button className="big skip" onClick={onSkip}>Skip <kbd>S</kbd></button>
            <button className="big approve" disabled={busy} onClick={() => decide('approved')}>✓ Approve and cut <kbd style={{ background: 'rgba(255,255,255,.22)' }}>A</kbd></button>
          </div>
          <div className="row muted" style={{ justifyContent: 'space-between', fontSize: 12 }}>
            <span>Approving starts the cut right away. Clips are cut one at a time, about a minute each. · {pos} of {queueSize}</span>
            {c.render_status !== 'done' && c.render_status !== 'rendering' && <button className="btn link" style={{ fontWeight: 500, fontSize: 12 }} onClick={cutFirst}>✂ Cut it first to watch the real clip</button>}
          </div>
        </div>
      </div>
    </div>
  )
}

function Rejected() {
  const { toast, refresh } = useApp()
  const { data } = useLoad('/candidates?status=rejected')
  const settings = useLoad('/settings')
  const thr = (settings.data || []).find((s) => s.key === 'judge_min_score')?.value ?? 6
  const act = async (id, fn, msg) => { try { await fn(); toast(msg); refresh() } catch (e) { toast(e.message) } }
  const list = data || []
  return (
    <>
      <p className="muted2" style={{ margin: '0 0 14px', fontSize: 14, maxWidth: 680, lineHeight: 1.5 }}>
        Moments the AI scores under {thr} land here automatically. The AI can mistake a good moment for a dull one, so rescue anything that looks good.
      </p>
      {list.length === 0 && <Empty>Nothing rejected. Moments the AI scores under {thr} land here, and you can rescue any of them.</Empty>}
      <div className="grid-cards">
        {list.map((c) => (
          <article className="card col" key={c.id} style={{ gap: 10 }}>
            {c.render_status === 'done' && c.vertical_url && <video src={c.vertical_url} controls style={{ width: '100%', maxHeight: 360, background: '#000', borderRadius: 8 }} />}
            <div className="row" style={{ gap: 10 }}>
              <span className="mono err" style={{ fontWeight: 600, fontSize: 14, padding: '5px 8px', borderRadius: 8, background: 'var(--bad-bg)' }}>AI {c.ai_score == null ? '—' : c.ai_score.toFixed(1)}</span>
              <span className="mono muted" style={{ fontSize: 12 }}>{c.content_type} · {mmss(c.start_s)}–{mmss(c.end_s)}</span>
            </div>
            <b style={{ fontSize: 16, lineHeight: 1.35 }}>{c.title || 'Untitled moment'}</b>
            <span className="muted2" style={{ fontSize: 14, lineHeight: 1.45 }}>{c.ai_reason}</span>
            <span className="muted" style={{ fontSize: 12 }}>{c.ai_score != null ? `Scored ${c.ai_score.toFixed(1)}, under the ${thr} cutoff.` : 'Rejected by you.'}</span>
            <div className="row" style={{ marginTop: 4 }}>
              <button className="btn dark" style={{ minHeight: 38 }} onClick={() => act(c.id, () => api(`/candidates/${c.id}`, { method: 'PATCH', body: JSON.stringify({ review_status: 'pending' }) }), 'Back in review')}>↩ Back to review</button>
              {c.render_status !== 'done' && c.render_status !== 'rendering' && (
                <button className="btn" style={{ minHeight: 38 }} onClick={() => act(c.id, () => api(`/candidates/${c.id}/render`, { method: 'POST' }), 'Cutting it so you can check')}>✂ Cut clip to check</button>
              )}
              {c.render_status === 'rendering' && <span className="pill info">cutting…</span>}
            </div>
          </article>
        ))}
      </div>
    </>
  )
}

export default function Review() {
  const { summary, go, toast, refresh, helpOpen, setHelp } = useApp()
  const [tab, setTab] = useState('review')
  const pending = useLoad('/candidates?status=pending')
  const [order, setOrder] = useState([])
  const [pos, setPos] = useState(0)
  const [saved, setSaved] = useState('')
  const [focus, setFocus] = useState(null)
  const last = useRef(null)
  const shortcuts = useRef({})

  // keep our order in step with the server: drop what was decided elsewhere, append what is new
  useEffect(() => {
    if (!pending.data) return
    const ids = pending.data.map((c) => c.id)
    setOrder((cur) => {
      let next = [...cur.filter((id) => ids.includes(id)), ...ids.filter((id) => !cur.includes(id))]
      if (focus && next.includes(focus)) { next = [focus, ...next.filter((id) => id !== focus)]; setPos(0); setFocus(null) }
      return next
    })
  }, [pending.data, focus])

  const byId = new Map((pending.data || []).map((c) => [c.id, c]))
  const total = order.length
  const cur = byId.get(order[Math.min(pos, Math.max(0, total - 1))])

  const decide = useCallback(async (c, kind) => {
    try {
      await api(`/candidates/${c.id}`, { method: 'PATCH', body: JSON.stringify({ review_status: kind }) })
    } catch (e) { toast(e.message); return }
    last.current = { id: c.id, kind }
    setOrder((o) => o.filter((id) => id !== c.id))
    setPos((p) => Math.max(0, Math.min(p, order.length - 2)))
    toast(kind === 'approved' ? 'Approved. Cutting now.' : 'Rejected.', async () => {
      await api(`/candidates/${c.id}`, { method: 'PATCH', body: JSON.stringify({ review_status: 'pending' }) })
      setFocus(c.id)
      pending.reload(); refresh()
    })
    pending.reload(); refresh()
  }, [toast, refresh, pending, order.length])

  const move = useCallback((d) => setPos((p) => (total ? (p + d + total) % total : 0)), [total])
  // the moment on screen goes to the back of the line, and the next one takes its place (wrapping at the end)
  const skip = useCallback(() => {
    if (total < 2) return
    const at = Math.min(pos, total - 1)
    setOrder((o) => [...o.filter((_, i) => i !== at), o[at]])
    setPos(at >= total - 1 ? 0 : at)
  }, [total, pos])

  useEffect(() => {
    const onKey = (e) => {
      if (e.metaKey || e.ctrlKey || e.altKey || tab !== 'review') return
      const tag = e.target.tagName
      if (['INPUT', 'TEXTAREA', 'SELECT'].includes(tag)) return
      if (e.key === '?') { setHelp(true); return }
      if (e.key.toLowerCase() === 'u' && last.current) {
        const { id } = last.current
        last.current = null
        api(`/candidates/${id}`, { method: 'PATCH', body: JSON.stringify({ review_status: 'pending' }) }).then(() => { setFocus(id); pending.reload(); refresh(); toast('Undone') })
        return
      }
      const fn = shortcuts.current[e.key.length === 1 ? e.key.toLowerCase() : e.key]
      if (fn) { e.preventDefault(); fn() }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [tab, setHelp, pending, refresh, toast])

  if (!pending.loaded) return <p className="muted">Loading…</p>

  const rejectedN = summary?.rejected ?? null
  const tabs = [['review', `To review (${total})`], ['rejected', rejectedN == null ? 'Rejected' : `Rejected (${rejectedN})`]]

  return (
    <>
      <div className="row" style={{ justifyContent: 'space-between', gap: 12, marginBottom: 20 }}>
        <div className="row" style={{ gap: 16 }}>
          <h1 className="page">Review</h1>
          <div className="seg" role="tablist">
            {tabs.map(([k, l]) => <button key={k} role="tab" aria-selected={tab === k} onClick={() => setTab(k)}>{l}</button>)}
          </div>
        </div>
        <div className="row" style={{ gap: 14 }}>
          {tab === 'review' && cur && (
            <div className="row" style={{ gap: 10 }}>
              <span className="mono" style={{ fontWeight: 600, fontSize: 13 }}>{Math.min(pos, total - 1) + 1} of {total}</span>
              <div style={{ width: 110, height: 5, borderRadius: 3, background: 'var(--track2)', overflow: 'hidden' }}><div style={{ height: '100%', background: 'var(--ink)', width: `${((Math.min(pos, total - 1) + 1) / total) * 100}%`, transition: 'width .3s' }} /></div>
            </div>
          )}
          <button className="btn" onClick={() => setHelp(!helpOpen)}><kbd className="mono">?</kbd> Shortcuts</button>
        </div>
      </div>

      {tab === 'review' && cur && (
        <>
          <Moment key={cur.id} c={cur} queueSize={total} pos={Math.min(pos, total - 1) + 1}
            onDecide={decide} onSkip={skip} onMove={move} shortcutsRef={shortcuts} setSaved={setSaved} />
          <p className="muted" style={{ fontSize: 12, margin: '10px 0 0' }} aria-live="polite">Edits save as you type. {saved && `· ${saved}`}</p>
        </>
      )}
      {tab === 'review' && !cur && (
        <div className="col" style={{ maxWidth: 620, margin: '40px auto', textAlign: 'center', alignItems: 'center', gap: 14 }}>
          <span style={{ width: 56, height: 56, borderRadius: '50%', background: 'var(--good-bg)', color: 'var(--good)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 24, fontWeight: 700 }}>✓</span>
          <h2 style={{ font: '600 30px/1.15 Instrument Sans', letterSpacing: '-.02em', margin: 0 }}>{summary?.approved ? 'All reviewed.' : 'Nothing to review right now.'}</h2>
          <p className="muted2" style={{ margin: 0, fontSize: 16, lineHeight: 1.5 }}>
            {summary?.approved ? `${summary.approved} approved. ${summary.cutting ? 'Clips are being cut; they appear in Ready to post when done.' : 'They are in Ready to post.'}` : 'New moments appear here when a video finishes analyzing.'}
          </p>
          <div className="row" style={{ justifyContent: 'center', marginTop: 6 }}>
            <button className="btn primary lg" style={{ borderRadius: 10, minHeight: 46 }} onClick={() => go(summary?.approved ? 'ready' : 'today')}>{summary?.approved ? 'Go to Ready to post →' : 'Back to Today'}</button>
            <button className="btn lg" style={{ borderRadius: 10, minHeight: 46 }} onClick={() => setTab('rejected')}>Look through rejected</button>
          </div>
        </div>
      )}
      {tab === 'rejected' && <Rejected />}
    </>
  )
}
