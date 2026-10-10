import { useCallback, useRef, useState } from 'react'
import type { FormEvent, KeyboardEvent, ReactNode } from 'react'
import { Search } from 'lucide-react'
import type { ApiClient } from '../lib/api-client'
import { videoIdFromInput } from '../lib/search'
import type { SearchBackend, SearchResult } from '../types/api'
import type { StatusUpdate } from '../hooks/useMusicSession'
import type { AuditionController } from '../hooks/useAudition'
import { useSearch } from '../hooks/useSearch'
import { TrackList } from './TrackList'
import { AuditionPlayer } from './AuditionPlayer'

interface FindMusicProps {
  client: ApiClient
  backends: SearchBackend[]
  audition: AuditionController
  /** Composite preview: starts the muted embed AND re-seeds related tracks. */
  onPreview: (meta: SearchResult) => void
  onAdd: (track: SearchResult) => void
  /** Related tracks seeded by the audition (owned by PlayerView); null hides the section. */
  related: SearchResult[] | null
  pending: ReadonlySet<string>
  onStatus: (update: StatusUpdate) => void
  onExpired: (message: string) => void
}

/**
 * The discovery column: search + suggestions, link preview, the muted
 * audition player, track cards, and related tracks. All backend calls go
 * through the shared ApiClient via useSearch; the audition player comes from
 * the shared useAudition controller so quick-add results can preview too.
 */
export function FindMusic({
  client,
  backends,
  audition,
  onPreview,
  onAdd,
  related,
  pending,
  onStatus,
  onExpired,
}: FindMusicProps) {
  const search = useSearch({ client, backends, onPreview, onStatus, onExpired })
  const [linkValue, setLinkValue] = useState('')
  const inputRef = useRef<HTMLInputElement | null>(null)
  const listRef = useRef<HTMLDivElement | null>(null)

  const onSearchSubmit = useCallback(
    (e: FormEvent) => {
      e.preventDefault()
      search.submit()
    },
    [search],
  )

  const onLinkSubmit = useCallback(
    (e: FormEvent) => {
      e.preventDefault()
      const id = videoIdFromInput(linkValue)
      if (!id) {
        onStatus({
          message: 'Could not find a video ID in that text. Paste a full link, a youtu.be link, or the 11-character ID.',
          kind: 'err',
        })
        return
      }
      onPreview({
        videoId: id,
        title: `YouTube video ${id}`,
        uploader: '?',
        duration: 0,
        thumbnail: `https://i.ytimg.com/vi/${id}/hqdefault.jpg`,
      })
      onStatus({ message: 'Preview loaded — add it to the queue when it feels right.', kind: 'ok' })
    },
    [linkValue, onPreview, onStatus],
  )

  const onOptionKeyDown = useCallback(
    (e: KeyboardEvent<HTMLDivElement>, index: number) => {
      if (e.key === 'Enter') {
        e.preventDefault()
        search.pickSuggestion(search.suggestions[index])
      } else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault()
        const opts = listRef.current?.querySelectorAll<HTMLDivElement>('[role="option"]')
        if (!opts || !opts.length) return
        const next = opts[(index + (e.key === 'ArrowDown' ? 1 : opts.length - 1)) % opts.length]
        next.focus()
        search.setActiveId(next.id)
      } else if (e.key === 'Escape') {
        search.hideSuggestions()
        inputRef.current?.focus()
      }
    },
    [search],
  )

  const { state } = search
  const countText =
    state.phase === 'searching'
      ? 'Searching…'
      : state.phase === 'done' && state.results.length
        ? `${state.results.length} ${state.results.length === 1 ? 'match' : 'matches'} · ${state.via}`
        : ''

  let resultsBody: ReactNode
  if (state.phase === 'failed') {
    resultsBody = (
      <>
        <p className="empty">{state.message}</p>
        <p className="muted-note">Paste a YouTube link instead — link previews and quick-add work without search.</p>
        <button type="button" className="pill" onClick={search.retry}>
          Try again
        </button>
      </>
    )
  } else if (state.phase === 'done') {
    resultsBody = (
      <TrackList
        results={state.results}
        emptyText={'No matches. Try “artist — title”.'}
        onAudition={onPreview}
        onAdd={onAdd}
        pending={pending}
      />
    )
  } else if (state.phase === 'searching' && state.previous) {
    resultsBody = <TrackList results={state.previous} emptyText="" onAudition={onPreview} onAdd={onAdd} pending={pending} />
  } else {
    resultsBody = <p className="empty">Search above — matches land here, ready to audition or queue.</p>
  }

  const onQueueSelected = useCallback(() => {
    const sel = audition.selected
    if (sel) onAdd(sel)
  }, [audition, onAdd])

  const queuePending = audition.selected ? pending.has(`pick:${audition.selected.videoId}`) : false

  return (
    <section className="find" aria-label="Find music">
      <h2 className="h-sect">Find music</h2>
      <p className="sect-note">Tap a result to preview it muted on this device, then add it for the room.</p>

      <form className="findbar" onSubmit={onSearchSubmit}>
        <div className="searchwrap" ref={search.wrapRef}>
          <Search size={18} className="sicon" aria-hidden="true" />
          <label className="sr-only" htmlFor="q">
            Search YouTube
          </label>
          <input
            id="q"
            name="search"
            ref={inputRef}
            className="field"
            type="search"
            enterKeyHint="search"
            placeholder="Search songs, artists, or paste a YouTube link…"
            autoComplete="off"
            spellCheck={false}
            role="combobox"
            aria-autocomplete="list"
            aria-controls="sugg"
            aria-expanded={search.suggOpen}
            aria-activedescendant={search.activeId ?? undefined}
            value={search.query}
            onChange={(e) => search.onQueryChange(e.target.value)}
          />
          {search.suggOpen && (
            <div className="sugg" id="sugg" role="listbox" aria-label="Search suggestions" ref={listRef}>
              {search.suggestions.map((s, i) => (
                <div
                  key={`${i}:${s}`}
                  role="option"
                  id={`suggestion-${i}`}
                  aria-selected={search.activeId === `suggestion-${i}`}
                  tabIndex={0}
                  onClick={() => search.pickSuggestion(s)}
                  onKeyDown={(e) => onOptionKeyDown(e, i)}
                >
                  {s}
                </div>
              ))}
            </div>
          )}
        </div>
        <button className="cta" type="submit" disabled={search.searching}>
          Search
        </button>
      </form>

      <form className="linkbar" onSubmit={onLinkSubmit}>
        <label className="sr-only" htmlFor="url">
          Paste a YouTube link or video ID
        </label>
        <input
          id="url"
          name="video"
          className="field"
          type="text"
          inputMode="url"
          autoComplete="off"
          spellCheck={false}
          placeholder="Paste a YouTube link or video ID…"
          value={linkValue}
          onChange={(e) => setLinkValue(e.target.value)}
        />
        <button className="pill" type="submit">
          Preview
        </button>
      </form>

      <p className="count tnum" role="status" aria-live="polite">
        {countText}
      </p>

      <AuditionPlayer
        selected={audition.selected}
        muted={audition.muted}
        slotOn={audition.slotOn}
        mountRef={audition.mountRef}
        onToggleMute={audition.toggleMute}
        onQueue={onQueueSelected}
        queuePending={queuePending}
      />

      <section className="sect" aria-label="Search results">
        <div className="sect-row">
          <h3 className="h-sub">Matches</h3>
        </div>
        <div aria-live="polite">{resultsBody}</div>
      </section>

      {related && related.length > 0 && (
        <section className="sect" aria-label="Related tracks">
          <div className="sect-row">
            <h3 className="h-sub">More like this</h3>
            <span className="sect-note">seeded by your audition</span>
          </div>
          <TrackList results={related} emptyText="" onAudition={onPreview} onAdd={onAdd} pending={pending} />
        </section>
      )}
    </section>
  )
}
