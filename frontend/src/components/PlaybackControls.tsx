import { Pause, Play, SkipForward, Square } from 'lucide-react'
import type { ControlAction, NowState } from '../types/music'

interface PlaybackControlsProps {
  now: NowState | null
  pending: ReadonlySet<string>
  onControl: (action: ControlAction) => void
}

/**
 * Transport group. Disabled logic mirrors the legacy picker: the toggle is
 * idle while nothing plays, skip stays usable while tracks are queued.
 * Pending is scoped to the tapped control.
 */
export function PlaybackControls({ now, pending, onControl }: PlaybackControlsProps) {
  const playing = !!now?.playing
  const paused = !!(playing && now?.paused)
  const toggleDisabled = pending.has('toggle') || !playing
  const skipDisabled = pending.has('skip') || (!playing && !(now && now.queue_len))

  return (
    <div className="tleft" role="group" aria-label="Transport">
      <button
        type="button"
        className="playbtn"
        aria-label={playing && !paused ? 'Pause' : 'Play'}
        title={playing && !paused ? 'Pause' : 'Play'}
        disabled={toggleDisabled}
        onClick={() => onControl('toggle')}
      >
        {playing && !paused ? (
          <Pause size={22} fill="currentColor" aria-hidden="true" />
        ) : (
          <Play size={22} fill="currentColor" aria-hidden="true" />
        )}
      </button>
      <button
        type="button"
        className="iconbtn"
        aria-label="Skip to the next track"
        title="Skip"
        disabled={skipDisabled}
        onClick={() => onControl('skip')}
      >
        <SkipForward size={18} aria-hidden="true" />
      </button>
      <button
        type="button"
        className="iconbtn"
        aria-label="Stop playback"
        title="Stop"
        disabled={pending.has('stop')}
        onClick={() => onControl('stop')}
      >
        <Square size={16} aria-hidden="true" />
      </button>
    </div>
  )
}
