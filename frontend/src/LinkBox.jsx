import { useState } from 'react'
import { api } from './api'
import { useApp } from './ui'

// Paste a YouTube, Twitch or Kick video link; the server reads it and analyzes it in the background.
export default function LinkBox({ onAdded }) {
  const { toast, refresh } = useApp()
  const [url, setUrl] = useState('')
  const [type, setType] = useState('')
  const [busy, setBusy] = useState(false)
  const [state, setState] = useState(null) // { ok, text, title }

  const submit = async (e) => {
    e?.preventDefault()
    const sent = url
    setBusy(true)
    setState(null)
    try {
      const r = await api('/videos/from-url', { method: 'POST', body: JSON.stringify({ url: sent, content_type: type || null }) })
      setUrl((cur) => (cur === sent ? '' : cur)) // keep anything typed while this was sending
      setState({ ok: true, title: r.title, text: r.queued ? 'Added. Analysis takes a few minutes; moments appear under Review.' : `Already added (${r.status}).` })
      if (r.queued) toast('Analyzing. You can close this page; it keeps going.')
      refresh()
      onAdded?.()
    } catch (err) { setState({ ok: false, text: err.message }) }
    setBusy(false)
  }

  return (
    <form className="card col" style={{ padding: 18, gap: 10, marginBottom: 22 }} onSubmit={submit}>
      <label htmlFor="clp-link" className="row" style={{ gap: '4px 10px', alignItems: 'baseline' }}>
        <b style={{ fontSize: 15 }}>Analyze a video</b>
        <span className="muted" style={{ fontSize: 13 }}>Paste a YouTube, Twitch or Kick link</span>
      </label>
      <div className="row" style={{ alignItems: 'stretch' }}>
        <input id="clp-link" className="f" style={{ flex: '1 1 320px', width: 'auto' }} value={url} autoComplete="off"
          onChange={(e) => setUrl(e.target.value)} placeholder="https://www.youtube.com/watch?v=…" />
        <select className="f" aria-label="Video type" style={{ flex: '0 1 190px', width: 'auto' }} value={type} onChange={(e) => setType(e.target.value)}>
          <option value="">type: channel default</option>
          <option value="gameplay">gameplay</option><option value="news">news</option><option value="commentary">commentary</option>
        </select>
        <button className="btn primary lg" disabled={busy || !url.trim()} style={{ opacity: busy || !url.trim() ? 0.55 : 1 }}>
          {busy ? 'Reading link…' : 'Analyze'}
        </button>
      </div>
      <div aria-live="polite" className="col" style={{ gap: 6, fontSize: 14 }}>
        {busy && <span className="muted">Reading link…</span>}
        {state?.ok && (
          <div className="row" style={{ gap: '6px 10px', alignItems: 'baseline' }}>
            <span className="mono goodc" style={{ fontWeight: 600, fontSize: 12 }}>✓ {state.text}</span>
            <span>“{state.title}”</span>
          </div>
        )}
        {state && !state.ok && <span className="err">✕ {state.text}</span>}
      </div>
    </form>
  )
}
