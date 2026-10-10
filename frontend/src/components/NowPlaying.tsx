import type { ReactNode, Ref } from 'react'
import type { NowState } from '../types/music'
import { fmtDur, noEmoji } from '../lib/format'

interface NowPlayingProps {
  now: NowState | null
  /** Local elapsed seconds, ticked between polls. */
  elapsed: number
  /** True when the last poll failed transiently. */
  stale: boolean
  /** Transport / modes / volume / feedback render inside the hero bottom. */
  children?: ReactNode
  ref?: Ref<HTMLElement>
}

function extraBits(now: NowState | null, stale: boolean): string {
  if (!now) return stale ? 'reconnecting…' : 'waiting for the bot…'
  const bits: string[] = []
  if (now.voice_channel) bits.push(`in ${noEmoji(now.voice_channel)}`)
  if (now.playing) {
    if (now.requester) bits.push(`queued by ${noEmoji(now.requester)}`)
  } else {
    bits.push(now.queue_len ? `${now.queue_len} ${now.queue_len === 1 ? 'track' : 'tracks'} waiting` : 'queue is empty')
  }
  if (stale) bits.push('reconnecting…')
  return bits.join(' · ')
}

/** Who's listening: humans in the bot's voice channel, per the bot snapshot. */
function Listeners({ now }: { now: NowState }) {
  const people = (now.listeners ?? []).filter((p) => p && p.name)
  if (!people.length) return null
  const avatars = people.slice(0, 5).filter((p) => p.avatar)
  const names = people.slice(0, 3).map((p) => noEmoji(p.name))
  const rest = people.length - names.length
  return (
    <div className="listeners">
      <span className="avstack" aria-hidden="true">
        {avatars.map((p) => (
          <img key={p.id} src={p.avatar} alt="" width={22} height={22} loading="lazy" decoding="async" />
        ))}
      </span>
      <span className="lnames">{rest > 0 ? `${names.join(', ')}, +${rest} more` : names.join(', ')}</span>
    </div>
  )
}

/**
 * The now-playing deck: state line, title, art, listeners, and the progress
 * bar with real elapsed ticking between polls. `duration === 0` means LIVE.
 * The connected/playing facts are backend-confirmed only — never invented.
 */
export function NowPlaying({ now, elapsed, stale, children, ref }: NowPlayingProps) {
  const playing = !!now?.playing
  const paused = !!(playing && now?.paused)
  const live = playing && (now?.duration ?? 0) === 0
  const total = now?.duration ?? 0
  const pct = playing && total > 0 ? Math.min(1, elapsed / total) : playing ? 1 : 0

  const stateText = !now ? 'Standby' : paused ? 'Paused' : playing ? 'Now playing' : 'Standby'
  const title = playing ? noEmoji(now?.title ?? '') : 'Nothing playing yet'
  const sub = playing ? noEmoji(now?.uploader ?? '') : 'Search below, preview muted, and add — the bot starts it.'
  const thumbnail = playing ? now?.thumbnail : undefined
  const deckClass = 'hero deck' + (playing ? ' playing' : '') + (paused ? ' paused' : '')

  return (
    <section className={deckClass} aria-label="Now playing" ref={ref}>
      <div className="hero-art" aria-hidden="true">
        {thumbnail ? (
          <img src={thumbnail} alt="" width={320} height={320} loading="lazy" decoding="async" />
        ) : (
          <svg className="disc" viewBox="0 0 24 24" fill="none">
            <path
              d="M9 18.5V6.2l10-2.3v10.6"
              stroke="currentColor"
              strokeWidth="1.6"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
            <circle cx="6.5" cy="18.5" r="2.6" fill="currentColor" />
            <circle cx="16.5" cy="14.5" r="2.6" fill="currentColor" />
          </svg>
        )}
      </div>

      <div className="hero-main">
        <p className="np-state">
          <span className="eq" aria-hidden="true">
            <span />
            <span />
            <span />
          </span>
          <span>{stateText}</span>
          <span className="np-sep" aria-hidden="true">
            —
          </span>
          <span>{extraBits(now, stale)}</span>
        </p>
        <h2 className="np-title">{title}</h2>
        <p className="np-sub">{sub}</p>
        {now && <Listeners now={now} />}
      </div>

      <div className="hero-bottom">
        <div
          className="np-track"
          role="progressbar"
          aria-label="Playback progress"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={Math.round(pct * 100)}
        >
          <div className="np-fill" style={{ transform: `scaleX(${pct.toFixed(4)})` }} />
        </div>
        <div className="np-times tnum">
          <span>{playing ? fmtDur(elapsed) : '–:––'}</span>
          {paused && <span className="chip">Paused</span>}
          <span>{!playing ? '–:––' : live ? 'LIVE' : fmtDur(total)}</span>
        </div>
        {children}
      </div>
    </section>
  )
}
