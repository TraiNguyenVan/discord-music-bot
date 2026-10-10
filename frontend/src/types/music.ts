/** Music-domain types mirroring the backend payloads traced in Checkpoint 1
 * (web/server.py + cogs/music.py). Field names are the exact wire format. */

export type LoopMode = 'off' | 'track' | 'queue'

export interface Listener {
  id: string
  name: string
  avatar: string
}

/** Row in the compact queue list of /api/now (first 10 entries). */
export interface NowQueueRow {
  title: string
  duration: number
  requester: string
  qid: string
}

/**
 * Snapshot from GET /api/now (also piggybacked on some /api/control responses).
 * The playing-only fields are absent while idle. `duration === 0` means live;
 * `elapsed` (seconds) freezes while paused.
 */
export interface NowState {
  playing: boolean
  queue_len: number
  user_len: number
  auto_len: number
  queue: NowQueueRow[]
  loop: LoopMode
  /** 0–200 */
  volume: number
  autoplay: boolean
  auto_status: string
  connected: boolean
  voice_channel: string | null
  listeners: Listener[]
  title?: string
  uploader?: string
  duration?: number
  thumbnail?: string
  webpage_url?: string
  requester?: string
  paused?: boolean
  elapsed?: number
}

/** All /api/control actions accepted by web_control (verified in Checkpoint 1). */
export type ControlAction =
  | 'toggle'
  | 'pause'
  | 'resume'
  | 'skip'
  | 'stop'
  | 'leave'
  | 'clear'
  | 'shuffle'
  | 'loop'
  | 'loop_set'
  | 'volume_set'
  | 'volume_delta'
  | 'autoplay'
  | 'autoplay_set'
  | 'remove'
  | 'jump'
  | 'join'
  | 'playlist_more'
  | 'mix_more'

/** Optional per-action parameters. remove/jump target the stable `qid`. */
export interface ControlParams {
  qid?: string
  index?: number
  mode?: string
  /** volume_set: 0–200 */
  level?: number
  /** volume_delta: signed */
  delta?: number
}
