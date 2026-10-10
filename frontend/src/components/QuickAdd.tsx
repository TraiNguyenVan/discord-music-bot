import { useCallback, useState } from 'react'
import type { FormEvent } from 'react'
import { LoaderCircle } from 'lucide-react'
import type { PlayResponse, SearchResult } from '../types/api'
import { TrackList } from './TrackList'

interface QuickAddProps {
  /** Resolves the query on the bot (/api/play). Returns the response, or null on failure. */
  onPlay: (key: string, query: string) => Promise<PlayResponse | null>
  onPreview: (meta: SearchResult) => void
  onAdd: (track: SearchResult) => void
  pending: ReadonlySet<string>
}

/**
 * "Add for the room": the same smart router as the bot's /play command — a
 * name, a link, or a playlist. When the bot answers with search results they
 * render below the form, ready to audition or queue.
 */
export function QuickAdd({ onPlay, onPreview, onAdd, pending }: QuickAddProps) {
  const [q, setQ] = useState('')
  const [results, setResults] = useState<SearchResult[] | null>(null)

  const onSubmit = useCallback(
    async (event: FormEvent) => {
      event.preventDefault()
      const text = q.trim()
      if (!text || pending.has('add')) return
      setResults(null)
      const res = await onPlay('add', text)
      if (res && res.kind === 'search' && res.results?.length) setResults(res.results)
      // Keep the text on failure so a retry is one tap away.
      if (res) setQ('')
    },
    [q, pending, onPlay],
  )

  return (
    <section className="quickadd" aria-label="Quick add">
      <h2 className="h-sect">Add for the room</h2>
      <p className="sect-note">A name, a link, or a playlist — the bot resolves it.</p>
      <form className="addbar" onSubmit={onSubmit}>
        <label className="sr-only" htmlFor="addq">
          Song name or YouTube link
        </label>
        <input
          id="addq"
          name="add-query"
          className="field"
          type="text"
          autoComplete="off"
          spellCheck={false}
          placeholder="Song, link, or playlist…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <button className="cta" type="submit" disabled={pending.has('add')}>
          {pending.has('add') && <LoaderCircle size={13} className="spin" aria-hidden="true" />} Add
        </button>
      </form>
      <div aria-live="polite">
        {results && <TrackList results={results} emptyText="" onAudition={onPreview} onAdd={onAdd} pending={pending} />}
      </div>
    </section>
  )
}
