import { api } from './api'
import LinkBox from './LinkBox'
import { Empty, StatusBadge, useApp, useLoad } from './ui'

const Tile = ({ icon, label, fg, n, detail, go }) => (
  <button className="tile" onClick={go}>
    <span className="eyebrow row" style={{ gap: 7, color: fg }}>{icon} {label}</span>
    <span className="n">{n}</span>
    <span className="muted" style={{ fontSize: 13, lineHeight: 1.4 }}>{detail}</span>
  </button>
)

export default function Today() {
  const { summary: s, go, toast, refresh } = useApp()
  const videos = useLoad('/videos?limit=100')
  const pending = useLoad('/candidates?status=pending')
  const approved = useLoad('/candidates?status=approved')
  const health = useLoad('/health')
  if (!s || !videos.loaded) return <p className="muted">Loading…</p>

  const vids = videos.data || []
  const failedCuts = (approved.data || []).concat(pending.data || []).filter((c) => c.render_status === 'failed')
  const failedVideos = vids.filter((v) => v.status === 'failed')
  const partial = vids.filter((v) => v.status === 'analyzed' && v.unscored > 0)
  const attentionN = failedVideos.length + failedCuts.length + partial.length
  const firstRun = vids.length === 0
  const act = async (path, msg) => { try { await api(path, { method: 'POST' }); toast(msg); refresh() } catch (e) { toast(e.message) } }

  if (firstRun) {
    const checks = (health.data?.checks || []).filter((c) => c.name !== 'Hardware video encoding')
    return (
      <section className="col" style={{ gap: 22 }} aria-label="Today">
        <div className="col" style={{ paddingTop: 12 }}>
          <span className="eyebrow" style={{ fontWeight: 500 }}>Welcome</span>
          <h1 className="hero" style={{ fontSize: 'clamp(32px,5vw,48px)', letterSpacing: '-.03em' }}>Let’s find your first clips.</h1>
          <p style={{ margin: 0, fontSize: 17, lineHeight: 1.5, maxWidth: 620 }} className="muted2">
            Paste a YouTube, Twitch or Kick link to find your first clips. Clipper finds the best 30-second moments,
            writes a title and caption for each, and you decide which ones to keep.
          </p>
        </div>
        {checks.length > 0 && (
          <div className="card list" style={{ padding: '6px 18px' }}>
            {checks.map((c) => (
              <div key={c.name} className="row" style={{ gap: 12 }}>
                <span className={`mono ${c.ok ? 'goodc' : 'warnc'}`} style={{ fontWeight: 600, width: 20 }}>{c.ok ? '✓' : '!'}</span>
                <span style={{ flex: '1 1 260px' }}><b>{c.name}</b> <span className="muted">{c.ok ? c.detail : c.fix || c.detail}</span></span>
              </div>
            ))}
          </div>
        )}
        <LinkBox onAdded={videos.reload} />
      </section>
    )
  }

  const toReview = s.to_review
  const pv = new Set((pending.data || []).map((c) => c.video_id)).size
  const headline = toReview ? `${toReview} ${toReview === 1 ? 'moment' : 'moments'} to review` : 'You’re all caught up.'
  const sub = toReview
    ? `From ${pv} ${pv === 1 ? 'video' : 'videos'}, about ${Math.max(1, Math.round(toReview * 0.8))} minutes of reviewing.`
    : `New moments appear here when a video finishes analyzing.${s.ready_to_post ? ` ${s.ready_to_post} clip${s.ready_to_post === 1 ? ' is' : 's are'} ready to post.` : ''}`
  const today = new Date().toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'long' })

  return (
    <section className="col" style={{ gap: 22, marginBottom: 22 }} aria-label="Today">
      <div className="row" style={{ alignItems: 'flex-end', justifyContent: 'space-between', gap: 20, paddingTop: 12 }}>
        <div className="col" style={{ minWidth: 0 }}>
          <span className="eyebrow" style={{ fontWeight: 500 }}>{today}</span>
          <h1 className="hero">{headline}</h1>
          <p style={{ margin: 0, fontSize: 16, lineHeight: 1.5, maxWidth: 640 }} className="muted2">{sub}</p>
        </div>
        {toReview > 0 && (
          <button className="btn primary" style={{ borderRadius: 12, padding: '18px 24px', fontSize: 18, minHeight: 56, gap: 12 }} onClick={() => go('review')}>
            Start reviewing →<kbd style={{ background: 'rgba(255,255,255,.2)' }}>Enter</kbd>
          </button>
        )}
      </div>

      <div className="tiles">
        <Tile icon="◐" label="Analyzing" fg="var(--info)" n={s.analyzing} go={() => go('videos')}
          detail={s.analyzing ? `${s.analyzing} video${s.analyzing === 1 ? '' : 's'} being analyzed` : 'Nothing running'} />
        <Tile icon="✂" label="Cutting" fg="var(--info)" n={s.cutting} go={() => go('ready')}
          detail={s.cutting ? `${s.cutting} clip${s.cutting === 1 ? '' : 's'}, about ${s.cutting} min left` : 'Nothing cutting'} />
        <Tile icon="⬇" label="Ready to post" fg="var(--good)" n={s.ready_to_post} go={() => go('ready')}
          detail={s.ready_to_post ? 'Cut and waiting for you' : 'Nothing waiting'} />
        <Tile icon="!" label="Needs attention" fg="var(--bad)" n={attentionN}
          go={() => document.getElementById('attention')?.scrollIntoView({ behavior: 'smooth' })}
          detail={attentionN ? 'See below' : 'All good'} />
      </div>

      {attentionN > 0 && (
        <div className="col" id="attention" style={{ gap: 8 }}>
          <span className="eyebrow">Needs attention</span>
          <div className="card list" style={{ padding: '4px 16px' }}>
            {failedVideos.map((v) => (
              <div key={`v${v.id}`} className="row" style={{ gap: 12 }}>
                <span className="mono err" style={{ fontWeight: 600, width: 18 }}>✕</span>
                <span style={{ flex: '1 1 300px', fontSize: 14, lineHeight: 1.45, minWidth: 0 }}>
                  <b>{v.title}</b> <span className="muted2">couldn’t be analyzed. {v.error}</span>
                </span>
                <button className="btn" onClick={() => act(`/videos/${v.id}/reanalyze`, 'Queued again')}>Retry</button>
              </div>
            ))}
            {failedCuts.map((c) => (
              <div key={`c${c.id}`} className="row" style={{ gap: 12 }}>
                <span className="mono err" style={{ fontWeight: 600, width: 18 }}>✕</span>
                <span style={{ flex: '1 1 300px', fontSize: 14, lineHeight: 1.45, minWidth: 0 }}>
                  <b>{c.title}</b> <span className="muted2">The cut failed. {c.render_error}</span>
                </span>
                <button className="btn" onClick={() => act(`/candidates/${c.id}/render`, 'Cutting again')}>Cut again</button>
              </div>
            ))}
            {partial.map((v) => (
              <div key={`p${v.id}`} className="row" style={{ gap: 12 }}>
                <span className="mono warnc" style={{ fontWeight: 600, width: 18 }}>!</span>
                <span style={{ flex: '1 1 300px', fontSize: 14, lineHeight: 1.45, minWidth: 0 }}>
                  <b>{v.title}</b> <span className="muted2">Scored {v.moments - v.unscored} of {v.moments}. The AI service was busy; the rest can be scored later.</span>
                </span>
                <button className="btn" onClick={() => act(`/videos/${v.id}/rescore`, 'Scoring the rest…')}>Score the rest</button>
              </div>
            ))}
          </div>
        </div>
      )}

      <LinkBox onAdded={videos.reload} />

      {vids.length > 0 && (
        <div className="col" style={{ gap: 8 }}>
          <div className="row" style={{ justifyContent: 'space-between', alignItems: 'baseline' }}>
            <span className="eyebrow">Recent videos</span>
            <button className="btn link" style={{ fontWeight: 500, fontSize: 13 }} onClick={() => go('videos')}>All videos →</button>
          </div>
          <div className="card list" style={{ padding: '2px 16px' }}>
            {vids.slice(0, 6).map((v) => (
              <div key={v.id} className="row" style={{ gap: '8px 14px' }}>
                <StatusBadge status={v.status} />
                <span style={{ flex: '1 1 280px', minWidth: 0, fontSize: 14, lineHeight: 1.4 }}>
                  <b>{v.title}</b> <span className="muted">· {v.channel}{v.status === 'analyzed' ? ` · ${v.moments} moments` : ''}</span>
                </span>
                <span className={v.status === 'failed' ? 'err' : 'muted'} style={{ fontSize: 13 }}>
                  {v.status === 'failed' ? 'analysis failed' : v.status === 'processing' ? 'a few minutes left' : v.unscored > 0 && v.status === 'analyzed' ? `scored ${v.moments - v.unscored} of ${v.moments}` : ''}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
      {vids.length === 0 && <Empty>Nothing here yet.</Empty>}
    </section>
  )
}
