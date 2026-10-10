import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import type { NowState } from './types/music'
import type { SearchResult, SessionInfo } from './types/api'

const SESSION: SessionInfo = {
  ok: true,
  guild: 'Test Guild',
  user: 'Tester',
  queue_len: 0,
  expires_in: null,
}

const NOW_IDLE: NowState = {
  playing: false,
  queue_len: 0,
  user_len: 0,
  auto_len: 0,
  queue: [],
  loop: 'off',
  volume: 50,
  autoplay: true,
  auto_status: '',
  connected: true,
  voice_channel: 'General',
  listeners: [],
}

const RESULTS: SearchResult[] = [
  {
    videoId: 'aaaaaaaaaaa',
    title: 'Test Song One',
    uploader: 'Uploader One',
    duration: 200,
    thumbnail: 'https://example.com/1.jpg',
  },
  {
    videoId: 'bbbbbbbbbbb',
    title: 'Test Song Two',
    uploader: 'Uploader Two',
    duration: 0,
    thumbnail: 'https://example.com/2.jpg',
  },
]

const EMPTY_CONFIG = { ok: true, searchBackends: [], searchInstances: [] }

/** Handlers may return the Response directly or a promise held open for pending-state assertions. */
type Handler = (url: string, init?: RequestInit) => Response | Promise<Response>

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

/** Route-dispatching fetch mock; records every call for request assertions. */
function mockBackend(routes: Record<string, Handler>) {
  const calls: Array<{ url: string; body: Record<string, unknown> | null }> = []
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    const path = url.split('?')[0]
    calls.push({ url, body: typeof init?.body === 'string' ? JSON.parse(init.body) : null })
    const handler = routes[path]
    if (!handler) throw new TypeError(`No mock route for ${path}`)
    return handler(url, init)
  })
  vi.stubGlobal('fetch', fetchMock)
  return calls
}

function pickCalls(calls: Array<{ url: string; body: Record<string, unknown> | null }>) {
  return calls.filter((c) => c.url.startsWith('/api/pick'))
}

function setTokenUrl(token: string): void {
  window.history.replaceState(null, '', `/?token=${token}`)
}

/** Deterministic YT IFrame stand: onReady fires in the constructor. */
class FakeYTPlayer {
  static instances: FakeYTPlayer[] = []
  calls: string[] = []
  videoId: string | null = null

  constructor(_el: HTMLElement, opts: { events: { onReady?: (e: { target: FakeYTPlayer }) => void } }) {
    FakeYTPlayer.instances.push(this)
    opts.events.onReady?.({ target: this })
  }

  loadVideoById(id: string) {
    this.calls.push(`load:${id}`)
    this.videoId = id
  }

  mute() {
    this.calls.push('mute')
  }

  unMute() {
    this.calls.push('unmute')
  }

  setVolume(v: number) {
    this.calls.push(`vol:${v}`)
  }

  getVideoData() {
    return this.videoId ? { video_id: this.videoId, title: 'Adopted Title', author: 'Adopter' } : null
  }

  getDuration() {
    return 120
  }
}

function stubYT(): void {
  ;(window as unknown as { YT?: unknown }).YT = { Player: FakeYTPlayer }
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.useRealTimers()
  delete (window as unknown as { YT?: unknown }).YT
  FakeYTPlayer.instances = []
  window.history.replaceState(null, '', '/')
})

describe('search + audition (Checkpoint 4)', () => {
  it('search falls back to the bot proxy when the browser backends fail', async () => {
    setTokenUrl('good-token')
    const calls = mockBackend({
      '/api/session': () => jsonResponse(200, SESSION),
      '/api/now': () => jsonResponse(200, { ok: true, now: NOW_IDLE }),
      '/api/config': () => jsonResponse(200, EMPTY_CONFIG),
      '/api/search': () => jsonResponse(200, { ok: true, results: RESULTS, via: 'bot' }),
    })

    render(<App />)
    expect(await screen.findByText(/Nothing up next/)).toBeTruthy()

    fireEvent.change(screen.getByLabelText('Search YouTube'), { target: { value: 'test song' } })
    fireEvent.click(screen.getByRole('button', { name: 'Search' }))

    expect(await screen.findByText('Test Song One')).toBeTruthy()
    expect(screen.getByText('2 matches · bot proxy')).toBeTruthy()
    expect(calls.filter((c) => c.url.startsWith('/api/search'))).toHaveLength(1)
  })

  it('a winning browser race renders its rows without calling the bot proxy', async () => {
    setTokenUrl('good-token')
    const calls = mockBackend({
      '/api/session': () => jsonResponse(200, SESSION),
      '/api/now': () => jsonResponse(200, { ok: true, now: NOW_IDLE }),
      '/api/config': () =>
        jsonResponse(200, { ok: true, searchBackends: [{ url: 'https://piped.example', kind: 'piped' }], searchInstances: [] }),
      'https://piped.example/search': () =>
        jsonResponse(200, {
          items: [{ url: '/watch?v=ccccccccccc', title: 'Raced Song', uploaderName: 'Racer', duration: 90 }],
        }),
    })

    render(<App />)
    expect(await screen.findByText(/Nothing up next/)).toBeTruthy()

    fireEvent.change(screen.getByLabelText('Search YouTube'), { target: { value: 'raced' } })
    fireEvent.click(screen.getByRole('button', { name: 'Search' }))

    expect(await screen.findByText('Raced Song')).toBeTruthy()
    expect(screen.getByText('1 match · browser · piped.example')).toBeTruthy()
    expect(calls.filter((c) => c.url.startsWith('/api/search'))).toHaveLength(0)
  })

  it('suggestions open after the 250ms debounce, and Enter picks one', async () => {
    vi.useFakeTimers()
    setTokenUrl('good-token')
    const calls = mockBackend({
      '/api/session': () => jsonResponse(200, SESSION),
      '/api/now': () => jsonResponse(200, { ok: true, now: NOW_IDLE }),
      '/api/config': () => jsonResponse(200, EMPTY_CONFIG),
      '/api/suggest': () => jsonResponse(200, { ok: true, suggestions: ['test song one', 'test song two'] }),
      '/api/search': () => jsonResponse(200, { ok: true, results: RESULTS, via: 'bot proxy' }),
    })

    render(<App />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(screen.getByText(/Nothing up next/)).toBeTruthy()

    const input = screen.getByLabelText('Search YouTube') as HTMLInputElement
    fireEvent.change(input, { target: { value: 'te' } })

    // not yet: the debounce has not elapsed
    expect(screen.queryByRole('listbox')).toBeNull()
    expect(calls.filter((c) => c.url.startsWith('/api/suggest'))).toHaveLength(0)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(250)
    })
    const options = screen.getAllByRole('option')
    expect(options).toHaveLength(2)
    expect((input as HTMLInputElement).getAttribute('aria-expanded')).toBe('true')

    // keyboard nav: ArrowDown moves into the list, Enter picks
    fireEvent.keyDown(options[0], { key: 'ArrowDown' })
    expect(document.activeElement).toBe(options[1])
    fireEvent.keyDown(options[1], { key: 'Enter' })

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(input.value).toBe('test song two')
    expect(screen.queryAllByRole('option')).toHaveLength(0) // dropdown closed
    expect(calls.filter((c) => c.url.startsWith('/api/suggest'))).toHaveLength(1)
  })

  it('add-to-queue posts /api/pick, shows pending, then the server message', async () => {
    setTokenUrl('good-token')
    let resolvePick: ((r: Response) => void) | null = null
    const calls = mockBackend({
      '/api/session': () => jsonResponse(200, SESSION),
      '/api/now': () => jsonResponse(200, { ok: true, now: NOW_IDLE }),
      '/api/config': () => jsonResponse(200, EMPTY_CONFIG),
      '/api/search': () => jsonResponse(200, { ok: true, results: RESULTS, via: 'bot proxy' }),
      '/api/pick': () =>
        new Promise<Response>((resolve) => {
          resolvePick = resolve
        }),
    })

    render(<App />)
    expect(await screen.findByText(/Nothing up next/)).toBeTruthy()

    fireEvent.change(screen.getByLabelText('Search YouTube'), { target: { value: 'test song' } })
    fireEvent.click(screen.getByRole('button', { name: 'Search' }))
    expect(await screen.findByText('Test Song Two')).toBeTruthy()

    const add = screen.getByRole('button', { name: 'Add Test Song Two to the queue' }) as HTMLButtonElement
    fireEvent.click(add)

    // pending: the tapped button disables while the request is in flight
    expect(add.disabled).toBe(true)
    expect(pickCalls(calls)).toHaveLength(1)
    expect(pickCalls(calls)[0]?.body).toMatchObject({ videoId: 'bbbbbbbbbbb', title: 'Test Song Two' })

    await act(async () => {
      resolvePick?.(jsonResponse(200, { ok: true, message: '**Added** Test Song Two.' }))
    })

    // accepted: the cleaned server message lands in the status line
    expect(await screen.findByText('Added Test Song Two.')).toBeTruthy()
    expect(add.disabled).toBe(false)
  })

  it('a pasted link in the search box previews instead of searching', async () => {
    setTokenUrl('good-token')
    stubYT()
    const calls = mockBackend({
      '/api/session': () => jsonResponse(200, SESSION),
      '/api/now': () => jsonResponse(200, { ok: true, now: NOW_IDLE }),
      '/api/config': () => jsonResponse(200, EMPTY_CONFIG),
      '/api/pick': () => jsonResponse(200, { ok: true, message: 'Added.' }),
    })

    render(<App />)
    expect(await screen.findByText(/Nothing up next/)).toBeTruthy()

    fireEvent.change(screen.getByLabelText('Search YouTube'), {
      target: { value: 'https://youtu.be/ccccccccccc' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Search' }))

    // the audition picked it up; no search request went out
    expect(await screen.findByText('YouTube video ccccccccccc')).toBeTruthy()
    expect(calls.filter((c) => c.url.startsWith('/api/search'))).toHaveLength(0)
    expect(FakeYTPlayer.instances[0]?.calls).toContain('load:ccccccccccc')
    expect(FakeYTPlayer.instances[0]?.calls).toContain('mute')
  })

  it('the audition player loads muted, unmutes on toggle, and queues the shown track', async () => {
    setTokenUrl('good-token')
    stubYT()
    const calls = mockBackend({
      '/api/session': () => jsonResponse(200, SESSION),
      '/api/now': () => jsonResponse(200, { ok: true, now: NOW_IDLE }),
      '/api/config': () => jsonResponse(200, EMPTY_CONFIG),
      '/api/pick': () => jsonResponse(200, { ok: true, message: 'Added to the queue.' }),
    })

    render(<App />)
    expect(await screen.findByText(/Nothing up next/)).toBeTruthy()

    fireEvent.change(screen.getByLabelText('Paste a YouTube link or video ID'), {
      target: { value: 'ccccccccccc' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }))

    expect(await screen.findByText('YouTube video ccccccccccc')).toBeTruthy()
    const player = FakeYTPlayer.instances[0]
    expect(player?.calls).toContain('load:ccccccccccc')
    expect(player?.calls).toContain('mute') // previews always start muted

    fireEvent.click(screen.getByRole('button', { name: 'Unmute preview' }))
    expect(player?.calls).toContain('unmute')
    expect(screen.getByRole('button', { name: 'Mute preview' })).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Add YouTube video ccccccccccc to the queue' }))
    expect(await screen.findByText('Added to the queue.')).toBeTruthy()
    expect(pickCalls(calls)[0]?.body).toMatchObject({ videoId: 'ccccccccccc' })
  })

  it('quick-add resolves on the bot: track answers show the message, search answers render rows', async () => {
    setTokenUrl('good-token')
    let kind: 'track' | 'search' = 'track'
    mockBackend({
      '/api/session': () => jsonResponse(200, SESSION),
      '/api/now': () => jsonResponse(200, { ok: true, now: NOW_IDLE }),
      '/api/config': () => jsonResponse(200, EMPTY_CONFIG),
      '/api/play': () =>
        kind === 'track'
          ? jsonResponse(200, { ok: true, message: '**Playing** it now.', kind: 'track' })
          : jsonResponse(200, { ok: true, message: 'Pick one below.', kind: 'search', results: RESULTS }),
    })

    render(<App />)
    expect(await screen.findByText(/Nothing up next/)).toBeTruthy()

    const input = screen.getByLabelText('Song name or YouTube link') as HTMLInputElement
    const add = screen.getByRole('button', { name: 'Add' })

    fireEvent.change(input, { target: { value: 'some song' } })
    fireEvent.click(add)
    expect(await screen.findByText('Playing it now.')).toBeTruthy()
    expect(screen.queryByText('Test Song One')).toBeNull()

    // the bot answered with search candidates: they render, ready to audition
    kind = 'search'
    fireEvent.change(input, { target: { value: 'some song' } })
    fireEvent.click(add)
    expect(await screen.findByText('Pick one below.')).toBeTruthy()
    expect(screen.getByText('Test Song One')).toBeTruthy()
    // the input clears after a successful resolve
    expect(input.value).toBe('')
  })

  it('quick-add failure shows the bot rejection as an error and keeps the text', async () => {
    setTokenUrl('good-token')
    mockBackend({
      '/api/session': () => jsonResponse(200, SESSION),
      '/api/now': () => jsonResponse(200, { ok: true, now: NOW_IDLE }),
      '/api/config': () => jsonResponse(200, EMPTY_CONFIG),
      '/api/play': () => jsonResponse(502, { ok: false, error: 'Search backend down.' }),
    })

    render(<App />)
    expect(await screen.findByText(/Nothing up next/)).toBeTruthy()

    const input = screen.getByLabelText('Song name or YouTube link') as HTMLInputElement
    fireEvent.change(input, { target: { value: 'some song' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add' }))

    expect(await screen.findByText('Search backend down.')).toBeTruthy()
    const readout = document.querySelector('.readout')
    expect(readout?.className).toContain('readout--err')
    expect(input.value).toBe('some song') // kept so a retry is one tap away
  })

  it('a 403 from /api/search escalates to the expired phase', async () => {
    setTokenUrl('good-token')
    mockBackend({
      '/api/session': () => jsonResponse(200, SESSION),
      '/api/now': () => jsonResponse(200, { ok: true, now: NOW_IDLE }),
      '/api/config': () => jsonResponse(200, EMPTY_CONFIG),
      '/api/search': () => jsonResponse(403, { ok: false, error: 'Link expired.' }),
    })

    render(<App />)
    expect(await screen.findByText(/Nothing up next/)).toBeTruthy()

    fireEvent.change(screen.getByLabelText('Search YouTube'), { target: { value: 'test song' } })
    fireEvent.click(screen.getByRole('button', { name: 'Search' }))

    expect(await screen.findByRole('alert')).toBeTruthy()
    expect(screen.getByText('Link expired.')).toBeTruthy()
  })

  it('a failed search offers Try again, which re-runs the last query', async () => {
    setTokenUrl('good-token')
    let fail = true
    mockBackend({
      '/api/session': () => jsonResponse(200, SESSION),
      '/api/now': () => jsonResponse(200, { ok: true, now: NOW_IDLE }),
      '/api/config': () => jsonResponse(200, EMPTY_CONFIG),
      '/api/search': () =>
        fail
          ? jsonResponse(502, { ok: false, error: 'All search backends failed.' })
          : jsonResponse(200, { ok: true, results: RESULTS, via: 'bot proxy' }),
    })

    render(<App />)
    expect(await screen.findByText(/Nothing up next/)).toBeTruthy()

    fireEvent.change(screen.getByLabelText('Search YouTube'), { target: { value: 'test song' } })
    fireEvent.click(screen.getByRole('button', { name: 'Search' }))

    expect(await screen.findByText('All search backends failed.')).toBeTruthy()

    fail = false
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(await screen.findByText('Test Song One')).toBeTruthy()
    expect(screen.getByText('2 matches · bot proxy')).toBeTruthy()
  })
})
