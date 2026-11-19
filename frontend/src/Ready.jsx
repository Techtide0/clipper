import { useEffect, useState } from 'react'
import { api } from './api'
import { Empty, Progress, copyText, postText, useApp, useLoad } from './ui'

const parse = (v) => (v.trim() === '' ? null : Number(v.replace(/[,\s]/g, '')))

function Results({ c, reload }) {
  const { toast } = useApp()
  const [views, setViews] = useState(c.views == null ? '' : String(c.views))
  const [likes, setLikes] = useState(c.likes == null ? '' : String(c.likes))
  const [err, setErr] = useState('')
  const dirty = views !== (c.views == null ? '' : String(c.views)) || likes !== (c.likes == null ? '' : String(c.likes))
  const save = async () => {
    const v = parse(views), l = parse(likes)
    if ([v, l].some((n) => n !== null && (!Number.isInteger(n) || n < 0))) { setErr('Enter whole numbers, 0 or more.'); return }
    try { await api(`/candidates/${c.id}`, { method: 'PATCH', body: JSON.stringify({ views: v, likes: l }) }); setErr(''); toast('Results saved'); reload() }
    catch (e) { setErr(e.message) }
  }
  return (
    <div className="row" style={{ flex: '1 1 100%', borderTop: '1px solid var(--line3)', paddingTop: 12, alignItems: 'flex-end' }}>
      <div className="row" style={{ flex: '1 1 100%', justifyContent: 'space-between' }}>
        <span style={{ fontWeight: 600, fontSize: 13 }}>Results after posting <span className="muted" style={{ fontWeight: 400 }}>· posted {new Date(c.posted_at + 'Z').toLocaleDateString(undefined, { day: 'numeric', month: 'short' })}</span></span>
        <span className="muted" style={{ fontSize: 12 }}>They help the app learn what works.</span>
      </div>
      {[['Views', views, setViews], ['Likes', likes, setLikes]].map(([label, val, set]) => (
        <label key={label} className="col muted2" style={{ gap: 4, fontSize: 12, flex: '1 1 110px' }}>{label}
          <input className="f num" inputMode="numeric" style={{ width: '100%' }} value={val} onChange={(e) => set(e.target.value)} />
        </label>
      ))}
      {dirty && <button className="btn dark" style={{ minHeight: 40 }} onClick={save}>Save results</button>}
      {err && <span className="err" style={{ flex: '1 1 100%', fontSize: 12 }}>✕ {err}</span>}
    </div>
  )
}

function Export({ c }) {
  const { toast } = useApp()
  const [manifest, setM] = useState(null)
  const [busy, setBusy] = useState(false)
  useEffect(() => { api(`/candidates/${c.id}/export`).then(setM).catch(() => {}) }, [c.id])  // 404 = not built yet
  const build = async () => {
    setBusy(true)
    try { setM(await api(`/candidates/${c.id}/export`, { method: 'POST' })) } catch (e) { toast(e.message) }
    setBusy(false)
  }
  if (!manifest) return (
    <div className="col" style={{ flex: '1 1 100%', borderTop: '1px solid var(--line3)', paddingTop: 12, gap: 8 }}>
      <span className="muted2" style={{ fontSize: 14 }}>Builds a folder per platform with the right video and text, trimmed to each platform’s limits.</span>
      <div><button className="btn dark" disabled={busy} onClick={build}>{busy ? 'Building…' : 'Build export folders'}</button></div>
    </div>
  )
  const fmtOf = { youtube_shorts: '9:16', youtube_video: '16:9', instagram_reels: '9:16', tiktok: '9:16', facebook_reels: '9:16' }
  return (
    <div className="col" style={{ flex: '1 1 100%', borderTop: '1px solid var(--line3)', paddingTop: 12, gap: 2 }}>
      {manifest.platforms.map((p) => (
        <div key={p.key} className="row" style={{ gap: '6px 12px', padding: '8px 0', borderBottom: '1px solid var(--line3)' }}>
          <span style={{ flex: '1 1 150px', fontWeight: 600, fontSize: 14 }}>{p.label} <span className="mono muted" style={{ fontSize: 11, fontWeight: 500 }}>{fmtOf[p.key]}</span></span>
          <a className="btn" style={{ minHeight: 34, fontSize: 12 }} href={`/exports/${p.video}`} download>⬇ video</a>
          <button className="btn" style={{ minHeight: 34, fontSize: 12 }} onClick={async () => toast((await copyText([p.title, p.text].filter(Boolean).join('\n\n'))) ? 'Copied' : 'Couldn’t copy; select the text by hand')}>Copy text</button>
          {p.warning && <span className="err" style={{ flex: '1 1 100%', fontSize: 12 }}>! {p.warning}</span>}
        </div>
      ))}
      <span className="muted" style={{ fontSize: 12, paddingTop: 8 }}>Text is trimmed to each platform’s limits. Files are also saved in the exports folder on your laptop.</span>
    </div>
  )
}

function Card({ c, reload }) {
  const { toast, refresh } = useApp()
  const [open, setOpen] = useState(false)
  const [fallback, setFallback] = useState('')
  const [copied, setCopied] = useState(false)
  const done = c.render_status === 'done'
  const dur = Math.round(c.end_s - c.start_s)

  const call = async (fn, msg) => { try { await fn(); if (msg) toast(msg); reload(); refresh() } catch (e) { toast(e.message) } }
  const recut = () => call(() => api(`/candidates/${c.id}/render`, { method: 'POST' }), 'Cutting')
  const copy = async () => {
    const text = postText(c)
    if (await copyText(text)) { setCopied(true); setFallback(''); setTimeout(() => setCopied(false), 2000) } else setFallback(text)
  }
  const patch = (body, msg) => call(() => api(`/candidates/${c.id}`, { method: 'PATCH', body: JSON.stringify(body) }), msg)

  return (
    <article className="card row" style={{ gap: 16, alignContent: 'flex-start', alignItems: 'flex-start' }}>
      <div className="thumb">
        {done && c.vertical_url
          ? <video src={c.vertical_url} controls preload="metadata" />
          : <span className="mono" style={{ position: 'absolute', left: 8, right: 8, bottom: 8, fontSize: 10, lineHeight: 1.3, color: 'rgba(255,253,249,.7)' }}>9:16 clip · {dur}s</span>}
      </div>
      <div className="col" style={{ flex: '1 1 240px', minWidth: 0, gap: 9 }}>
        <span className="mono muted" style={{ fontSize: 12 }}>AI {c.ai_score == null ? '—' : c.ai_score.toFixed(1)} · {c.content_type} · {dur}s</span>
        <span style={{ font: '600 17px/1.3 Instrument Sans' }}>{c.title || 'Untitled moment'}</span>
        {c.hook && <span className="muted2" style={{ fontSize: 14, lineHeight: 1.45 }}>“{c.hook}”</span>}

        {c.render_status === 'rendering' && <div className="col" style={{ gap: 6 }}><span className="infoc" style={{ fontSize: 14 }}>◐ Cutting; about a minute…</span><Progress pct={null} /></div>}
        {c.render_status === 'failed' && (
          <div className="col"><span className="err" style={{ fontSize: 14, lineHeight: 1.45 }}>✕ The cut failed. {c.render_error}</span>
            <div><button className="btn dark" style={{ minHeight: 38 }} onClick={recut}>✂ Cut clip again</button></div></div>
        )}
        {c.render_status === 'none' && <div><button className="btn dark" style={{ minHeight: 38 }} onClick={recut}>✂ Cut clip</button></div>}

        {done && (
          <>
            <div className="row" style={{ gap: 6, marginTop: 2 }}>
              <a className="btn primary" style={{ minHeight: 40 }} href={`/api/candidates/${c.id}/download/vertical`}>⬇ Download 9:16</a>
              <a className="btn" style={{ minHeight: 40 }} href={`/api/candidates/${c.id}/download/wide`}>⬇ Download 16:9</a>
              <button className="btn" style={{ minHeight: 40 }} onClick={copy}>{copied ? '✓ Copied' : 'Copy title + caption'}</button>
            </div>
            <span className="muted" style={{ fontSize: 12 }}>9:16 for Shorts, Reels and TikTok · 16:9 for YouTube</span>
            {fallback && (
              <div className="col" style={{ gap: 5 }}>
                <span className="muted2" style={{ fontSize: 13 }}>Couldn’t copy automatically. Select and copy this text:</span>
                <textarea className="f" readOnly rows={4} value={fallback} onFocus={(e) => e.target.select()} />
              </div>
            )}
            <div className="row" style={{ gap: '6px 14px' }}>
              <button className="btn link" style={{ fontSize: 13 }} aria-expanded={open} onClick={() => setOpen(!open)}>{open ? 'Hide platform exports' : 'Export for each platform'}</button>
              {!c.posted_at && <button className="btn link" style={{ fontSize: 13, color: 'var(--ink)' }} onClick={() => patch({ posted: true }, 'Marked as posted')}>✓ Mark as posted</button>}
              {c.posted_at && <button className="btn link" style={{ fontSize: 13, color: 'var(--mut)', fontWeight: 500 }} onClick={() => patch({ posted: false }, 'Moved back to waiting')}>Not posted after all</button>}
            </div>
          </>
        )}
      </div>
      {done && open && <Export c={c} />}
      {done && c.posted_at && <Results c={c} reload={reload} />}
    </article>
  )
}

export default function Ready() {
  const { toast, refresh } = useApp()
  const { data, loaded, reload } = useLoad('/candidates?status=approved')
  const [tab, setTab] = useState('waiting')
  if (!loaded) return <p className="muted">Loading…</p>

  const all = data || []
  const waiting = all.filter((c) => !c.posted_at)
  const posted = all.filter((c) => c.posted_at)
  const renderable = all.filter((c) => c.render_status === 'none' || c.render_status === 'failed')
  const list = tab === 'waiting' ? waiting : tab === 'posted' ? posted : all
  const tabs = [['waiting', `Waiting (${waiting.length})`], ['posted', `Posted (${posted.length})`], ['all', `All (${all.length})`]]
  const renderAll = async () => { try { const r = await api('/candidates/render-approved', { method: 'POST' }); toast(`Cutting ${r.queued.length} clip${r.queued.length === 1 ? '' : 's'}, one after another`); reload(); refresh() } catch (e) { toast(e.message) } }

  return (
    <>
      <div className="row" style={{ justifyContent: 'space-between', alignItems: 'flex-end', gap: 12, marginBottom: 18 }}>
        <div className="col" style={{ maxWidth: 640, gap: 6 }}>
          <h1 className="page">Ready to post</h1>
          <p className="muted" style={{ margin: 0, fontSize: 15, lineHeight: 1.5 }}>Download, copy the caption, post by hand. A few days later, add the views so the app learns what works.</p>
        </div>
        <div className="seg" role="tablist">{tabs.map(([k, l]) => <button key={k} role="tab" aria-selected={tab === k} onClick={() => setTab(k)}>{l}</button>)}</div>
      </div>

      {renderable.length > 0 && (
        <div className="card row" style={{ gap: 12, padding: '12px 16px', marginBottom: 16 }}>
          <button className="btn dark" style={{ minHeight: 40, fontWeight: 600, fontSize: 14 }} onClick={renderAll}>✂ Render all approved ({renderable.length})</button>
          <span className="muted2" style={{ flex: '1 1 260px', fontSize: 13 }}>Clips are cut one at a time, about a minute each, so your laptop stays usable.</span>
        </div>
      )}

      {list.length === 0 && (
        <Empty>{tab === 'posted' ? 'Nothing marked as posted yet. When you post a clip, press “Mark as posted” on it.'
          : all.length === 0 ? 'Nothing approved yet. Approve moments in Review and the cut clips appear here.' : 'Nothing waiting. Everything cut has been posted.'}</Empty>
      )}
      <div className="grid-ready">{list.map((c) => <Card key={c.id} c={c} reload={reload} />)}</div>
    </>
  )
}
