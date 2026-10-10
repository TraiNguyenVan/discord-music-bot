import type { ControlAction, NowState } from '../types/music'

interface ModeControlsProps {
  now: NowState | null
  pending: ReadonlySet<string>
  onControl: (action: ControlAction) => void
}

/** Shuffle / loop / autoplay. Labels and aria-pressed mirror the backend snapshot. */
export function ModeControls({ now, pending, onControl }: ModeControlsProps) {
  const loop = now?.loop ?? 'off'
  // The backend defaults autoplay to on when absent.
  const autoplay = typeof now?.autoplay !== 'undefined' ? now.autoplay : true

  return (
    <div className="modes" role="group" aria-label="Playback modes">
      <button
        type="button"
        className="mode"
        disabled={pending.has('shuffle')}
        onClick={() => onControl('shuffle')}
      >
        Shuffle
      </button>
      <button
        type="button"
        className="mode"
        aria-pressed={loop !== 'off'}
        disabled={pending.has('loop')}
        onClick={() => onControl('loop')}
      >
        Loop {loop}
      </button>
      <button
        type="button"
        className="mode"
        aria-pressed={autoplay === true}
        disabled={pending.has('autoplay')}
        onClick={() => onControl('autoplay')}
      >
        Autoplay {autoplay ? 'on' : 'off'}
      </button>
    </div>
  )
}
