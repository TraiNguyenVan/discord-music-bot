import { useCallback, useEffect, useRef, useState } from 'react'
import type { RefObject } from 'react'
import type { ApiClient } from '../lib/api-client'
import { ExpiredSessionError, errorMessage } from '../lib/errors'
import { raceSearch, videoIdFromInput } from '../lib/search'
import { noEmoji } from '../lib/format'
import type { SearchBackend, SearchResult } from '../types/api'

/** Request pending / results / failure are distinct phases. */
export type SearchState =
  | { phase: 'idle' }
  | { phase: 'searching'; previous: SearchResult[] | null }
  | { phase: 'done'; results: SearchResult[]; via: string }
  | { phase: 'failed'; message: string }

interface UseSearchOptions {
  client: ApiClient
  backends: SearchBackend[]
  onPreview: (meta: SearchResult) => void
  onStatus: (update: { message: string; kind: 'busy' | 'ok' | 'err' }) => void
  onExpired: (message: string) => void
}

const SUGGEST_DEBOUNCE_MS = 250
const SUGGEST_MIN_CHARS = 2

export interface SearchController {
  state: SearchState
  /** True while a search is in flight (the Search button disables). */
  searching: boolean
  /** Runs the search — or previews a pasted link/ID. Hides suggestions. */
  submit: () => void
  /** Re-runs the last query (the retry affordance). */
  retry: () => void
  query: string
  onQueryChange: (value: string) => void
  suggestions: string[]
  suggOpen: boolean
  activeId: string | null
  setActiveId: (id: string | null) => void
  pickSuggestion: (value: string) => void
  hideSuggestions: () => void
  wrapRef: RefObject<HTMLDivElement | null>
}

/**
 * The find-music state machine. Search races the browser-direct backends
 * first and falls back to the bot's `/api/search` proxy; a pasted link or
 * video ID previews directly instead of searching. Suggestions are debounced
 * 250ms, best-effort, and guarded so a slow older response can never
 * repopulate the dropdown after a newer keystroke.
 */
export function useSearch({ client, backends, onPreview, onStatus, onExpired }: UseSearchOptions): SearchController {
  const [state, setState] = useState<SearchState>({ phase: 'idle' })
  const [query, setQuery] = useState('')
  const [suggestions, setSuggestions] = useState<string[]>([])
  const [suggOpen, setSuggOpen] = useState(false)
  const [activeId, setActiveId] = useState<string | null>(null)

  const wrapRef = useRef<HTMLDivElement | null>(null)
  const suggTimer = useRef<number | null>(null)
  const suggSeq = useRef(0)
  const lastQuery = useRef('')

  // Clear a pending suggestion fetch on unmount.
  useEffect(
    () => () => {
      if (suggTimer.current !== null) window.clearTimeout(suggTimer.current)
    },
    [],
  )

  const hideSuggestions = useCallback(() => {
    setSuggOpen(false)
    setSuggestions([])
    setActiveId(null)
  }, [])

  const onQueryChange = useCallback(
    (value: string) => {
      setQuery(value)
      if (suggTimer.current !== null) window.clearTimeout(suggTimer.current)
      const q = value.trim()
      if (q.length < SUGGEST_MIN_CHARS) {
        hideSuggestions()
        return
      }
      suggTimer.current = window.setTimeout(async () => {
        const seq = ++suggSeq.current
        try {
          const res = await client.suggest(q)
          if (seq !== suggSeq.current) return // a newer keystroke superseded it
          const items = (res.suggestions ?? []).map((s) => noEmoji(s)).filter(Boolean)
          if (!items.length) {
            hideSuggestions()
            return
          }
          setSuggestions(items)
          setSuggOpen(true)
          setActiveId('suggestion-0')
        } catch {
          /* suggestions are best-effort — never block typing */
        }
      }, SUGGEST_DEBOUNCE_MS)
    },
    [client, hideSuggestions],
  )

  const runSearch = useCallback(
    (raw: string) => {
      const text = raw.trim()
      if (!text) return
      lastQuery.current = text
      // One box for both: a pasted link/ID previews directly instead of searching.
      const pastedId = videoIdFromInput(text)
      if (pastedId) {
        onPreview({
          videoId: pastedId,
          title: `YouTube video ${pastedId}`,
          uploader: '?',
          duration: 0,
          thumbnail: `https://i.ytimg.com/vi/${pastedId}/hqdefault.jpg`,
        })
        onStatus({ message: 'Preview loaded — add it to the queue when it feels right.', kind: 'ok' })
        return
      }
      void (async () => {
        setState((prev) => ({
          phase: 'searching',
          // Keep previous matches on screen while re-searching (legacy).
          previous: prev.phase === 'done' ? prev.results : null,
        }))
        let results: SearchResult[] = []
        let via = ''
        const win = await raceSearch(backends, text)
        if (win) {
          results = win.items
          via = `browser · ${win.host}`
        }
        if (!results.length) {
          try {
            const res = await client.search(text)
            results = res.results ?? []
            via = 'bot proxy'
          } catch (error) {
            if (error instanceof ExpiredSessionError) {
              onExpired(errorMessage(error))
              return
            }
            setState({ phase: 'failed', message: errorMessage(error) })
            return
          }
        }
        setState({ phase: 'done', results, via })
      })()
    },
    [backends, client, onPreview, onStatus, onExpired],
  )

  const submit = useCallback(() => {
    hideSuggestions()
    runSearch(query)
  }, [hideSuggestions, runSearch, query])

  const retry = useCallback(() => {
    hideSuggestions()
    runSearch(lastQuery.current)
  }, [hideSuggestions, runSearch])

  const pickSuggestion = useCallback(
    (value: string) => {
      setQuery(value)
      hideSuggestions()
      runSearch(value)
    },
    [hideSuggestions, runSearch],
  )

  // Click outside the search box hides the dropdown (legacy behavior).
  useEffect(() => {
    const onDocClick = (e: MouseEvent) => {
      const wrap = wrapRef.current
      if (!wrap || !suggOpen) return
      if (e.target instanceof Node && wrap.contains(e.target)) return
      hideSuggestions()
    }
    document.addEventListener('click', onDocClick)
    return () => document.removeEventListener('click', onDocClick)
  }, [hideSuggestions, suggOpen])

  return {
    state,
    searching: state.phase === 'searching',
    submit,
    retry,
    query,
    onQueryChange,
    suggestions,
    suggOpen,
    activeId,
    setActiveId,
    pickSuggestion,
    hideSuggestions,
    wrapRef,
  }
}
