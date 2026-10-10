import { useCallback, useMemo } from 'react'
import type { ReactNode } from 'react'
import { ChevronLeft, ChevronRight, X } from 'lucide-react'
import type { QueuePage } from '../types/api'
import type { ControlAction, ControlParams, NowState } from '../types/music'
import type { StatusUpdate } from '../hooks/useMusicSession'
import { useTwoTap } from '../hooks/useTwoTap'
import { fmtDur, noEmoji } from '../lib/format'
import { QueueRow, type QueueRowData } from './QueueRow'

interface QueuePanelProps {
  now: NowState | null
  page: number
  pageData: QueuePage | null
  pending: ReadonlySet<string>
  onControl: (key: string, action: ControlAction, params?: ControlParams) => void
  onPage: (next: number) => void
  onStatus: (update: StatusUpdate) => void
  /** Present when the panel doubles as the phone queue sheet. */
  onClose?: () => void
  /** Rendered above the queue — the "Add for the room" quick add (as in the
   *  legacy column, which becomes the phone sheet with quick add on top). */
  children?: ReactNode
}

interface QueueViewData {
  countLabel: string
  rows: QueueRowData[]
  currentTitle: string | null
  emptyText: string
  info: string
  prevDisabled: boolean
  nextDisabled: boolean
  moreNote: string | null
}

/** Cheap path: the /api/now snapshot carries the first 10 rows. */
function buildFromNow(now: NowState | null): QueueViewData {
  const q = now?.queue ?? []
  const total = now?.queue_len ?? 0
  const rows = q.map((t, i) => ({
    key: t.qid || `idx-${i}`,
    index: i + 1,
    title: noEmoji(t.title),
    sub: `${noEmoji(t.requester || '?')}${t.duration ? ` · ${fmtDur(t.duration)}` : ''}`,
    target: t.qid ? { qid: t.qid } : { index: i + 1 },
  }))
  return {
    countLabel: total ? `${total} ${total === 1 ? 'track' : 'tracks'}` : '',
    rows,
    currentTitle: null,
    emptyText: 'Nothing up next — add the first track.',
    info: total ? `page 1${total > 10 ? ` of about ${Math.ceil(total / 10)}` : ''} · ${total} tracks` : '',
    prevDisabled: true,
    nextDisabled: total <= 10,
    moreNote: total > q.length ? `+${total - q.length} more on later pages` : null,
  }
}

/** Full pages (with remove buttons + page numbers) come from /api/queue. */
function buildFromPage(j: QueuePage): QueueViewData {
  const rows = j.items.map((t) => ({
    key: t.qid || `idx-${t.index}`,
    index: t.index,
    title: noEmoji(t.title),
    sub: `${noEmoji(t.requester || '?')}${t.duration ? ` · ${fmtDur(t.duration)}` : ''}`,
    target: t.qid ? { qid: t.qid } : { index: t.index },
  }))
  const total = j.total
  return {
    countLabel: total ? `${total} ${total === 1 ? 'track' : 'tracks'}` : '',
    rows,
    currentTitle: j.current ? `Playing — ${noEmoji(j.current.title)}` : null,
    emptyText: j.items.length ? '' : j.current || total ? 'End of the queue.' : 'Nothing up next — add the first track.',
    info: `page ${j.page} of ${j.total_pages} · ${total} tracks`,
    prevDisabled: j.page <= 1,
    nextDisabled: j.page >= j.total_pages,
    moreNote: null,
  }
}

/** While a full page fetch is in flight and nothing is cached yet. */
const LOADING_VIEW: QueueViewData = {
  countLabel: '',
  rows: [],
  currentTitle: null,
  emptyText: 'Loading the queue…',
  info: '',
  prevDisabled: true,
  nextDisabled: true,
  moreNote: null,
}

/**
 * The queue. Page 1 renders from the /api/now snapshot, rebuilt only when
 * its signature changes — the 2s poll must not replace rows under a finger
 * mid-tap. Clear hides behind the legacy two-tap armed confirm.
 */
export function QueuePanel({
  now,
  page,
  pageData,
  pending,
  onControl,
  onPage,
  onStatus,
  onClose,
  children,
}: QueuePanelProps) {
  const onClearConfirm = useCallback(() => onControl('clear', 'clear'), [onControl])
  const onClearArm = useCallback(
    () => onStatus({ message: 'Press “Sure?” again within 5 seconds to clear the whole queue.', kind: 'ok' }),
    [onStatus],
  )
  const clear = useTwoTap(onClearConfirm, onClearArm)

  // Signature-diff: identical snapshot data never rebuilds the rows.
  const nowSig = `now:${now?.queue_len ?? 0}:${JSON.stringify(now?.queue ?? [])}`
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const nowView = useMemo(() => buildFromNow(now), [nowSig])
  const pageView = useMemo(() => (pageData ? buildFromPage(pageData) : null), [pageData])

  const view = page === 1 ? nowView : (pageView ?? LOADING_VIEW)

  return (
    <aside className="queuecol" aria-label="Queue">
      <div className="sheet-grip">
        {onClose && (
          <button type="button" className="iconbtn" aria-label="Close the queue" title="Close" onClick={onClose}>
            <X size={18} aria-hidden="true" />
          </button>
        )}
      </div>
      {children}
      <section className="queue" aria-label="Queue">
        <div className="qhead">
          <h2 className="h-sect">
            Up next
            {view.countLabel && <span className="qcount tnum">{view.countLabel}</span>}
          </h2>
          <div className="qtools">
            <button
              type="button"
              className={clear.armed ? 'pill pill--armed' : 'pill'}
              disabled={pending.has('clear')}
              onClick={clear.click}
            >
              {clear.armed ? 'Sure?' : 'Clear'}
            </button>
          </div>
        </div>

        <div className="qlist">
          {view.currentTitle && <p className="nowline">{view.currentTitle}</p>}
          {view.rows.length === 0 ? (
            <p className="empty">{view.emptyText}</p>
          ) : (
            view.rows.map((row) => <QueueRow key={row.key} row={row} pending={pending} onControl={onControl} />)
          )}
          {view.moreNote && <p className="morenote">{view.moreNote}</p>}
        </div>

        <div className="pager">
          <button type="button" className="pill" disabled={view.prevDisabled} onClick={() => onPage(page - 1)}>
            <ChevronLeft size={15} aria-hidden="true" /> Prev
          </button>
          <span className="qinfo tnum">{view.info}</span>
          <button type="button" className="pill" disabled={view.nextDisabled} onClick={() => onPage(page + 1)}>
            Next <ChevronRight size={15} aria-hidden="true" />
          </button>
        </div>

        <div className="qmore">
          <button
            type="button"
            className="pill"
            disabled={pending.has('mix_more')}
            onClick={() => onControl('mix_more', 'mix_more')}
          >
            Mix +25
          </button>
          <button
            type="button"
            className="pill"
            disabled={pending.has('playlist_more')}
            onClick={() => onControl('playlist_more', 'playlist_more')}
          >
            Playlist +25
          </button>
        </div>
      </section>
    </aside>
  )
}
