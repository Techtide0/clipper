import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import { api } from './api'

export const AppCtx = createContext(null)
export const useApp = () => useContext(AppCtx)

export const mmss = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`

// "14 min", "1 h 05 min": unambiguous, unlike a clock-style 0:14
export const lengthText = (s) => {
  if (!s) return '—'
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  return h ? `${h} h ${String(m).padStart(2, '0')} min` : `${Math.max(1, m)} min`
}

export const sizeText = (mb) => (mb == null ? '' : mb >= 1000 ? `${(mb / 1000).toFixed(1)} GB` : `${Math.round(mb)} MB`)

export const ago = (iso) => {
  if (!iso) return 'never'
  const s = Math.max(0, (Date.now() - new Date(iso + (iso.endsWith('Z') ? '' : 'Z')).getTime()) / 1000)
  if (s < 90) return 'just now'
  if (s < 5400) return `${Math.round(s / 60)} min ago`
  if (s < 129600) return `${Math.round(s / 3600)} h ago`
  return `${Math.round(s / 86400)} days ago`
}

const BADGES = {
  analyzed: ['good', '✓', 'analyzed'], processing: ['info', '◐', 'analyzing'], queued: ['info', '◷', 'queued'],
  failed: ['bad', '✕', 'failed'], new: ['neutral', '◷', 'waiting'],
}
export function StatusBadge({ status }) {
  const [cls, icon, label] = BADGES[status] || ['neutral', '•', status]
  return <span className={`pill ${cls}`}>{icon} {label}</span>
}

export const Pill = ({ cls = 'neutral', children }) => <span className={`pill ${cls}`}>{children}</span>

export function Progress({ pct }) {
  return pct == null
    ? <div className="bar indet" aria-hidden="true"><i /></div>
    : <div className="bar" aria-hidden="true"><i style={{ width: `${Math.max(2, Math.min(100, pct))}%` }} /></div>
}

export const Empty = ({ children }) => <div className="empty">{children}</div>

export const Section = ({ title, children }) => (
  <section className="sect">{title && <h2>{title}</h2>}{children}</section>
)

// Fetch a path now and again whenever the app's live state changes (a video finishes, a cut ends...).
export function useLoad(path) {
  const { tick } = useApp()
  const [state, setState] = useState({ data: null, error: null, loaded: false })
  const reload = useCallback(async () => {
    try { setState({ data: await api(path), error: null, loaded: true }) }
    catch (error) { setState((s) => ({ ...s, error, loaded: true })) }
  }, [path])
  useEffect(() => { reload() }, [reload, tick])
  return { ...state, reload }
}

// Copy to the clipboard; false if the browser refuses (the caller then shows the text to select by hand).
export async function copyText(text) {
  try {
    if (!navigator.clipboard) return false
    await navigator.clipboard.writeText(text)
    return true
  } catch { return false }
}

export const postText = (c) => [c.title, c.caption].filter(Boolean).join('\n\n')

// What a cut-state means for the owner, for cards that show clips.
export function cutState(c) {
  if (c.render_status === 'rendering') return 'cutting'
  if (c.render_status === 'failed') return 'failed'
  if (c.render_status === 'done') return 'done'
  return 'none'
}

// A function that waits until you stop calling it for `ms` before running (for "saves as you type").
export function useDebounced(fn, ms = 600) {
  const timer = useRef(null)
  const latest = useRef(fn)
  useEffect(() => { latest.current = fn })
  useEffect(() => () => clearTimeout(timer.current), [])
  return useCallback((...args) => {
    clearTimeout(timer.current)
    timer.current = setTimeout(() => latest.current(...args), ms)
  }, [ms])
}
