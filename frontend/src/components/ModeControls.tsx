import { Infinity as InfinityIcon, Repeat, Repeat1, Shuffle } from 'lucide-react'
import type { ControlAction, NowState } from '../types/music'

interface ModeControlsProps {
  now: NowState | null
  pending: ReadonlySet<string>
  onControl: (action: ControlAction) => void
}

/**
 * Shuffle / loop / autoplay as Spotify-style icon toggles: ghost icons that
 * turn green with a dot when a mode is on. Labels live in aria-label +
 * title; the loop icon tracks the mode (Repeat1 = track loop).
 */
export function ModeControls({ now, pending, onControl }: ModeControlsProps) {
  const loop = now?.loop ?? 'off'
  // The backend defaults autoplay to on when absent.
  const autoplay = typeof now?.autoplay !== 'undefined' ? now.autoplay : true

  return (
    <div className="modes" role="group" aria-label="Playback modes">
      <button
        type="button"
        className="mode"
        aria-label="Shuffle the queue"
        title="Shuffle"
        disabled={pending.has('shuffle')}
        onClick={() => onControl('shuffle')}
      >
        <Shuffle size={16} aria-hidden="true" />
      </button>
      <button
        type="button"
        className="mode"
        aria-pressed={loop !== 'off'}
        aria-label={`Loop mode: ${loop}`}
        title={`Loop: ${loop}`}
        disabled={pending.has('loop')}
        onClick={() => onControl('loop')}
      >
        {loop === 'track' ? <Repeat1 size={16} aria-hidden="true" /> : <Repeat size={16} aria-hidden="true" />}
        <span className="mode-dot" aria-hidden="true" />
      </button>
      <button
        type="button"
        className="mode"
        aria-pressed={autoplay === true}
        aria-label={`Autoplay ${autoplay ? 'on' : 'off'}`}
        title={`Autoplay: ${autoplay ? 'on' : 'off'}`}
        disabled={pending.has('autoplay')}
        onClick={() => onControl('autoplay')}
      >
        <InfinityIcon size={17} aria-hidden="true" />
        <span className="mode-dot" aria-hidden="true" />
      </button>
    </div>
  )
}
