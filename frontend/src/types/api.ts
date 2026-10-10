import type { NowState } from './music'

/** Search result as returned by /api/search, /api/related and /api/play. */
export interface SearchResult {
  videoId: string
  title: string
  uploader: string
  duration: number
  thumbnail: string
}

/** Entry of /api/config `searchBackends` — raced in parallel by web/search.py. */
export interface SearchBackend {
  url: string
  kind: string
}

export interface SessionInfo {
  ok: true
  guild: string
  user: string
  queue_len: number
  /** Heartbeat-driven sessions never expire (null); legacy countdown sessions report seconds. */
  expires_in: number | null
}

export interface ConfigInfo {
  ok: true
  searchBackends: SearchBackend[]
  searchInstances: string[]
}

export interface SearchResponse {
  ok: true
  results: SearchResult[]
  via: string
}

export interface SuggestResponse {
  ok: true
  suggestions: string[]
}

/** Row in a paginated /api/queue response. `qid` is the stable id for remove/jump. */
export interface QueueItem {
  index: number
  title: string
  duration: number
  requester: string
  uploader: string | null
  from_auto: boolean
  qid: string
}

export interface QueuePage {
  ok: true
  current: QueueItem | null
  items: QueueItem[]
  page: number
  total_pages: number
  total: number
}

export interface NowResponse {
  ok: true
  now: NowState
}

export interface ControlResponse {
  ok: true
  message: string
  /** Piggybacked fresh state for loop/loop_set/volume_set/volume_delta/autoplay/autoplay_set/jump. */
  now?: NowState
}

export type PlayKind = 'track' | 'playlist' | 'search'

export interface PlayResponse {
  ok: true
  message: string
  kind: PlayKind
  /** Present when kind === 'search'. */
  results?: SearchResult[]
  /** Present when kind === 'playlist'. */
  count?: number
}

export interface PickResponse {
  ok: true
  message: string
}

/** Body for POST /api/pick — what "Add to queue" sends. */
export interface PickTrack {
  videoId: string
  title?: string
  uploader?: string
  duration?: number
  thumbnail?: string
}
