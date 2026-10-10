import { LoaderCircle, Play, X } from 'lucide-react'
import type { ControlAction, ControlParams } from '../types/music'

export interface QueueRowData {
  /** Stable React key: the qid when present, else the position. */
  key: string
  index: number
  title: string
  sub: string
  /** remove/jump target the stable qid; only a qid-less row falls back to index. */
  target: ControlParams
}

interface QueueRowProps {
  row: QueueRowData
  pending: ReadonlySet<string>
  onControl: (key: string, action: ControlAction, params?: ControlParams) => void
}

/**
 * One queue row. Actions target the stable track id: in a shared queue an
 * index can shift between render and tap — the id cannot. Pending shows as a
 * spinner scoped to the tapped button.
 */
export function QueueRow({ row, pending, onControl }: QueueRowProps) {
  const jumpPending = pending.has(`jump:${row.key}`)
  const removePending = pending.has(`remove:${row.key}`)

  return (
    <div className="qrow" data-qid={row.target.qid}>
      <span className="qnum" aria-hidden="true">
        {row.index}
      </span>
      <div className="rtext">
        <span className="rtitle">{row.title}</span>
        <span className="rsub">{row.sub}</span>
      </div>
      <button
        type="button"
        className="qplay"
        aria-label={`Play ${row.title} now`}
        title="Play now"
        disabled={jumpPending}
        onClick={() => onControl(`jump:${row.key}`, 'jump', row.target)}
      >
        {jumpPending ? (
          <LoaderCircle size={16} className="spin" aria-hidden="true" />
        ) : (
          <Play size={14} fill="currentColor" aria-hidden="true" />
        )}
      </button>
      <button
        type="button"
        className="rm"
        aria-label={`Remove ${row.title} from the queue`}
        title="Remove from queue"
        disabled={removePending}
        onClick={() => onControl(`remove:${row.key}`, 'remove', row.target)}
      >
        {removePending ? (
          <LoaderCircle size={16} className="spin" aria-hidden="true" />
        ) : (
          <X size={16} aria-hidden="true" />
        )}
      </button>
    </div>
  )
}
