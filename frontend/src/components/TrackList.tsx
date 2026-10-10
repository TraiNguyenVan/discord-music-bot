import { LoaderCircle, Play, Plus } from 'lucide-react'
import { fmtDur, noEmoji } from '../lib/format'
import type { SearchResult } from '../types/api'

interface TrackListProps {
  results: SearchResult[]
  /** Shown when the list is empty; blank for lists that hide instead. */
  emptyText: string
  onAudition: (track: SearchResult) => void
  onAdd: (track: SearchResult) => void
  pending: ReadonlySet<string>
}

/**
 * Track rows shared by search results, related tracks, and quick-add
 * results: the row auditions (muted preview), the side button adds for the
 * room. Pending add shows as a spinner scoped to the tapped button.
 */
export function TrackList({ results, emptyText, onAudition, onAdd, pending }: TrackListProps) {
  if (!results.length) {
    return emptyText ? <p className="empty">{emptyText}</p> : null
  }
  return (
    <>
      {results.map((m) => {
        const clean = noEmoji(m.title)
        const adding = pending.has(`pick:${m.videoId}`)
        return (
          <div className="rowwrap" key={m.videoId}>
            <button
              type="button"
              className="row"
              aria-label={`Audition ${clean}`}
              onClick={() => onAudition(m)}
            >
              <span className="rthumb">
                <img src={m.thumbnail} alt="" width={64} height={64} loading="lazy" decoding="async" />
                <span className="playov" aria-hidden="true">
                  <span>
                    <Play size={16} fill="currentColor" aria-hidden="true" />
                  </span>
                </span>
              </span>
              <span className="rtext">
                <span className="rtitle">{clean}</span>
                <span className="rsub">{noEmoji(m.uploader)}</span>
              </span>
              <span className="rdur tnum" aria-hidden="true">
                {m.duration > 0 ? fmtDur(m.duration) : ''}
              </span>
            </button>
            <button
              type="button"
              className="radd"
              aria-label={`Add ${clean} to the queue`}
              disabled={adding}
              onClick={() => onAdd(m)}
            >
              {adding ? (
                <LoaderCircle size={16} className="spin" aria-hidden="true" />
              ) : (
                <Plus size={18} aria-hidden="true" />
              )}
            </button>
          </div>
        )
      })}
    </>
  )
}
