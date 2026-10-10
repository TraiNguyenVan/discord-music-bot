import { ListMusic, Music2, Pause, Play, SkipForward } from 'lucide-react'
import type { ControlAction, NowState } from '../types/music'
import { noEmoji } from '../lib/format'

interface BottomBarProps {
  now: NowState | null
  /** Local elapsed seconds, ticked between polls. */
  elapsed: number
  pending: ReadonlySet<string>
  onControl: (action: ControlAction) => void
  onOpenQueue: () => void
  onOpenPlayer: () => void
}

/**
 * Phone bottom bar: shown whenever something plays or tracks are queued.
 * Tapping the body returns to the full player; the queue button opens the
 * queue sheet.
 */
export function BottomBar({ now, elapsed, pending, onControl, onOpenQueue, onOpenPlayer }: BottomBarProps) {
  const playing = !!now?.playing
  const paused = !!(playing && now?.paused)
  const total = now?.duration ?? 0
  const pct = playing && total > 0 ? Math.min(1, elapsed / total) : playing ? 1 : 0
  const queueLen = now?.queue_len ?? 0

  const title = playing ? noEmoji(now?.title ?? '') || 'Untitled' : 'Nothing playing yet'
  const artist = playing ? noEmoji(now?.uploader ?? '') : queueLen ? `${queueLen} ${queueLen === 1 ? 'track' : 'tracks'} queued` : ''

  return (
    <div className="mbar" role="region" aria-label="Playback bar">
      <div className="mprog" aria-hidden="true">
        <div className="mprog-fill" style={{ transform: `scaleX(${pct.toFixed(4)})` }} />
      </div>
      <button type="button" className="mbody" aria-label="Open the full player" onClick={onOpenPlayer}>
        {now?.thumbnail ? (
          <img src={now.thumbnail} alt="" width={40} height={40} loading="lazy" decoding="async" />
        ) : (
          <span className="m-disc" aria-hidden="true">
            <Music2 size={18} />
          </span>
        )}
        <span className="m-txt">
          <span className="m-title">{title}</span>
          <span className="m-artist">{artist}</span>
        </span>
      </button>
      <button
        type="button"
        className="iconbtn"
        aria-label={playing && !paused ? 'Pause' : 'Play'}
        title={playing && !paused ? 'Pause' : 'Play'}
        disabled={pending.has('toggle') || !playing}
        onClick={() => onControl('toggle')}
      >
        {playing && !paused ? (
          <Pause size={16} fill="currentColor" aria-hidden="true" />
        ) : (
          <Play size={16} fill="currentColor" aria-hidden="true" />
        )}
      </button>
      <button
        type="button"
        className="iconbtn"
        aria-label="Skip to the next track"
        title="Skip"
        disabled={pending.has('skip') || (!playing && !queueLen)}
        onClick={() => onControl('skip')}
      >
        <SkipForward size={16} aria-hidden="true" />
      </button>
      <button type="button" className="mqueue" aria-label="Open the queue" onClick={onOpenQueue}>
        <ListMusic size={16} aria-hidden="true" />
        Queue · <span className="tnum">{queueLen}</span>
      </button>
    </div>
  )
}
