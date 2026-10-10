import { useCallback, useEffect, useRef, useState } from 'react'
import type { RefObject } from 'react'
import { ensureYTApi } from '../lib/yt'
import type { YTPlayer, YTNamespace } from '../lib/yt'
import type { SearchResult } from '../types/api'

export interface AuditionController {
  /** What "Add to queue" sends; also drives the preview meta and button. */
  selected: SearchResult | null
  /** True while the preview plays silently on this device. */
  muted: boolean
  /** True once the embed exists — the hint gives way to the video. */
  slotOn: boolean
  /** Mount point: the player iframe is created inside this node. */
  mountRef: RefObject<HTMLDivElement | null>
  preview: (meta: SearchResult) => void
  toggleMute: () => void
}

const PREVIEW_VOLUME = 80

/**
 * The muted audition player (YouTube IFrame API). Previews never fight the
 * bot's audio: they always start muted (`mute: 1` + `playsinline: 1`, forced
 * again after every load), and the mute toggle is local-only state.
 *
 * Taps inside the embed itself (e.g. an end-screen) are adopted: a 3s sync
 * adopts the video the user switched to, so "Add to queue" follows what is
 * actually shown.
 */
export function useAudition(): AuditionController {
  const [selected, setSelected] = useState<SearchResult | null>(null)
  const [muted, setMuted] = useState(true)
  const [slotOn, setSlotOn] = useState(false)
  const mountRef = useRef<HTMLDivElement | null>(null)

  const playerRef = useRef<YTPlayer | null>(null)
  const readyRef = useRef(false)
  const pendingIdRef = useRef<string | null>(null)
  const mutedRef = useRef(true)
  const selectedRef = useRef<SearchResult | null>(null)

  const applyMute = useCallback(() => {
    const p = playerRef.current
    if (!p || !readyRef.current) return
    try {
      if (mutedRef.current) {
        p.mute()
        p.setVolume(0)
      } else {
        p.unMute()
        p.setVolume(PREVIEW_VOLUME)
      }
    } catch {
      /* the embed can be gone mid-call; the mute state stays ours */
    }
  }, [])

  function syncFromPlayer(): void {
    // The user tapped something inside the embed itself (e.g. an end-screen)
    // — adopt it so "Add to queue" follows what is actually shown.
    const p = playerRef.current
    if (!p || !readyRef.current) return
    try {
      const data = p.getVideoData?.()
      if (!data?.video_id) return
      const sel = selectedRef.current
      if (sel && data.video_id === sel.videoId) return
      let duration = 0
      try {
        duration = Math.round(p.getDuration?.() || 0)
      } catch {
        duration = 0
      }
      const adopted: SearchResult = {
        videoId: data.video_id,
        title: data.title || `YouTube video ${data.video_id}`,
        uploader: data.author || '?',
        duration,
        thumbnail: `https://i.ytimg.com/vi/${data.video_id}/hqdefault.jpg`,
      }
      selectedRef.current = adopted
      setSelected(adopted)
    } catch {
      /* best effort only */
    }
  }

  const ensurePlayer = useCallback(async () => {
    if (playerRef.current) return
    let YT: YTNamespace
    try {
      YT = await ensureYTApi()
    } catch {
      return // API unavailable — previews stay off; adding to the queue still works
    }
    if (playerRef.current || !mountRef.current) return
    // The player replaces this inner node with its iframe; React never
    // manages it, so reconciliation can never clobber the embed.
    const inner = document.createElement('div')
    mountRef.current.appendChild(inner)
    setSlotOn(true)
    try {
      playerRef.current = new YT.Player(inner, {
        height: '100%',
        width: '100%',
        playerVars: { rel: 0, mute: 1, playsinline: 1 },
        events: {
          onReady: (event) => {
            readyRef.current = true
            try {
              event.target.mute()
              event.target.setVolume(0)
            } catch {
              /* applyMute re-asserts our state */
            }
            applyMute()
            if (pendingIdRef.current) {
              const id = pendingIdRef.current
              pendingIdRef.current = null
              try {
                event.target.loadVideoById(id)
              } catch {
                /* the user can preview again */
              }
            }
          },
          onStateChange: () => syncFromPlayer(),
        },
      })
    } catch {
      /* player creation failed — previews stay off */
    }
  }, [applyMute])

  const preview = useCallback(
    (meta: SearchResult) => {
      selectedRef.current = meta
      setSelected(meta)
      void ensurePlayer()
      const p = playerRef.current
      if (p && readyRef.current) {
        try {
          p.loadVideoById(meta.videoId)
        } catch {
          /* the user can preview again */
        }
        // Re-assert mute: loadVideoById can reset it, and unmuted autoplay is
        // blocked by browsers anyway — muted autoplay always works.
        if (mutedRef.current) {
          try {
            p.mute()
            p.setVolume(0)
          } catch {
            /* applyMute covers it */
          }
          window.setTimeout(() => {
            try {
              if (mutedRef.current) p.mute()
            } catch {
              /* the embed can be gone */
            }
          }, 600)
        }
      } else {
        // API not ready yet (or still initializing) — load on ready.
        pendingIdRef.current = meta.videoId
      }
    },
    [ensurePlayer],
  )

  const toggleMute = useCallback(() => {
    const next = !mutedRef.current
    mutedRef.current = next
    setMuted(next)
    applyMute()
  }, [applyMute])

  // Adopt embed-side video changes every 3s (legacy cadence).
  useEffect(() => {
    const id = window.setInterval(syncFromPlayer, 3000)
    return () => window.clearInterval(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return { selected, muted, slotOn, mountRef, preview, toggleMute }
}
