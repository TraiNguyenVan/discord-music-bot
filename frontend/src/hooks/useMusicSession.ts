import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiClient } from '../lib/api-client'
import { ExpiredSessionError, errorMessage } from '../lib/errors'
import { cleanMsg } from '../lib/format'
import type { PickTrack, PlayResponse, QueuePage } from '../types/api'
import type { ControlAction, ControlParams, NowState } from '../types/music'

/** One line of inline feedback. `ok` is neutral/plain, `err` is red. */
export interface StatusUpdate {
  message: string
  kind: 'busy' | 'ok' | 'err'
}

interface PageState {
  sig: string
  data: QueuePage
}

const POLL_MS = 2000
const TICK_MS = 250

export interface MusicSession {
  /** Last good /api/now snapshot; kept on screen through transient failures. */
  now: NowState | null
  /** Local elapsed seconds, ticked 250ms while playing and not paused. */
  elapsed: number
  /** True when the last poll failed transiently (data may be old). */
  stale: boolean
  status: StatusUpdate | null
  setStatus: (update: StatusUpdate | null) => void
  /** Keys of in-flight controls (per action, or per row for remove/jump). */
  pending: ReadonlySet<string>
  runControl: (key: string, action: ControlAction, params?: ControlParams) => Promise<void>
  /** POST /api/pick ("add to queue"); resolves after the status + refresh land. */
  runPick: (key: string, track: PickTrack) => Promise<void>
  /** POST /api/play (quick add); returns the response, or null when rejected/failed. */
  runPlay: (key: string, query: string) => Promise<PlayResponse | null>
  page: number
  pageData: QueuePage | null
  gotoPage: (next: number) => void
}

/**
 * The centralized player/queue state layer (one hook, used by every
 * component — components never fetch). Owns:
 *
 * - `/api/now` polling: immediate, then every 2s, never overlapping, and a
 *   transient failure keeps the last good snapshot instead of wiping it.
 * - the 250ms local elapsed tick (advances only while playing, not paused).
 * - control mutations with scoped pending keys; a successful response applies
 *   the piggybacked `now`, reports the server message, resets pagination to
 *   page 1, and triggers a fresh poll.
 * - queue pagination: page 1 renders from the poll snapshot, pages > 1 fetch
 *   `/api/queue` and only swap in when the payload actually changed.
 *
 * HTTP 403 anywhere means the session expired and is escalated to `onExpired`.
 */
export function useMusicSession(client: ApiClient, onExpired: (message: string) => void): MusicSession {
  const [now, setNow] = useState<NowState | null>(null)
  const [elapsed, setElapsed] = useState(0)
  const [stale, setStale] = useState(false)
  const [status, setStatus] = useState<StatusUpdate | null>(null)
  const [pending, setPending] = useState<ReadonlySet<string>>(new Set())
  const [page, setPage] = useState(1)
  const [pageState, setPageState] = useState<PageState | null>(null)

  const nowRef = useRef<NowState | null>(null)
  const pollingRef = useRef(false)
  const pendingRef = useRef<Set<string>>(new Set())

  const applyNow = useCallback((next: NowState) => {
    // Defensive: the backend always sends `now`, but a malformed 200 must
    // never crash the poll loop or wipe the last good snapshot.
    if (!next || typeof next !== 'object') return
    nowRef.current = next
    setNow(next)
    setElapsed(next.elapsed ?? 0)
    setStale(false)
  }, [])

  const pollNow = useCallback(async () => {
    if (pollingRef.current) return
    pollingRef.current = true
    try {
      const res = await client.now()
      applyNow(res.now)
    } catch (error) {
      if (error instanceof ExpiredSessionError) {
        onExpired(errorMessage(error))
      } else {
        // Transient failure: keep the last good snapshot on screen.
        setStale(true)
      }
    } finally {
      pollingRef.current = false
    }
  }, [client, applyNow, onExpired])

  // Immediate first poll, then the legacy ~2s cadence, never overlapping.
  useEffect(() => {
    void pollNow()
    const id = window.setInterval(() => void pollNow(), POLL_MS)
    return () => window.clearInterval(id)
  }, [pollNow])

  // 250ms local elapsed tick between polls (no seek — the API has none).
  useEffect(() => {
    const id = window.setInterval(() => {
      const n = nowRef.current
      if (!n || !n.playing || n.paused) return
      const total = n.duration ?? 0
      const step = TICK_MS / 1000
      setElapsed((e) => (total > 0 ? Math.min(total, e + step) : e + step))
    }, TICK_MS)
    return () => window.clearInterval(id)
  }, [])

  const runControl = useCallback(
    async (key: string, action: ControlAction, params: ControlParams = {}) => {
      if (pendingRef.current.has(key)) return
      pendingRef.current.add(key)
      setPending(new Set(pendingRef.current))
      try {
        const res = await client.control(action, params)
        if (res.now) applyNow(res.now)
        setStatus({ message: cleanMsg(res.message) || 'Done.', kind: 'ok' })
        // Any mutation can reshuffle the queue: reset to the cheap page 1.
        setPage(1)
        void pollNow()
      } catch (error) {
        if (error instanceof ExpiredSessionError) {
          onExpired(errorMessage(error))
        } else {
          setStatus({ message: errorMessage(error), kind: 'err' })
        }
      } finally {
        pendingRef.current.delete(key)
        setPending(new Set(pendingRef.current))
      }
    },
    [client, applyNow, onExpired, pollNow],
  )

  /**
   * "Add to queue" via /api/pick. Same contract as runControl: scoped
   * pending key, busy → server message, queue reset + fresh poll after a
   * success, 403 escalates the whole session.
   */
  const runPick = useCallback(
    async (key: string, track: PickTrack): Promise<void> => {
      if (pendingRef.current.has(key)) return
      pendingRef.current.add(key)
      setPending(new Set(pendingRef.current))
      setStatus({ message: 'Sending to the bot…', kind: 'busy' })
      try {
        const res = await client.pick(track)
        setStatus({ message: cleanMsg(res.message) || 'Added to the queue.', kind: 'ok' })
        // The queue contents changed: reset to the cheap page 1 and refresh.
        setPage(1)
        void pollNow()
      } catch (error) {
        if (error instanceof ExpiredSessionError) {
          onExpired(errorMessage(error))
        } else {
          setStatus({ message: errorMessage(error), kind: 'err' })
        }
      } finally {
        pendingRef.current.delete(key)
        setPending(new Set(pendingRef.current))
      }
    },
    [client, onExpired, pollNow],
  )

  /**
   * Quick add via /api/play (the bot's smart router). The busy status and the
   * server's answer land in the shared status line; the response is returned
   * so the caller can render search results when kind === 'search'.
   */
  const runPlay = useCallback(
    async (key: string, query: string): Promise<PlayResponse | null> => {
      if (pendingRef.current.has(key)) return null
      pendingRef.current.add(key)
      setPending(new Set(pendingRef.current))
      setStatus({ message: 'Resolving on the bot…', kind: 'busy' })
      try {
        const res = await client.play(query)
        setStatus({ message: cleanMsg(res.message) || 'Done.', kind: 'ok' })
        setPage(1)
        void pollNow()
        return res
      } catch (error) {
        if (error instanceof ExpiredSessionError) {
          onExpired(errorMessage(error))
        } else {
          setStatus({ message: errorMessage(error), kind: 'err' })
        }
        return null
      } finally {
        pendingRef.current.delete(key)
        setPending(new Set(pendingRef.current))
      }
    },
    [client, onExpired, pollNow],
  )

  // Pages > 1 come from /api/queue and are refetched on every snapshot
  // update (matching the legacy cadence). A repeat payload never triggers
  // a re-render, so rows are not rebuilt under a finger mid-tap.
  useEffect(() => {
    if (page === 1) return
    let alive = true
    client
      .queue(page)
      .then((j) => {
        if (!alive) return
        const sig = `page:${j.page}:${j.total}:${JSON.stringify(j.items)}:${JSON.stringify(j.current)}`
        setPageState((prev) => (prev && prev.sig === sig ? prev : { sig, data: j }))
      })
      .catch((error) => {
        if (!alive) return
        if (error instanceof ExpiredSessionError) onExpired(errorMessage(error))
        // Transient failure: keep showing the current page data.
      })
    return () => {
      alive = false
    }
  }, [page, now, client, onExpired])

  const gotoPage = useCallback((next: number) => {
    setPage(Math.max(1, next))
  }, [])

  return {
    now,
    elapsed,
    stale,
    status,
    setStatus,
    pending,
    runControl,
    runPick,
    runPlay,
    page,
    pageData: pageState?.data ?? null,
    gotoPage,
  }
}
