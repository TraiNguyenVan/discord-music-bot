import { LoaderCircle, Pause, Play, SkipForward, Square } from 'lucide-react'
import type { ControlAction, NowState } from '../types/music'

interface PlaybackControlsProps {
  now: NowState | null
  pending: ReadonlySet<string>
  onControl: (action: ControlAction) => void
  /** The compact bar omits Stop (it lives in the phone hero tools). */
  showStop?: boolean
}

/**
 * Transport group. Disabled logic mirrors the legacy picker: the toggle is
 * idle while nothing plays, skip stays usable while tracks are queued.
 * Pending is scoped to the tapped control.
 */
export function PlaybackControls({ now, pending, onControl, showStop = true }: PlaybackControlsProps) {
  const playing = !!now?.playing
  const paused = !!(playing && now?.paused)
  const toggleDisabled = pending.has('toggle') || !playing
  const skipDisabled = pending.has('skip') || (!playing && !(now && now.queue_len))

  return (
    <>
      <button
        type="button"
        className="playbtn"
        aria-label={playing && !paused ? 'Pause' : 'Play'}
        title={playing && !paused ? 'Pause' : 'Play'}
        disabled={toggleDisabled}
        onClick={() => onControl('toggle')}
      >
        {playing && !paused ? (
          <Pause size={18} fill="currentColor" aria-hidden="true" />
        ) : (
          <Play size={18} fill="currentColor" aria-hidden="true" />
        )}
      </button>
      <button
        type="button"
        className="ghostbtn"
        aria-label="Skip to the next track"
        title="Skip"
        disabled={skipDisabled}
        onClick={() => onControl('skip')}
      >
        <SkipForward size={18} aria-hidden="true" />
      </button>
      {showStop && <StopButton pending={pending} onControl={onControl} />}
    </>
  )
}

interface StopButtonProps {
  pending: ReadonlySet<string>
  onControl: (action: ControlAction) => void
}

/** Stop playback — shared by the desktop transport and the phone hero tools. */
export function StopButton({ pending, onControl }: StopButtonProps) {
  return (
    <button
      type="button"
      className="ghostbtn"
      aria-label="Stop playback"
      title="Stop"
      disabled={pending.has('stop')}
      onClick={() => onControl('stop')}
    >
      {pending.has('stop') ? (
        <LoaderCircle size={16} className="spin" aria-hidden="true" />
      ) : (
        <Square size={14} aria-hidden="true" />
      )}
    </button>
  )
}
