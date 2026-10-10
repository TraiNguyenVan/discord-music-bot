import type { SearchBackend, SearchResult } from '../types/api'

/**
 * Browser-direct search providers (mirrors the legacy picker's discovery
 * slice). Every Piped/Invidious instance is raced in parallel — one provider
 * die-off can't kill search — and the bot's `/api/search` proxy is only the
 * fallback when all of them fail. These requests intentionally bypass the
 * ApiClient: they talk to third-party instances, not to the bot.
 */

/** Backends used until /api/config answers (the legacy defaults). */
export const DEFAULT_SEARCH_BACKENDS: SearchBackend[] = [
  { url: 'https://api.piped.private.coffee', kind: 'piped' },
  { url: 'https://invidious.f5.si', kind: 'invidious' },
]

/** Map an /api/config payload to the backend list, mirroring the legacy rules. */
export function backendsFromConfig(config: { searchBackends?: SearchBackend[]; searchInstances?: string[] }): SearchBackend[] {
  if (config.searchBackends && config.searchBackends.length) return config.searchBackends
  if (config.searchInstances && config.searchInstances.length) {
    return config.searchInstances.map((url) => ({ url, kind: 'piped' }))
  }
  return []
}

/** Extract a YouTube video id from raw text: a bare 11-char id, `?&v=`,
 *  `youtu.be/`, `/shorts/`, or `/embed/` links. */
export function videoIdFromInput(raw: string | null | undefined): string | null {
  const s = String(raw ?? '').trim()
  if (!s) return null
  if (/^[A-Za-z0-9_-]{11}$/.test(s)) return s
  const m =
    s.match(/[?&]v=([A-Za-z0-9_-]{11})/) ||
    s.match(/youtu\.be\/([A-Za-z0-9_-]{11})/) ||
    s.match(/\/shorts\/([A-Za-z0-9_-]{11})/) ||
    s.match(/\/embed\/([A-Za-z0-9_-]{11})/)
  return m ? m[1] : null
}

function fetchJson(url: string, ms: number): Promise<unknown> {
  const ctl = new AbortController()
  const timer = setTimeout(() => ctl.abort(), ms)
  return fetch(url, { signal: ctl.signal })
    .then((r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      return r.json()
    })
    .finally(() => clearTimeout(timer))
}

/** Normalize Piped `/search` + `/streams` items (deduped) into result rows. */
function normPiped(items: unknown): SearchResult[] {
  const out: SearchResult[] = []
  const seen = new Set<string>()
  for (const it of Array.isArray(items) ? items : []) {
    const row = it as { url?: unknown; title?: unknown; uploaderName?: unknown; duration?: unknown }
    if (!row || typeof row.url !== 'string' || row.url.indexOf('/watch') < 0) continue
    const m = row.url.match(/[?&]v=([A-Za-z0-9_-]{11})/)
    if (!m || seen.has(m[1])) continue
    seen.add(m[1])
    out.push({
      videoId: m[1],
      title: String(row.title || 'Unknown title').slice(0, 200),
      uploader: String(row.uploaderName || '?').slice(0, 200),
      duration: Math.max(0, parseInt(String(row.duration || 0), 10) || 0),
      thumbnail: `https://i.ytimg.com/vi/${m[1]}/hqdefault.jpg`,
    })
  }
  return out
}

/** Normalize Invidious `/api/v1/search` + `/api/v1/videos` items (deduped). */
function normInvidious(items: unknown): SearchResult[] {
  const out: SearchResult[] = []
  const seen = new Set<string>()
  for (const it of Array.isArray(items) ? items : []) {
    const row = it as { videoId?: unknown; title?: unknown; author?: unknown; lengthSeconds?: unknown; type?: unknown }
    if (!row || typeof row !== 'object') continue
    const vid = typeof row.videoId === 'string' ? row.videoId : null
    if (!vid || seen.has(vid)) continue
    if (typeof row.type === 'string' && row.type !== 'video') continue
    seen.add(vid)
    out.push({
      videoId: vid,
      title: String(row.title || 'Unknown title').slice(0, 200),
      uploader: String(row.author || '?').slice(0, 200),
      duration: Math.max(0, parseInt(String(row.lengthSeconds || 0), 10) || 0),
      thumbnail: `https://i.ytimg.com/vi/${vid}/hqdefault.jpg`,
    })
  }
  return out
}

export interface RaceWin {
  items: SearchResult[]
  host: string
}

const RACE_TIMEOUT_MS = 6000

/** Race every backend at once; the first with results wins, failures are ignored. */
export async function raceSearch(backends: SearchBackend[], q: string): Promise<RaceWin | null> {
  const attempts = backends.map(async (b) => {
    const url =
      b.kind === 'invidious'
        ? `${b.url}/api/v1/search?q=${encodeURIComponent(q)}&type=video`
        : `${b.url}/search?q=${encodeURIComponent(q)}&filter=videos`
    const data = await fetchJson(url, RACE_TIMEOUT_MS)
    const items =
      b.kind === 'invidious'
        ? normInvidious(data)
        : normPiped((data as { items?: unknown } | null)?.items ?? [])
    if (!items.length) throw new Error('no items')
    let host = b.url
    try {
      host = new URL(b.url).host
    } catch {
      /* keep the raw url as the host label */
    }
    return { items, host }
  })
  try {
    return await Promise.any(attempts)
  } catch {
    return null
  }
}

/** Race related-track lookups the same way; an empty list means "none". */
export async function raceRelated(backends: SearchBackend[], videoId: string): Promise<SearchResult[]> {
  const attempts = backends.map(async (b) => {
    const url =
      b.kind === 'invidious'
        ? `${b.url}/api/v1/videos/${encodeURIComponent(videoId)}`
        : `${b.url}/streams/${encodeURIComponent(videoId)}`
    const data = await fetchJson(url, RACE_TIMEOUT_MS)
    const items =
      b.kind === 'invidious'
        ? normInvidious((data as { recommendedVideos?: unknown } | null)?.recommendedVideos ?? [])
        : normPiped((data as { relatedStreams?: unknown } | null)?.relatedStreams ?? [])
    if (!items.length) throw new Error('no items')
    return items
  })
  try {
    return await Promise.any(attempts)
  } catch {
    return []
  }
}
