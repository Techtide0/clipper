import { useCallback, useEffect, useState } from 'react'
import { api } from './api'
import { Empty, LoadError, Progress, StatusBadge, ago, lengthText, sizeText, useApp, useLoad } from './ui'

const FILTERS = [
  ['all', 'All', () => true],
  ['analyzing', 'Analyzing', (v) => v.status === 'processing' || v.status === 'queued' || v.status === 'new'],
  ['analyzed', 'Analyzed', (v) => v.status === 'analyzed'],
  ['attention', 'Needs attention', (v) => v.status === 'failed' || (v.status === 'analyzed' && v.unscored > 0)],
]

function Row({ v, reload }) {
  const { toast, refresh } = useApp()
  const [confirm, setConfirm] = useState(null) // 'delete' | 'remove'
  const act = async (path, msg, opts = { method: 'POST' }) => {
    try { await api(path, opts); setConfirm(null); if (msg) toast(msg); reload(); refresh() } catch (e) { toast(e.message) }
  }
  const scored = v.moments - v.unscored
  const busy = v.status === 'processing' || v.status === 'queued'
  const size = sizeText(v.full_size_mb)

  return (
    <article className="card col" style={{ gap: 12 }}>
      <div className="row" style={{ gap: '12px 20px', alignItems: 'flex-start' }}>
        <div className="col" style={{ flex: '1 1 360px', minWidth: 0, gap: 7 }}>
          <div className="row"><StatusBadge status={v.status} /><span className="mono muted" style={{ fontSize: 12 }}>{v.platform} · {lengthText(v.duration_s)} · {v.content_type}</span></div>
          <span style={{ font: '600 16px/1.35 Instrument Sans' }}>{v.title}</span>
          <span className="muted" style={{ fontSize: 13 }}>{v.channel} · added {ago(v.added)}</span>
          {v.status === 'processing' && <div className="col" style={{ maxWidth: 420, gap: 5 }}><span className="muted2" style={{ fontSize: 13 }}>Finding moments and scoring them; a few minutes.</span><Progress pct={null} /></div>}
          {v.status === 'queued' && <span className="muted2" style={{ fontSize: 13 }}>Waiting for the current analysis to finish.</span>}
          {v.status === 'new' && <span className="muted2" style={{ fontSize: 13 }}>Found on the channel; analysis starts with the next automatic check.</span>}
          {v.status === 'failed' && <span className="err" style={{ fontSize: 13, lineHeight: 1.45 }}>✕ {v.error || 'The analysis failed.'}</span>}
          {v.status === 'analyzed' && v.unscored > 0 && <span className="warnc" style={{ fontSize: 13 }}>! Scored {scored} of {v.moments}. The AI service was busy; the rest can be scored later.</span>}
        </div>
        <div className="col" style={{ flex: '0 1 auto', gap: 4, minWidth: 150, fontSize: 13 }}>
          <b style={{ fontSize: 15 }}>{v.moments} moment{v.moments === 1 ? '' : 's'}</b>
          <span className="muted">{v.moments ? `${scored} scored by the AI` : 'none yet'}</span>
          <span className="mono muted" style={{ fontSize: 12 }}>{v.clips_mb ? `${sizeText(v.clips_mb)} of clips` : 'no clips cut'}</span>
        </div>
      </div>

      <div className="row" style={{ borderTop: '1px solid var(--line3)', paddingTop: 12 }}>
        {(!v.full_status || v.full_status === 'none') && (
          <button className="btn" title="Downloads the whole original video (up to 1080p). A long stream can be several GB." onClick={() => act(`/videos/${v.id}/full-download`, 'Downloading the full video')}>⬇ Full video ({lengthText(v.duration_s)})</button>
        )}
        {v.full_status === 'queued' && <span className="pill info">◷ waiting to download…</span>}
        {v.full_status === 'downloading' && <span className="pill info" style={{ gap: 10 }}>downloading {v.full_progress || 0}%<span style={{ width: 70 }}><Progress pct={v.full_progress || 0} /></span></span>}
        {v.full_status === 'done' && (
          <>
            <a className="btn dark" href={`/api/videos/${v.id}/full`}>⬇ Save full video{size && ` (${size})`}</a>
            <button className="btn" title="Deletes the downloaded file to free disk space. Your clips are unaffected." onClick={() => setConfirm('delete')}>Delete copy</button>
          </>
        )}
        {v.full_status === 'failed' && (
          <><span className="err" style={{ flex: '1 1 280px', fontSize: 13, lineHeight: 1.4 }}>✕ {v.full_error}</span>
            <button className="btn" onClick={() => act(`/videos/${v.id}/full-download`, 'Trying again')}>Retry</button></>
        )}
        <span style={{ flex: 1 }} />
        {v.status === 'failed' && <button className="btn dark" onClick={() => act(`/videos/${v.id}/reanalyze`, 'Queued again')}>Retry analysis</button>}
        {v.status === 'analyzed' && <button className="btn ghost" onClick={() => act(`/videos/${v.id}/reanalyze`, 'Analyzing again')}>Re-analyze</button>}
        {v.status === 'analyzed' && v.unscored > 0 && <button className="btn" onClick={() => act(`/videos/${v.id}/rescore`, 'Scoring the rest…')}>Score the rest</button>}
        {!busy && <button className="btn danger-ghost" onClick={() => setConfirm('remove')}>Remove</button>}
      </div>

      {confirm === 'delete' && (
        <div role="alertdialog" className="row" style={{ background: 'var(--bg)', borderRadius: 8, padding: 12, fontSize: 14 }}>
          <span style={{ flex: '1 1 260px' }}>Delete the downloaded copy? This frees {size || 'the space'}. Your clips are unaffected.</span>
          <button className="btn danger" onClick={() => act(`/videos/${v.id}/full`, 'Copy deleted', { method: 'DELETE' })}>Delete copy</button>
          <button className="btn" onClick={() => setConfirm(null)}>Keep it</button>
        </div>
      )}
      {confirm === 'remove' && (
        <div role="alertdialog" className="row" style={{ background: 'var(--bg)', borderRadius: 8, padding: 12, fontSize: 14 }}>
          <span style={{ flex: '1 1 260px' }}>Remove this video? {v.moments ? `Its ${v.moments} moment${v.moments === 1 ? '' : 's'} and any cut clips go too.` : 'Nothing else is affected.'}</span>
          <button className="btn danger" onClick={() => act(`/videos/${v.id}`, 'Video removed', { method: 'DELETE' })}>Remove</button>
          <button className="btn" onClick={() => setConfirm(null)}>Cancel</button>
        </div>
      )}
    </article>
  )
}

export default function Videos() {
  const { tick } = useApp()
  const first = useLoad('/videos?limit=100')
  const [filter, setFilter] = useState('all')
  const [olderPages, setOlderPages] = useState(0)
  const [older, setOlder] = useState([])
  // the server hands out 100 at a time; each "show older" press brings one more page
  const loadOlder = useCallback(async () => {
    if (!olderPages) { setOlder([]); return }
    try {
      const pages = await Promise.all(Array.from({ length: olderPages }, (_, i) => api(`/videos?limit=100&offset=${100 * (i + 1)}`)))
      setOlder(pages.flat())
    } catch { /* the first page is still shown; the button can be pressed again */ }
  }, [olderPages])
  useEffect(() => { loadOlder() }, [loadOlder, tick])
  const reload = () => { first.reload(); loadOlder() }
  if (first.error && !first.data) return <LoadError error={first.error} retry={first.reload} />
  if (!first.loaded) return <p className="muted">Loading…</p>
  const vids = [...(first.data || []), ...older]
  const more = olderPages ? older.length === 100 * olderPages : (first.data || []).length === 100
  const f = FILTERS.find((x) => x[0] === filter)
  const rows = vids.filter(f[2])
  return (
    <>
      <div className="col" style={{ gap: 6, marginBottom: 18 }}>
        <h1 className="page">Videos</h1>
        <p className="muted" style={{ margin: 0, fontSize: 15 }}>Everything Clipper has looked at, newest first.</p>
      </div>
      <div className="chips" role="tablist" aria-label="Filter videos">
        {FILTERS.map(([k, label, fn]) => <button key={k} role="tab" aria-selected={filter === k} onClick={() => setFilter(k)}>{label} ({vids.filter(fn).length})</button>)}
      </div>
      {rows.length === 0 && <Empty>{vids.length === 0 ? 'No videos yet. Paste a link on Today or follow a channel.' : 'No videos match this filter.'}</Empty>}
      <div className="col" style={{ gap: 10 }}>{rows.map((v) => <Row key={v.id} v={v} reload={reload} />)}</div>
      {more && <div style={{ marginTop: 14 }}><button className="btn" onClick={() => setOlderPages(olderPages + 1)}>Show older videos</button></div>}
    </>
  )
}
