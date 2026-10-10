import { ListMusic, Music2 } from 'lucide-react'
import type { ControlAction, NowState } from '../types/music'
import { noEmoji } from '../lib/format'
import { ModeControls } from './ModeControls'
import { PlaybackControls } from './PlaybackControls'
import { VolumeControl } from './VolumeControl'

interface PlayerBarProps {
  now: NowState | null
  /** Local elapsed seconds, ticked between polls. */
  elapsed: number
  pending: ReadonlySet<string>
  onControl: (action: ControlAction) => void
  onOpenQueue: () => void
  onOpenPlayer: () => void
  /** Wide layout: modes + volume live here; phones keep them in the hero. */
  desktop: boolean
  /** 0–200, backend-confirmed. */
  volume: number
  onVolumeSet: (level: number) => Promise<void>
  onVolumeDelta: (delta: number) => void
}

/**
 * The always-on player bar. Desktop is Spotify's three-zone layout — track
 * info, centered transport over a progress line, queue + volume — while
 * phones get the compact bar with a hairline progress on top.
 */
export function PlayerBar({
  now,
  elapsed,
  pending,
  onControl,
  onOpenQueue,
  onOpenPlayer,
  desktop,
  volume,
  onVolumeSet,
  onVolumeDelta,
}: PlayerBarProps) {
  const playing = !!now?.playing
  const total = now?.duration ?? 0
  const pct = playing && total > 0 ? Math.min(1, elapsed / total) : playing ? 1 : 0
  const queueLen = now?.queue_len ?? 0

  const title = playing ? noEmoji(now?.title ?? '') || 'Untitled' : 'Nothing playing yet'
  const artist = playing ? noEmoji(now?.uploader ?? '') : queueLen ? `${queueLen} ${queueLen === 1 ? 'track' : 'tracks'} queued` : ''

  const trackButton = (
    <button type="button" className="pbar-track" aria-label="Open the full player" onClick={onOpenPlayer}>
      {now?.thumbnail ? (
        <img src={now.thumbnail} alt="" width={56} height={56} loading="lazy" decoding="async" />
      ) : (
        <span className="pbar-disc" aria-hidden="true">
          <Music2 size={20} />
        </span>
      )}
      <span className="pbar-txt">
        <span className="pbar-title">{title}</span>
        <span className="pbar-artist">{artist}</span>
      </span>
    </button>
  )

  const queueButton = (
    <button
      type="button"
      className={desktop ? 'ghostbtn' : 'pbar-queue'}
      aria-label={`Open the queue (${queueLen} ${queueLen === 1 ? 'track' : 'tracks'} queued)`}
      title="Queue"
      onClick={onOpenQueue}
    >
      <ListMusic size={desktop ? 16 : 18} aria-hidden="true" />
      {!desktop && (
        <span className="pbar-queue-count tnum" aria-hidden="true">
          {queueLen}
        </span>
      )}
    </button>
  )

  if (!desktop) {
    return (
      <div className="pbar" role="region" aria-label="Player">
        <div className="pbar-hairline" aria-hidden="true">
          <div style={{ transform: `scaleX(${pct.toFixed(4)})` }} />
        </div>
        {trackButton}
        <div className="pbar-mini-ctr" role="group" aria-label="Transport">
          <PlaybackControls now={now} pending={pending} onControl={onControl} showStop={false} />
        </div>
        {queueButton}
      </div>
    )
  }

  return (
    <div className="pbar" role="region" aria-label="Player">
      {trackButton}

      <div className="pbar-center">
        <div className="pbar-ctr">
          <ModeControls now={now} pending={pending} onControl={onControl} />
          <div className="pbar-transport" role="group" aria-label="Transport">
            <PlaybackControls now={now} pending={pending} onControl={onControl} />
          </div>
        </div>
        <div
          className="pbar-line"
          role="progressbar"
          aria-label="Playback progress"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={Math.round(pct * 100)}
        >
          <div className="pbar-line-fill" style={{ transform: `scaleX(${pct.toFixed(4)})` }} />
        </div>
      </div>

      <div className="pbar-right">
        {queueButton}
        <VolumeControl volume={volume} pending={pending} onSet={onVolumeSet} onDelta={onVolumeDelta} />
      </div>
    </div>
  )
}
