import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from './api'
import { AppCtx, Progress } from './ui'
import Today from './Today'
import Review from './Review'
import Ready from './Ready'
import Videos from './Videos'
import Channels from './Channels'
import Performance from './Performance'
import Settings from './Settings'

const SCREENS = ['today', 'review', 'ready', 'videos', 'channels', 'performance', 'settings']
const LABELS = { today: 'Today', review: 'Review', ready: 'Ready to post', videos: 'Videos', channels: 'Channels', performance: 'Performance', settings: 'Settings' }
const VIEW = { today: Today, review: Review, ready: Ready, videos: Videos, channels: Channels, performance: Performance, settings: Settings }
const SHORTCUTS = [
  ['A', 'Approve and cut'], ['R', 'Reject'], ['S', 'Skip'], ['E', 'Edit the title'], ['C', 'Cut it first'],
  ['← →', 'Previous / next moment'], ['Space', 'Load the preview'], ['U', 'Undo the last decision'], ['?', 'Show this list'],
]

const fromHash = () => { const h = window.location.hash.replace('#', ''); return SCREENS.includes(h) ? h : 'today' }
const readTheme = () => { try { return localStorage.getItem('clipper-theme') || 'system' } catch { return 'system' } }

export default function App() {
  const [screen, setScreen] = useState(fromHash)
  const [summary, setSummary] = useState(null)
  const [jobs, setJobs] = useState([])
  const [online, setOnline] = useState(true)
  const [tick, setTick] = useState(0)
  const [theme, setThemeState] = useState(readTheme)
  const [toastState, setToast] = useState(null)
  const [helpOpen, setHelp] = useState(false)
  const [activityOpen, setActivity] = useState(false)
  const sig = useRef('')
  const toastTimer = useRef(null)
  const undoRef = useRef(null)

  const poll = useCallback(async () => {
    try {
      const [s, j] = await Promise.all([api('/summary'), api('/jobs')])
      setSummary(s); setJobs(j); setOnline(true)
      const next = JSON.stringify([s, j.map((x) => [x.kind, x.title, x.pct])])
      if (next !== sig.current) { sig.current = next; setTick((t) => t + 1) }
    } catch { setOnline(false) }
  }, [])

  useEffect(() => {
    poll()
    const id = setInterval(poll, 4000)
    return () => clearInterval(id)
  }, [poll])

  useEffect(() => {
    const on = () => setScreen(fromHash())
    window.addEventListener('hashchange', on)
    return () => window.removeEventListener('hashchange', on)
  }, [])

  useEffect(() => {
    const root = document.documentElement
    root.setAttribute('data-theme', theme) // light, dark or system: the stylesheet handles all three
  }, [theme])

  const setTheme = useCallback((t) => { setThemeState(t); try { localStorage.setItem('clipper-theme', t) } catch { /* private mode */ } }, [])
  const go = useCallback((s) => { window.location.hash = s === 'today' ? '' : s; setScreen(s); setActivity(false); window.scrollTo(0, 0) }, [])

  const toast = useCallback((text, undo) => {
    clearTimeout(toastTimer.current)
    undoRef.current = undo || null
    setToast({ text, undo: !!undo })
    toastTimer.current = setTimeout(() => setToast(null), 6000)
  }, [])
  const undo = useCallback(async () => {
    const fn = undoRef.current
    undoRef.current = null
    setToast(null)
    if (fn) { try { await fn() } catch (e) { toast(e.message) } }
  }, [toast])

  useEffect(() => {
    const onKey = (e) => {
      if (e.key === 'Escape') { setHelp(false); setActivity(false) }
      if (e.key === '?' && !['INPUT', 'TEXTAREA', 'SELECT'].includes(e.target.tagName)) setHelp(true)
      if (e.key === 'Enter' && screen === 'today' && summary?.to_review && e.target === document.body) go('review')
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [screen, summary, go])

  const ctx = useMemo(() => ({ summary, jobs, online, tick, go, toast, refresh: poll, theme, setTheme, helpOpen, setHelp }),
    [summary, jobs, online, tick, go, toast, poll, theme, setTheme, helpOpen])

  const View = VIEW[screen]
  const counts = { review: summary?.to_review, ready: summary?.ready_to_post }
  const busy = jobs.length

  if (!online && !summary) {
    return (
      <div className="col" style={{ minHeight: '100vh', alignItems: 'center', justifyContent: 'center', gap: 14, padding: 24, textAlign: 'center' }}>
        <h1 className="page" style={{ margin: 0 }}>Can’t reach the app’s server.</h1>
        <p className="muted2" style={{ margin: 0, maxWidth: 420, lineHeight: 1.5 }}>Clipper’s server isn’t answering. Start it with start.bat, then try again.</p>
        <button className="btn primary lg" onClick={poll}>Try again</button>
      </div>
    )
  }

  return (
    <AppCtx.Provider value={ctx}>
      <header className="top">
        <div className="top-in">
          <div className="logo"><i /> Clipper</div>
          <nav className="main" aria-label="Main">
            {SCREENS.map((s) => (
              <button key={s} aria-current={screen === s ? 'page' : undefined} onClick={() => go(s)}>
                {LABELS[s]}{counts[s] > 0 && <span className="count">{counts[s]}</span>}
              </button>
            ))}
          </nav>
          <button className="activity" aria-expanded={activityOpen} onClick={() => setActivity(!activityOpen)}>
            <span style={{ color: busy ? 'var(--info)' : 'var(--good)' }}>{busy ? '◐' : '●'}</span>
            {busy ? `${busy} running` : 'Idle'}
          </button>
          <span className="conn" style={{ color: online ? 'var(--good)' : 'var(--bad)' }}><i /> {online ? `connected · ${window.location.host}` : 'offline'}</span>
        </div>
      </header>

      {activityOpen && (
        <>
          <div style={{ position: 'fixed', inset: 0, zIndex: 35 }} onClick={() => setActivity(false)} />
          <div className="popover" role="dialog" aria-label="Background work">
            <b style={{ fontSize: 15 }}>Background work</b>
            {busy === 0 && <span className="muted" style={{ fontSize: 14 }}>Nothing running. Everything is up to date.</span>}
            {jobs.map((j, i) => (
              <div key={`${j.kind}${i}`} className="col" style={{ gap: 6 }}>
                <div className="row" style={{ justifyContent: 'space-between', gap: 10 }}>
                  <span style={{ fontSize: 14, fontWeight: 600, minWidth: 0 }}>{j.title}</span>
                  <span className="mono muted" style={{ fontSize: 12, flex: 'none' }}>{j.kind}</span>
                </div>
                <Progress pct={j.pct} />
                {j.detail && <span className="muted" style={{ fontSize: 12 }}>{j.detail}</span>}
              </div>
            ))}
          </div>
        </>
      )}

      {!online && <div className="banner warn" style={{ maxWidth: 1140, margin: '12px auto 0' }}>The server stopped answering. Showing the last known state; it reconnects on its own.</div>}

      <main className="page" id="main"><View /></main>

      {toastState && (
        <div className="toast" role="status">
          <span>{toastState.text}</span>
          {toastState.undo && <button onClick={undo}>Undo <kbd className="mono" style={{ marginLeft: 4 }}>U</kbd></button>}
        </div>
      )}

      {helpOpen && (
        <div className="scrim" onClick={() => setHelp(false)}>
          <div className="dialog" role="dialog" aria-label="Keyboard shortcuts" onClick={(e) => e.stopPropagation()}>
            <b style={{ fontSize: 18, marginBottom: 8 }}>Keyboard shortcuts</b>
            {SHORTCUTS.map(([k, d]) => (
              <div key={k} className="row" style={{ justifyContent: 'space-between', padding: '5px 0' }}><span style={{ fontSize: 14 }}>{d}</span><kbd className="mono">{k}</kbd></div>
            ))}
            <button className="btn dark" style={{ marginTop: 12 }} onClick={() => setHelp(false)}>Close</button>
          </div>
        </div>
      )}
    </AppCtx.Provider>
  )
}
