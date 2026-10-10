import { useCallback, useEffect, useRef, useState } from 'react'
import type { ApiClient } from '../lib/api-client'
import { ExpiredSessionError, errorMessage } from '../lib/errors'
import { backendsFromConfig, DEFAULT_SEARCH_BACKENDS, raceRelated } from '../lib/search'
import type { SearchResult } from '../types/api'
import type { ControlAction } from '../types/music'
import type { SessionInfo } from '../types/api'
import { useMusicSession } from '../hooks/useMusicSession'
import { useAudition } from '../hooks/useAudition'
import { useMediaQuery } from '../hooks/useMediaQuery'
import { Sidebar } from './Sidebar'
import { TopBar } from './TopBar'
import { NowPlaying } from './NowPlaying'
import { StopButton } from './PlaybackControls'
import { ModeControls } from './ModeControls'
import { VolumeControl } from './VolumeControl'
import { FindMusic } from './FindMusic'
import { QuickAdd } from './QuickAdd'
import { QueuePanel } from './QueuePanel'
import { StatusLine } from './StatusLine'
import { PlayerBar } from './PlayerBar'
import { ScrollTop } from './ScrollTop'

interface PlayerViewProps {
  client: ApiClient
  session: SessionInfo
  onExpired: (message: string) => void
}

/**
 * The Spotify-style shell: fixed sidebar, sticky topbar, a scrollable main
 * column (now-playing header, search, queue), and the always-on player bar.
 * Owns no data itself: all polling, controls, status, and pagination come
 * from `useMusicSession`; components render from that state and post through
 * the same runner.
 */
export function PlayerView({ client, session, onExpired }: PlayerViewProps) {
  const ms = useMusicSession(client, onExpired)
  const audition = useAudition()
  const deckRef = useRef<HTMLElement | null>(null)
  const [sheetOpen, setSheetOpen] = useState(false)
  const [backends, setBackends] = useState(DEFAULT_SEARCH_BACKENDS)
  const [related, setRelated] = useState<SearchResult[] | null>(null)
  const relatedSeq = useRef(0)
  const desktop = useMediaQuery('(min-width: 1000px)')

  // Phone queue sheet: body class drives the slide-up + scroll lock.
  useEffect(() => {
    document.body.classList.toggle('qsheet', sheetOpen)
    if (!sheetOpen) return
    return () => document.body.classList.remove('qsheet')
  }, [sheetOpen])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setSheetOpen(false)
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [])

  const runSimple = useCallback(
    (action: ControlAction) => void ms.runControl(action, action),
    [ms.runControl],
  )
  const handleVolumeSet = useCallback(
    (level: number) => ms.runControl('volume_set', 'volume_set', { level }),
    [ms.runControl],
  )
  const handleVolumeDelta = useCallback(
    (delta: number) => ms.runControl('volume_delta', 'volume_delta', { delta }),
    [ms.runControl],
  )

  const scrollTo = useCallback((id: string) => {
    const el = document.getElementById(id)
    if (!el) return
    const reduced = typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches
    el.scrollIntoView({ behavior: reduced ? 'auto' : 'smooth', block: 'start' })
  }, [])

  const openPlayer = useCallback(() => {
    setSheetOpen(false)
    scrollTo('main')
  }, [scrollTo])

  // Phones: the queue is a bottom sheet. Wide screens: scroll to the panel.
  const openQueue = useCallback(() => {
    if (desktop) scrollTo('queue')
    else setSheetOpen(true)
  }, [desktop, scrollTo])
  const closeQueue = useCallback(() => setSheetOpen(false), [])

  // Search backends come from the bot's config; the defaults cover an outage.
  useEffect(() => {
    let alive = true
    client
      .config()
      .then((j) => {
        if (!alive) return
        setBackends(backendsFromConfig(j))
      })
      .catch(() => {})
    return () => {
      alive = false
    }
  }, [client])

  // Related tracks are seeded by the audition; the latest preview wins.
  const loadRelated = useCallback(
    async (videoId: string) => {
      const seq = ++relatedSeq.current
      setRelated(null)
      let rel: SearchResult[] = []
      try {
        rel = await raceRelated(backends, videoId)
      } catch {
        rel = []
      }
      if (seq !== relatedSeq.current) return
      if (!rel.length) {
        try {
          const res = await client.related(videoId)
          rel = res.results ?? []
        } catch (error) {
          if (error instanceof ExpiredSessionError) {
            onExpired(errorMessage(error))
            return
          }
          // Related is best-effort — the section just stays hidden.
        }
      }
      if (seq !== relatedSeq.current) return
      setRelated(rel.length ? rel.slice(0, 10) : null)
    },
    [backends, client, onExpired],
  )

  // Previewing = start the muted embed AND re-seed "More like this".
  const preview = useCallback(
    (meta: SearchResult) => {
      audition.preview(meta)
      void loadRelated(meta.videoId)
    },
    [audition.preview, loadRelated],
  )

  const addTrack = useCallback(
    (track: SearchResult) => {
      void ms.runPick(`pick:${track.videoId}`, track)
    },
    [ms.runPick],
  )

  return (
    <div className="shell">
      <a className="skip-link" href="#main">
        Skip to the player
      </a>
      <Sidebar now={ms.now} />
      <div className="frame">
        <TopBar
          session={session}
          now={ms.now}
          pending={ms.pending}
          onControl={ms.runControl}
          onStatus={ms.setStatus}
        />
        <main className="app-main" id="main">
          <NowPlaying now={ms.now} elapsed={ms.elapsed} stale={ms.stale} ref={deckRef}>
            {!desktop && (
              <div className="hero-tools">
                <ModeControls now={ms.now} pending={ms.pending} onControl={runSimple} />
                <StopButton pending={ms.pending} onControl={runSimple} />
                <VolumeControl
                  volume={ms.now?.volume ?? 50}
                  pending={ms.pending}
                  onSet={handleVolumeSet}
                  onDelta={handleVolumeDelta}
                />
              </div>
            )}
          </NowPlaying>

          <div className="cols">
            <FindMusic
              client={client}
              backends={backends}
              audition={audition}
              onPreview={preview}
              onAdd={addTrack}
              related={related}
              pending={ms.pending}
              onStatus={ms.setStatus}
              onExpired={onExpired}
            />
            <QueuePanel
              now={ms.now}
              page={ms.page}
              pageData={ms.pageData}
              pending={ms.pending}
              onControl={ms.runControl}
              onPage={ms.gotoPage}
              onStatus={ms.setStatus}
              onClose={closeQueue}
            >
              <QuickAdd onPlay={ms.runPlay} onPreview={preview} onAdd={addTrack} pending={ms.pending} />
            </QueuePanel>
          </div>
        </main>
      </div>

      <PlayerBar
        now={ms.now}
        elapsed={ms.elapsed}
        pending={ms.pending}
        onControl={runSimple}
        onOpenQueue={openQueue}
        onOpenPlayer={openPlayer}
        desktop={desktop}
        volume={ms.now?.volume ?? 50}
        onVolumeSet={handleVolumeSet}
        onVolumeDelta={handleVolumeDelta}
      />

      {ms.status && (
        <div className="toast">
          <StatusLine status={ms.status} />
        </div>
      )}
      {sheetOpen && <div className="qscrim" aria-hidden="true" onClick={closeQueue} />}
      <ScrollTop />
    </div>
  )
}
