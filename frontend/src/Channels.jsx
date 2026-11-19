import { useState } from 'react'
import { api } from './api'
import { Empty, ago, useApp, useLoad } from './ui'

const TYPES = ['gameplay', 'news', 'commentary']

function Auto() {
  const { toast, refresh } = useApp()
  const { data: a, reload } = useLoad('/automation')
  if (!a) return null
  const toggle = async () => {
    try { await api('/settings', { method: 'PUT', body: JSON.stringify({ auto_mode: !a.auto_mode }) }); reload(); refresh() } catch (e) { toast(e.message) }
  }
  const runNow = async () => { try { await api('/automation/run', { method: 'POST' }); toast('Checking now'); reload() } catch (e) { toast(e.message) } }
  const every = a.interval_minutes % 60 === 0 ? (a.interval_minutes === 60 ? 'hour' : `${a.interval_minutes / 60} hours`) : `${a.interval_minutes} minutes`
  return (
    <div className="card row" style={{ gap: 16, alignItems: 'flex-start', padding: 18, marginBottom: 14, flexWrap: 'nowrap' }}>
      <button className="switch" style={{ marginTop: 2 }} role="switch" aria-checked={a.auto_mode} aria-label="Automatic mode" onClick={toggle}><span /></button>
      <div className="col" style={{ gap: 4, minWidth: 0, flex: 1 }}>
        <span style={{ font: '600 17px Instrument Sans' }}>Automatic mode <span className="mono" style={{ fontSize: 11, color: a.auto_mode ? 'var(--good)' : 'var(--mut)' }}>{a.auto_mode ? 'ON' : 'OFF'}</span></span>
        <span className="muted2" style={{ fontSize: 14, lineHeight: 1.45 }}>Check every {every} and analyze new videos. Moments land in To review; you still decide what gets cut.</span>
        <span className="mono muted" style={{ fontSize: 12 }}>
          {a.running ? 'Checking now…' : a.last_run ? `Last check ${ago(a.last_run)}${a.last_summary ? ` · ${a.last_summary}` : ''}` : 'Hasn’t checked yet'}
          {a.auto_mode && !a.running && a.next_run && a.next_run !== 'soon' ? ' · next check due soon' : ''}
        </span>
        <div><button className="btn link" style={{ fontSize: 13 }} disabled={a.running} onClick={runNow}>Check now</button></div>
      </div>
    </div>
  )
}

function Row({ s, reload }) {
  const { toast, refresh } = useApp()
  const [checking, setChecking] = useState(false)
  const patch = async (body, msg) => { try { await api(`/streamers/${s.id}`, { method: 'PATCH', body: JSON.stringify(body) }); if (msg) toast(msg); reload(); refresh() } catch (e) { toast(e.message) } }
  const check = async () => {
    setChecking(true)
    try { const r = await api(`/streamers/${s.id}/scan`, { method: 'POST' }); toast(r.new ? `${r.new} new video${r.new === 1 ? '' : 's'} found` : 'Nothing new') } catch (e) { toast(e.message) }
    setChecking(false); reload(); refresh()
  }
  return (
    <article className="card row" style={{ gap: '10px 18px', padding: '14px 16px', opacity: s.tracked ? 1 : 0.6 }}>
      <div className="col" style={{ flex: '1 1 240px', minWidth: 0, gap: 3 }}>
        <span style={{ font: '600 16px Instrument Sans' }}>{s.name} <span className="mono muted" style={{ fontSize: 12, fontWeight: 500 }}>{s.platform}</span></span>
        <span className="muted" style={{ fontSize: 13 }}>
          {s.videos} video{s.videos === 1 ? '' : 's'}{s.waiting ? ` · ${s.waiting} waiting` : ''} · {s.last_scan_at ? `checked ${ago(s.last_scan_at)}` : 'not checked yet'}
        </span>
        {s.last_scan_error && <span className="err" style={{ fontSize: 13 }}>✕ {s.last_scan_error}</span>}
      </div>
      <select className="f" aria-label={`Type for ${s.name}`} style={{ width: 'auto', minHeight: 38, fontSize: 13 }} value={s.content_type} onChange={(e) => patch({ content_type: e.target.value })}>
        {TYPES.map((t) => <option key={t}>{t}</option>)}
      </select>
      <button className="btn" style={{ minHeight: 38 }} disabled={checking} onClick={check}>{checking ? 'Checking…' : 'Check now'}</button>
      <button className="btn" style={{ minHeight: 38, fontWeight: 600, ...(s.tracked ? { background: 'var(--good-bg)', color: 'var(--good)', borderColor: 'transparent' } : {}) }}
        aria-pressed={s.tracked} onClick={() => patch({ tracked: !s.tracked }, s.tracked ? 'Stopped following' : 'Following again')}>{s.tracked ? '✓ Following' : 'Follow'}</button>
    </article>
  )
}

export default function Channels() {
  const { toast, refresh } = useApp()
  const { data, loaded, reload } = useLoad('/streamers')
  const [url, setUrl] = useState('')
  const [type, setType] = useState('gameplay')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  if (!loaded) return <p className="muted">Loading…</p>

  const add = async (e) => {
    e.preventDefault()
    setBusy(true); setErr('')
    try {
      const guess = url.replace(/[?#].*$/, '').replace(/\/+$/, '').split('/').pop().replace(/^@/, '') || url
      const r = await api(`/streamers?name=${encodeURIComponent(guess)}&channel_url=${encodeURIComponent(url.trim())}&content_type=${type}`, { method: 'POST' })
      setUrl(''); toast(r.created ? 'Channel added' : 'You already follow this channel'); reload(); refresh()
    } catch (x) { setErr(x.message) }
    setBusy(false)
  }
  const rows = data || []
  return (
    <>
      <div className="col" style={{ gap: 6, marginBottom: 18 }}>
        <h1 className="page">Channels</h1>
        <p className="muted" style={{ margin: 0, fontSize: 15 }}>Clipper checks these for new videos and analyzes them for you.</p>
      </div>
      <Auto />
      <form className="card col" style={{ gap: 8, marginBottom: 20 }} onSubmit={add}>
        <label htmlFor="clp-chan" style={{ fontWeight: 600, fontSize: 14 }}>Add a channel</label>
        <div className="row">
          <input id="clp-chan" className="f" style={{ flex: '1 1 300px', width: 'auto' }} value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://www.youtube.com/@IGN or twitch.tv/name" />
          <select className="f" aria-label="Channel type" style={{ flex: '0 1 160px', width: 'auto' }} value={type} onChange={(e) => setType(e.target.value)}>{TYPES.map((t) => <option key={t}>{t}</option>)}</select>
          <button className="btn dark lg" disabled={busy || !url.trim()}>{busy ? 'Adding…' : 'Add channel'}</button>
        </div>
        {err && <span className="err" style={{ fontSize: 13 }}>✕ {err}</span>}
        <span className="muted" style={{ fontSize: 12 }}>The type decides how the AI judges this channel’s videos.</span>
      </form>
      {rows.length === 0 && <Empty>You’re not following anyone yet. Add a channel and Clipper will pick up its new videos.</Empty>}
      <div className="col" style={{ gap: 10 }}>{rows.map((s) => <Row key={s.id} s={s} reload={reload} />)}</div>
      <p className="muted" style={{ margin: '16px 0 0', fontSize: 13, lineHeight: 1.5, maxWidth: 680 }}>Chat signals only work for some YouTube videos. Twitch and Kick moments are picked on the transcript and loudness alone.</p>
    </>
  )
}
