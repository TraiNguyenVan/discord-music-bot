import { Music2, Pause, Play, SkipForward } from 'lucide-react'
import type { ControlAction, NowState } from '../types/music'
import { noEmoji } from '../lib/format'

interface MiniPlayerProps {
  now: NowState | null
  pending: ReadonlySet<string>
  onControl: (action: ControlAction) => void
}

/** Topbar mini player — rendered only while the hero deck is scrolled away. */
export function MiniPlayer({ now, pending, onControl }: MiniPlayerProps) {
  const playing = !!now?.playing
  const paused = !!(playing && now?.paused)
  const title = now?.playing ? noEmoji(now.title) || 'Untitled' : 'Nothing playing yet'

  return (
    <div className="mini" role="group" aria-label="Mini player">
      {now?.thumbnail ? (
        <img src={now.thumbnail} alt="" width={40} height={40} loading="lazy" decoding="async" />
      ) : (
        <span className="mini-disc" aria-hidden="true">
          <Music2 size={18} />
        </span>
      )}
      <span className="mini-text">{title}</span>
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
        disabled={pending.has('skip') || (!playing && !(now && now.queue_len))}
        onClick={() => onControl('skip')}
      >
        <SkipForward size={16} aria-hidden="true" />
      </button>
    </div>
  )
}
