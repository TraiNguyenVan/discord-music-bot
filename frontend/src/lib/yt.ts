/**
 * YouTube IFrame Player API loader (mirrors the legacy picker's audition
 * slice). The script is injected lazily on first use; previews always start
 * muted (`mute: 1` + `playsinline: 1`) so they never fight the room's audio.
 */

export interface YTPlayer {
  loadVideoById(id: string): void
  mute(): void
  unMute(): void
  setVolume(volume: number): void
  getVideoData?(): { video_id: string; title: string; author: string }
  getDuration?(): number
}

export interface YTNamespace {
  Player: new (
    element: HTMLElement,
    options: {
      height: string
      width: string
      playerVars: { rel: number; mute: number; playsinline: number }
      events: {
        onReady?: (event: { target: YTPlayer }) => void
        onStateChange?: (event: { target: YTPlayer }) => void
      }
    },
  ) => YTPlayer
}

interface WindowWithYT {
  YT?: YTNamespace
  onYouTubeIframeAPIReady?: () => void
}

let loading: Promise<YTNamespace> | null = null

/**
 * Resolve the IFrame API namespace, loading the script on first use. A
 * pre-existing `window.YT` (a previously loaded page, or a test stub) is
 * used directly, so the promise cache only covers the script-load path.
 */
export function ensureYTApi(): Promise<YTNamespace> {
  const w = window as unknown as WindowWithYT
  if (w.YT?.Player) return Promise.resolve(w.YT)
  if (!loading) {
    loading = new Promise<YTNamespace>((resolve) => {
      const previous = w.onYouTubeIframeAPIReady
      w.onYouTubeIframeAPIReady = () => {
        previous?.()
        if (w.YT?.Player) resolve(w.YT)
      }
      const script = document.createElement('script')
      script.src = 'https://www.youtube.com/iframe_api'
      script.async = true
      document.head.appendChild(script)
    })
  }
  return loading
}
