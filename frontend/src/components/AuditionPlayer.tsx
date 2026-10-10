import type { RefObject } from 'react'
import { LoaderCircle } from 'lucide-react'
import { fmtDur, noEmoji } from '../lib/format'
import type { SearchResult } from '../types/api'

interface AuditionPlayerProps {
  selected: SearchResult | null
  muted: boolean
  slotOn: boolean
  mountRef: RefObject<HTMLDivElement | null>
  onToggleMute: () => void
  onQueue: () => void
  queuePending: boolean
}

/**
 * The preview player: only you hear it, so it never fights the room. Starts
 * muted via the IFrame API; the mute toggle is local-only. "Add to queue"
 * stays disabled until a track is actually previewing.
 */
export function AuditionPlayer({
  selected,
  muted,
  slotOn,
  mountRef,
  onToggleMute,
  onQueue,
  queuePending,
}: AuditionPlayerProps) {
  const clean = selected ? noEmoji(selected.title) : null
  return (
    <section className="aud" aria-label="Preview player">
      <div className="aud-head">
        <h3 className="h-sub">
          Preview on this device <span className="aud-note">(only you hear it)</span>
        </h3>
        <button
          type="button"
          className="pill"
          aria-pressed={!muted}
          aria-label={muted ? 'Unmute preview' : 'Mute preview'}
          onClick={onToggleMute}
        >
          {muted ? 'Muted' : 'Sound on'}
        </button>
      </div>
      <div className={slotOn ? 'aud-slot on' : 'aud-slot'}>
        <div ref={mountRef} />
        <p className="aud-hint">
          Tap a match to preview it here. Previews start muted and play only on this device, so they never fight the
          room.
        </p>
      </div>
      <div className="aud-meta">
        <div className="aud-text">
          <p className="aud-title">{clean ?? 'Nothing previewing yet'}</p>
          <p className="aud-sub">
            {selected
              ? `${noEmoji(selected.uploader)}${selected.duration ? ` · ${fmtDur(selected.duration)}` : ''}`
              : ''}
          </p>
        </div>
        <button
          type="button"
          className="cta"
          disabled={!selected || queuePending}
          aria-label={clean ? `Add ${clean} to the queue` : undefined}
          onClick={onQueue}
        >
          {queuePending && <LoaderCircle size={13} className="spin" aria-hidden="true" />} Add to queue
        </button>
      </div>
    </section>
  )
}
