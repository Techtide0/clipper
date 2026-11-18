import { useCallback, useEffect, useRef, useState } from 'react'

const TABS = [
  ['pending', 'To review'],
  ['approved', 'Approved'],
  ['rejected', 'Rejected'],
]

async function api(path, opts) {
  const res = await fetch(`/api${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  })
  if (!res.ok) {
    let msg = `${res.status}`
    try { msg = (await res.json()).detail || msg } catch { /* not JSON */ }
    throw Object.assign(new Error(msg), { status: res.status })
  }
  return res.json()
}

const mmss = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`

function Card({ c, onChange }) {
  const [form, setForm] = useState({
    title: c.title || '', hook: c.hook || '', caption: c.caption || '',
    start_s: c.start_s, end_s: c.end_s,
  })
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const dirty = form.title !== (c.title || '') || form.hook !== (c.hook || '')
    || form.caption !== (c.caption || '') || form.start_s !== c.start_s || form.end_s !== c.end_s

  const patch = async (body) => {
    setBusy(true); setErr('')
    try { onChange(await api(`/candidates/${c.id}`, { method: 'PATCH', body: JSON.stringify(body) })) }
    catch (e) { setErr(e.message) }
    setBusy(false)
  }
  const [perf, setPerf] = useState({ views: c.views ?? '', likes: c.likes ?? '' })
  const perfDirty = String(perf.views) !== String(c.views ?? '') || String(perf.likes) !== String(c.likes ?? '')
  const savePerf = () => {
    const body = {}
    for (const k of ['views', 'likes']) {
      const raw = String(perf[k]).trim()
      if (raw === String(c[k] ?? '')) continue        // untouched: never overwrite it with a stale value
      if (raw === '') { body[k] = null; continue }    // emptied on purpose: clear the saved number
      const n = Number(raw)
      if (!Number.isSafeInteger(n) || n < 0) {        // 0 is fine; nothing is sent if any field is bad
        setErr(`${k === 'views' ? 'Views' : 'Likes'} must be a whole number, 0 or more`)
        return
      }
      body[k] = n
    }
    if (Object.keys(body).length) patch(body)
  }

  const [exp, setExp] = useState(null)
  const [lookupErr, setLookupErr] = useState('')
  const [lookupTick, setLookupTick] = useState(0)
  const [copyFallback, setCopyFallback] = useState('')
  const expSeq = useRef(0) // a lookup only applies if nothing newer (another lookup or an export) started since
  const canExport = c.review_status === 'approved' && c.render_status === 'done'

  // show a previous export if one exists; 404 just means "not exported yet", anything else is a real error
  useEffect(() => {
    if (!canExport) return
    let live = true
    const seq = ++expSeq.current
    const current = () => live && seq === expSeq.current
    api(`/candidates/${c.id}/export`)
      .then((m) => { if (current()) { setExp(m); setLookupErr('') } })
      .catch((e) => { if (current() && e.status !== 404) setLookupErr(e.message) })
    return () => { live = false }
  }, [canExport, c.id, lookupTick])

  const doExport = async () => {
    expSeq.current++ // a slower earlier lookup must not overwrite this result
    setBusy(true); setErr(''); setLookupErr('')
    try { setExp(await api(`/candidates/${c.id}/export`, { method: 'POST' })) }
    catch (e) { setErr(e.message) }
    setBusy(false)
  }
  const copy = async (text) => {
    try {
      if (!navigator.clipboard) throw new Error('clipboard unavailable')
      await navigator.clipboard.writeText(text)
      setCopyFallback('')
    } catch {
      setCopyFallback(text) // show it selectable so it can still be copied by hand
    }
  }

  const render = async () => {
    setBusy(true); setErr('')
    try { await api(`/candidates/${c.id}/render`, { method: 'POST' }); onChange({ ...c, render_status: 'rendering' }) }
    catch (e) { setErr(e.message) }
    setBusy(false)
  }

  return (
    <div className="card">
      <div>
        {c.render_status === 'done' ? (
          <>
            <video className="media tall" src={c.vertical_url} controls />
            <div className="row">
              <a href={c.vertical_url} download>⬇ 9:16</a>
              <a href={c.wide_url} download>⬇ 16:9</a>
            </div>
          </>
        ) : c.platform !== 'youtube' ? (
          <div className="media nopreview">
            <p className="muted">No embedded preview for {c.platform}.</p>
            {/^https?:\/\//i.test(c.video_url) && (
              <a href={c.video_url.split('#')[0]} target="_blank" rel="noreferrer">Open the video</a>
            )}
            <p className="muted">Jump to {mmss(c.start_s)}</p>
          </div>
        ) : (
          <iframe
            className="media"
            title={`preview ${c.id}`}
            src={`https://www.youtube.com/embed/${c.youtube_id}?start=${Math.floor(c.start_s)}&end=${Math.ceil(c.end_s)}`}
            allowFullScreen
          />
        )}
      </div>
      <div>
        <div className="row" style={{ marginTop: 0 }}>
          <span className="badge">AI {c.ai_score != null ? c.ai_score.toFixed(1) : '—'}</span>
          <span className="badge">{c.content_type}</span>
          <span className="badge">signal {c.score.toFixed(2)}</span>
          <span className="muted">{c.video_title} · {mmss(c.start_s)}–{mmss(c.end_s)}</span>
        </div>
        {c.ai_reason && <p className="muted">{c.ai_reason}</p>}

        <label htmlFor={`title-${c.id}`}>Title</label>
        <input id={`title-${c.id}`} value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} />
        <label htmlFor={`hook-${c.id}`}>Hook</label>
        <input id={`hook-${c.id}`} value={form.hook} onChange={(e) => setForm({ ...form, hook: e.target.value })} />
        <label htmlFor={`caption-${c.id}`}>Caption</label>
        <textarea id={`caption-${c.id}`} rows={3} value={form.caption} onChange={(e) => setForm({ ...form, caption: e.target.value })} />
        <div className="trim">
          <div>
            <label htmlFor={`start-${c.id}`}>Start (seconds)</label>
            <input id={`start-${c.id}`} type="number" value={form.start_s} onChange={(e) => setForm({ ...form, start_s: Number(e.target.value) })} />
          </div>
          <div>
            <label htmlFor={`end-${c.id}`}>End (seconds)</label>
            <input id={`end-${c.id}`} type="number" value={form.end_s} onChange={(e) => setForm({ ...form, end_s: Number(e.target.value) })} />
          </div>
        </div>

        <div className="row">
          {dirty && <button className="primary" disabled={busy} onClick={() => patch(form)}>Save changes</button>}
          {c.review_status !== 'approved' && <button className="good" disabled={busy} onClick={() => patch({ review_status: 'approved' })}>Approve</button>}
          {c.review_status !== 'rejected' && <button className="bad" disabled={busy} onClick={() => patch({ review_status: 'rejected' })}>Reject</button>}
          {c.review_status !== 'pending' && <button disabled={busy} onClick={() => patch({ review_status: 'pending' })}>Back to review</button>}
          {c.review_status === 'approved' && c.render_status !== 'rendering' && (
            <button disabled={busy || dirty} onClick={render}>{c.render_status === 'done' ? 'Re-render' : 'Render clip'}</button>
          )}
          {c.render_status === 'rendering' && <span className="badge">rendering…</span>}
          {canExport && (
            <button className="primary" disabled={busy || dirty} onClick={doExport}>
              {exp ? 'Re-export' : 'Export for platforms'}
            </button>
          )}
        </div>
        {exp && (
          <div className="export">
            {exp.platforms.map((p) => (
              <div key={p.key} className="exp-row">
                <strong>{p.label}</strong>
                <a href={`/exports/${p.video}`} download>⬇ video</a>
                <button onClick={() => copy(p.title ? `${p.title}\n\n${p.text}` : p.text)}>Copy text</button>
                {p.warning && <span className="err">{p.warning}</span>}
              </div>
            ))}
            {copyFallback && (
              <>
                <p className="muted">Couldn't copy automatically. Select and copy this text:</p>
                <textarea readOnly rows={5} value={copyFallback} onFocus={(e) => e.target.select()} />
              </>
            )}
            <p className="muted">Files are also saved in the exports folder inside the backend's data directory.</p>
          </div>
        )}
        {c.review_status === 'approved' && (
          <div className="export">
            <strong>Results after posting</strong>
            <p className="muted">Enter these once the clip has been up a while. They teach the scoring (run <code>tune</code>).</p>
            <div className="trim">
              <div>
                <label htmlFor={`views-${c.id}`}>Views</label>
                <input id={`views-${c.id}`} type="number" min="0" value={perf.views} onChange={(e) => setPerf({ ...perf, views: e.target.value })} />
              </div>
              <div>
                <label htmlFor={`likes-${c.id}`}>Likes</label>
                <input id={`likes-${c.id}`} type="number" min="0" value={perf.likes} onChange={(e) => setPerf({ ...perf, likes: e.target.value })} />
              </div>
            </div>
            {perfDirty && <div className="row"><button className="primary" disabled={busy} onClick={savePerf}>Save results</button></div>}
          </div>
        )}
        {lookupErr && (
          <p className="err">
            Couldn't check for an earlier export ({lookupErr}){' '}
            <button onClick={() => setLookupTick((t) => t + 1)}>Retry</button>
          </p>
        )}
        {c.render_status === 'failed' && <p className="err">Render failed: {c.render_error}</p>}
        {err && <p className="err">{err}</p>}
      </div>
    </div>
  )
}

const IN_PROGRESS = ['new', 'downloaded']

// Paste a YouTube / Twitch / Kick video link; the backend analyzes it in the background.
function LinkBar({ onFinished }) {
  const [url, setUrl] = useState('')
  const [type, setType] = useState('gameplay')
  const [msg, setMsg] = useState(null) // { ok, text }
  const [busy, setBusy] = useState(false)
  const [videos, setVideos] = useState([])
  const wasWorking = useRef(false)

  const refresh = useCallback(async () => {
    try { setVideos(await api('/videos?limit=6')) } catch { /* the main banner already reports a dead backend */ }
  }, [])
  useEffect(() => { refresh() }, [refresh])

  // while something is being analyzed, check on it; when it finishes, reload the review list
  const working = videos.some((v) => IN_PROGRESS.includes(v.status))
  useEffect(() => {
    if (!working) {
      if (wasWorking.current) { wasWorking.current = false; onFinished() }
      return
    }
    wasWorking.current = true
    const t = setInterval(refresh, 5000)
    return () => clearInterval(t)
  }, [working, refresh, onFinished])

  const submit = async (e) => {
    e.preventDefault()
    setBusy(true); setMsg(null)
    try {
      const r = await api('/videos/from-url', { method: 'POST', body: JSON.stringify({ url, content_type: type }) })
      setMsg({
        ok: true,
        text: r.queued
          ? `Added "${r.title}". Analysis takes a few minutes; its moments will appear under "To review".`
          : `"${r.title}" was already added (${r.status}).`,
      })
      setUrl('')
      await refresh()
    } catch (err) { setMsg({ ok: false, text: err.message }) }
    setBusy(false)
  }

  return (
    <div className="card linkbar">
      <form onSubmit={submit}>
        <label htmlFor="video-link">Analyze a video: paste a YouTube, Twitch or Kick link</label>
        <div className="row">
          <input id="video-link" className="grow" value={url} placeholder="https://www.youtube.com/watch?v=…"
            onChange={(e) => setUrl(e.target.value)} />
          <select aria-label="Video type" value={type} onChange={(e) => setType(e.target.value)}>
            <option value="gameplay">gameplay</option>
            <option value="news">news</option>
            <option value="commentary">commentary</option>
          </select>
          <button className="primary" disabled={busy || !url.trim()}>{busy ? 'Reading link…' : 'Analyze'}</button>
        </div>
      </form>
      {msg && <p className={msg.ok ? 'muted' : 'err'}>{msg.text}</p>}
      {videos.length > 0 && (
        <ul className="videos">
          {videos.map((v) => (
            <li key={v.id}>
              <span className="badge">{v.status}</span> {v.title}
              <span className="muted"> · {v.channel}{v.status === 'analyzed' ? ` · ${v.moments} moments` : ''}</span>
              {v.error && <span className="err"> · {v.error}</span>}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

export default function App() {
  const [tab, setTab] = useState('pending')
  const [items, setItems] = useState([])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    try { setItems(await api(`/candidates?status=${tab}`)); setError('') }
    catch (e) { setError(`Can't reach the backend (${e.message}). Is it running on port 8000?`) }
    setLoading(false)
  }, [tab])

  useEffect(() => { load() }, [load])

  const selectTab = (key) => { setLoading(true); setTab(key) }

  // poll while anything is rendering so finished clips appear on their own
  const anyRendering = items.some((c) => c.render_status === 'rendering')
  useEffect(() => {
    if (!anyRendering) return
    const t = setInterval(load, 4000)
    return () => clearInterval(t)
  }, [anyRendering, load])

  const replace = (updated) => {
    // a card that changed review status no longer belongs in this tab
    setItems((list) => list.flatMap((x) => (x.id !== updated.id ? [x]
      : updated.review_status === tab ? [updated] : [])))
  }

  return (
    <>
      <h1>Clipper — review</h1>
      <LinkBar onFinished={load} />
      <div className="tabs">
        {TABS.map(([key, label]) => (
          <button key={key} className={tab === key ? 'on' : ''} onClick={() => selectTab(key)}>{label}</button>
        ))}
      </div>
      {error && <p className="err">{error}</p>}
      {loading && !error && <p className="muted">Loading…</p>}
      {!loading && !error && items.length === 0 && <div className="empty">Nothing here yet.</div>}
      {items.map((c) => <Card key={c.id} c={c} onChange={replace} />)}
    </>
  )
}
