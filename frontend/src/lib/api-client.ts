import type {
  ConfigInfo,
  ControlResponse,
  NowResponse,
  PickResponse,
  PickTrack,
  PlayResponse,
  QueuePage,
  SearchResponse,
  SessionInfo,
  SuggestResponse,
} from '../types/api'
import type { ControlAction, ControlParams } from '../types/music'
import { ApiError, ExpiredSessionError, NetworkError } from './errors'

type QueryParams = Record<string, string | number | undefined>

const FALLBACK_MESSAGES: Record<number, string> = {
  400: 'The bot rejected that request.',
  403: 'Link expired. Run /music mode:web again.',
  404: 'Not found.',
  502: 'Search is temporarily unavailable. Try again in a moment.',
}

/** Extract the `{ok:false,error}` message from a parsed body, if present. */
function bodyErrorMessage(body: unknown): string | null {
  if (body !== null && typeof body === 'object' && 'error' in body) {
    const message = (body as { error: unknown }).error
    if (typeof message === 'string' && message) return message
  }
  return null
}

/**
 * The single typed gateway to the aiohttp backend (contracts verified in
 * Checkpoint 1). Components must go through this client, never `fetch`.
 *
 * Token placement follows the backend: query string on GET, JSON body on POST.
 * HTTP 403 always means the session token is invalid/expired and is raised as
 * `ExpiredSessionError`; transport failures raise `NetworkError`.
 */
export class ApiClient {
  private readonly token: string
  private readonly fetchImpl: typeof fetch

  constructor(token: string, fetchImpl: typeof fetch = fetch) {
    this.token = token
    this.fetchImpl = fetchImpl
  }

  private async request<T>(path: string, init?: RequestInit): Promise<T> {
    let res: Response
    try {
      res = await this.fetchImpl(path, init)
    } catch (cause) {
      throw new NetworkError(undefined, { cause })
    }

    let body: unknown = null
    try {
      body = await res.json()
    } catch {
      body = null // non-JSON (proxy/tunnel error page, empty body)
    }

    if (!res.ok) {
      const message =
        bodyErrorMessage(body) ?? FALLBACK_MESSAGES[res.status] ?? `Request failed (HTTP ${res.status}).`
      if (res.status === 403) throw new ExpiredSessionError(message)
      throw new ApiError(message, res.status)
    }

    // Defensive: a 200 body that says ok:false is still a rejected request.
    if (
      body !== null &&
      typeof body === 'object' &&
      'ok' in body &&
      (body as { ok: unknown }).ok === false
    ) {
      throw new ApiError(bodyErrorMessage(body) ?? 'The bot rejected that request.', 0)
    }

    return body as T
  }

  private url(path: string, params?: QueryParams): string {
    const search = new URLSearchParams({ token: this.token })
    if (params) {
      for (const [key, value] of Object.entries(params)) {
        if (value !== undefined) search.set(key, String(value))
      }
    }
    return `${path}?${search.toString()}`
  }

  get<T>(path: string, params?: QueryParams): Promise<T> {
    return this.request<T>(this.url(path, params))
  }

  post<T>(path: string, body: Record<string, unknown>): Promise<T> {
    return this.request<T>(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: this.token, ...body }),
    })
  }

  // ---- typed route wrappers (one per backend endpoint) ----

  session(): Promise<SessionInfo> {
    return this.get<SessionInfo>('/api/session')
  }

  config(): Promise<ConfigInfo> {
    return this.get<ConfigInfo>('/api/config')
  }

  search(q: string): Promise<SearchResponse> {
    return this.get<SearchResponse>('/api/search', { q })
  }

  suggest(q: string): Promise<SuggestResponse> {
    return this.get<SuggestResponse>('/api/suggest', { q })
  }

  related(videoId: string): Promise<SearchResponse> {
    return this.get<SearchResponse>('/api/related', { videoId })
  }

  now(): Promise<NowResponse> {
    return this.get<NowResponse>('/api/now')
  }

  queue(page = 1): Promise<QueuePage> {
    return this.get<QueuePage>('/api/queue', { page })
  }

  control(action: ControlAction, params: ControlParams = {}): Promise<ControlResponse> {
    return this.post<ControlResponse>('/api/control', { action, ...params })
  }

  play(query: string): Promise<PlayResponse> {
    return this.post<PlayResponse>('/api/play', { query })
  }

  pick(track: PickTrack): Promise<PickResponse> {
    return this.post<PickResponse>('/api/pick', { ...track })
  }
}
