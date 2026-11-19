import { useEffect, useState } from 'react'
import { api } from './api'
import { sizeText, useApp, useDebounced, useLoad } from './ui'

const THEMES = [['light', 'Light'], ['dark', 'Dark'], ['system', 'System']]
const WEIGHT_LABELS = { audio: 'Loudness', chat: 'Chat activity', keywords: 'Keywords' }
const UNITS = { auto_interval_minutes: 'min', auto_videos_per_cycle: '', auto_max_video_minutes: 'min', shortlist_size: '', full_download_headroom_gb: 'GB' }

function Num({ spec, onSave }) {
  const [v, setV] = useState(String(spec.value))
  const [err, setErr] = useState('')
  useEffect(() => setV(String(spec.value)), [spec.value])
  const commit = async () => {
    const n = Number(v)
    if (v.trim() === '' || Number.isNaN(n)) { setErr('Enter a number.'); return }
    if (n < spec.min || n > spec.max) { setErr(`Between ${spec.min} and ${spec.max}.`); return }
    if (n === spec.value) { setErr(''); return }
    setErr(await onSave(spec.key, n))
  }
  return (
    <div className="row" style={{ justifyContent: 'space-between', gap: 12 }}>
      <label className="col" style={{ flex: '1 1 200px', gap: 2, fontWeight: 600, fontSize: 14 }}>{spec.label}<span className="muted" style={{ fontWeight: 400, fontSize: 12 }}>{spec.help}</span>{err && <span className="err" style={{ fontWeight: 400, fontSize: 12 }}>✕ {err}</span>}</label>
      <div className="row" style={{ gap: 6 }}>
        <input type="number" className="f num" aria-label={spec.label} style={{ width: 84 }} min={spec.min} max={spec.max} value={v} onChange={(e) => setV(e.target.value)} onBlur={commit} onKeyDown={(e) => e.key === 'Enter' && e.currentTarget.blur()} />
        <span className="mono muted" style={{ fontSize: 12, width: 30 }}>{UNITS[spec.key] || ''}</span>
      </div>
    </div>
  )
}

function Youtube() {
  const { toast } = useApp()
  const { data: y, reload } = useLoad('/youtube')
  const [polling, setPolling] = useState(false)
  useEffect(() => {
    if (!polling) return undefined
    const t = setInterval(async () => { await reload(); }, 2500)
    return () => clearInterval(t)
  }, [polling, reload])
  useEffect(() => { if (y && !y.connecting && polling) setPolling(false) }, [y, polling])
  if (!y) return null
  const call = async (path, msg) => { try { await api(path, { method: 'POST' }); if (msg) toast(msg); reload() } catch (e) { toast(e.message) } }
  const connect = async () => { await call('/youtube/connect'); setPolling(true) }
  return (
    <section className="sect">
      <h2>Accounts</h2>
      <div className="row" style={{ gap: 10 }}>
        <span style={{ flex: '1 1 200px', fontSize: 14 }}><b>YouTube</b> <span className="muted">· {y.connected ? `connected${y.channel ? ` as ${y.channel}` : ''}` : y.connecting ? 'waiting for you to sign in in the new window…' : 'not connected'}</span></span>
        {y.connected
          ? <><button className="btn" onClick={() => call('/youtube/refresh-stats', 'Views updated')}>Refresh views</button><button className="btn" onClick={() => call('/youtube/disconnect', 'Disconnected')}>Disconnect</button></>
          : <button className="btn" disabled={!y.configured || y.connecting} onClick={connect}>Connect YouTube</button>}
      </div>
      {y.connect_error && <span className="err" style={{ fontSize: 13 }}>✕ {y.connect_error}</span>}
      {!y.configured && !y.connected && (
        <details className="how"><summary>Set up posting to YouTube</summary>
          <ol style={{ margin: '10px 0 0', paddingLeft: 18 }}>{(y.setup_steps || []).map((s, i) => <li key={i}>{s}</li>)}</ol>
          <p className="muted" style={{ fontSize: 12 }}>Save the file as {y.secrets_file}</p>
        </details>
      )}
      <span className="muted" style={{ fontSize: 12 }}>Clips are uploaded as {y.privacy} by default. You can still download and post by hand.</span>
    </section>
  )
}

export default function Settings() {
  const { toast, theme, setTheme, refresh } = useApp()
  const health = useLoad('/health')
  const sets = useLoad('/settings')
  const weights = useLoad('/weights')
  const [w, setW] = useState(null)

  useEffect(() => { if (weights.data) setW(weights.data.current) }, [weights.data])
  const saveW = useDebounced(async (next) => {
    try { await api('/weights', { method: 'PUT', body: JSON.stringify(next) }); toast('Saved') } catch (e) { toast(e.message); weights.reload() }
  }, 700)

  const save = async (key, value) => {
    try { await api('/settings', { method: 'PUT', body: JSON.stringify({ [key]: value }) }); sets.reload(); refresh(); return '' } catch (e) { return e.message }
  }
  const saveOrTell = async (key, value) => { const err = await save(key, value); if (err) { toast(err); sets.reload() } }
  const post = async (path, fmt) => { try { const r = await api(path, { method: 'POST' }); toast(fmt(r)); health.reload() } catch (e) { toast(e.message) } }

  if (!health.loaded || !sets.loaded) return <p className="muted">Loading…</p>
  const h = health.data, specs = sets.data || []
  const by = Object.fromEntries(specs.map((s) => [s.key, s]))
  const st = h?.storage_mb || {}
  const clips = st.clips || 0, exports = st.exports || 0, full = st['full videos'] || 0
  const used = Math.max(1, clips + exports + full + (st['moment audio'] || 0) + (st['working files'] || 0))
  const pct = (n) => `${(n / used) * 100}%`

  return (
    <>
      <div className="col" style={{ gap: 6, marginBottom: 18 }}>
        <h1 className="page">Settings &amp; health</h1>
        <p className="muted" style={{ margin: 0, fontSize: 15 }}>Set it up once. This page tells you if something stops working.</p>
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(min(100%,440px),1fr))', gap: 16, alignItems: 'start' }}>
        <div className="col" style={{ gap: 16 }}>
          <section className="card" style={{ padding: '6px 18px 8px' }}>
            <h2 className="eyebrow" style={{ margin: '12px 0 4px' }}>Health</h2>
            <div className="list">
              {(h?.checks || []).map((c) => (
                <div key={c.name} className="row" style={{ alignItems: 'flex-start', gap: 12 }}>
                  <span className={`pill ${c.ok ? 'good' : c.required ? 'bad' : 'warn'}`}>{c.ok ? '✓ OK' : c.required ? '✕ Problem' : '! Note'}</span>
                  <span className="col" style={{ flex: '1 1 220px', gap: 2, fontSize: 14 }}><b>{c.name}</b><span className="muted" style={{ fontSize: 13, lineHeight: 1.4 }}>{c.ok ? c.detail : `${c.detail}. ${c.fix}`}</span></span>
                </div>
              ))}
            </div>
          </section>

          <section className="sect">
            <h2>Storage</h2>
            <div aria-hidden="true" style={{ display: 'flex', height: 12, borderRadius: 6, overflow: 'hidden', background: 'var(--track)' }}>
              <span style={{ width: pct(clips), background: 'var(--acc)' }} /><span style={{ width: pct(exports), background: 'oklch(0.72 0.14 55)' }} /><span style={{ width: pct(full), background: 'var(--info)' }} />
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr auto', gap: '6px 12px', fontSize: 14 }}>
              <span><i style={{ color: 'var(--acc)', fontStyle: 'normal' }}>■</i> Clips</span><span className="mono">{sizeText(clips)}</span>
              <span><i style={{ color: 'oklch(0.72 0.14 55)', fontStyle: 'normal' }}>■</i> Exports</span><span className="mono">{sizeText(exports)}</span>
              <span><i style={{ color: 'var(--info)', fontStyle: 'normal' }}>■</i> Full downloads</span><span className="mono">{sizeText(full)}</span>
              <span className="muted">Free on this laptop</span><span className="mono muted">{h?.free_disk_gb} GB</span>
            </div>
            <div className="row">
              <button className="btn dark" style={{ minHeight: 38 }} onClick={() => post('/cleanup', (r) => (r.removed ? `Freed ${sizeText(r.freed_mb)}` : 'Nothing old enough to clean up'))}>Clean up old files</button>
              <button className="btn" style={{ minHeight: 38 }} onClick={() => post('/recover', (r) => (r.recovered ? `Freed ${r.recovered} stalled item${r.recovered === 1 ? '' : 's'}` : 'Nothing was stalled'))}>Recover stalled items</button>
            </div>
            <span className="muted" style={{ fontSize: 12, lineHeight: 1.45 }}>Clean up removes working files and exports older than 30 days. Your cut clips and full downloads stay.</span>
          </section>

          <Youtube />
        </div>

        <div className="col" style={{ gap: 16 }}>
          <section className="sect" style={{ gap: 16 }}>
            <h2>Finding moments</h2>
            {by.judge_min_score && (
              <div className="col" style={{ gap: 6 }}>
                <div className="row" style={{ justifyContent: 'space-between' }}><label htmlFor="s-thr" style={{ fontWeight: 600, fontSize: 14 }}>Auto-reject below</label><span className="mono" style={{ fontWeight: 600, fontSize: 14 }}>{by.judge_min_score.value} / 10</span></div>
                <input id="s-thr" type="range" min="0" max="10" step="0.5" value={by.judge_min_score.value} style={{ accentColor: 'var(--acc)', width: '100%' }}
                  onChange={(e) => saveOrTell('judge_min_score', Number(e.target.value))} />
                <span className="muted" style={{ fontSize: 12 }}>Moments scoring under this go straight to Rejected. You can still rescue them.</span>
              </div>
            )}
            {['auto_interval_minutes', 'auto_videos_per_cycle', 'auto_max_video_minutes', 'shortlist_size'].filter((k) => by[k]).map((k) => <Num key={k} spec={by[k]} onSave={save} />)}
          </section>

          <section className="sect">
            <h2>How much each signal counts</h2>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(120px,1fr))', gap: 10 }}>
              {w && Object.keys(w).map((k) => (
                <label key={k} className="col muted2" style={{ gap: 4, fontSize: 13 }}>{WEIGHT_LABELS[k] || k}
                  <input type="number" className="f num" step="0.1" min="0.1" max="3" style={{ width: '100%' }} value={w[k]}
                    onChange={(e) => { const next = { ...w, [k]: e.target.value === '' ? '' : Number(e.target.value) }; setW(next); if (next[k] !== '') saveW(next) }} />
                </label>
              ))}
            </div>
            <span className="muted" style={{ fontSize: 12 }}>Between 0.1 and 3. The Performance page can suggest these for you.</span>
          </section>

          <section className="sect" style={{ gap: 14 }}>
            <h2>Files &amp; appearance</h2>
            {by.delete_working_files && (
              <div className="row" style={{ gap: 12, flexWrap: 'nowrap' }}>
                <button className="switch sm" role="switch" aria-checked={by.delete_working_files.value} aria-label="Delete working files after analysis" onClick={() => saveOrTell('delete_working_files', !by.delete_working_files.value)}><span /></button>
                <span style={{ fontSize: 14 }}>Delete working files after analysis</span>
              </div>
            )}
            {by.full_download_headroom_gb && <Num spec={{ ...by.full_download_headroom_gb, label: 'Full-download safety margin', help: 'Free space kept back when downloading whole videos.' }} onSave={save} />}
            <div className="row" style={{ justifyContent: 'space-between', gap: 12 }}>
              <span style={{ fontSize: 14 }}>Theme</span>
              <div className="seg" role="radiogroup" aria-label="Theme">{THEMES.map(([k, l]) => <button key={k} role="radio" aria-checked={theme === k} onClick={() => setTheme(k)}>{l}</button>)}</div>
            </div>
          </section>
        </div>
      </div>
    </>
  )
}
