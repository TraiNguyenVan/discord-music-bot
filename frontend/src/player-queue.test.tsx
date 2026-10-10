import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import type { NowState } from './types/music'
import type { SessionInfo } from './types/api'

const SESSION: SessionInfo = {
  ok: true,
  guild: 'Test Guild',
  user: 'Tester',
  queue_len: 2,
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

const NOW_PLAYING: NowState = {
  ...NOW_IDLE,
  playing: true,
  title: 'Test Song',
  uploader: 'Test Uploader',
  duration: 180,
  thumbnail: 'https://example.com/art.jpg',
  requester: 'Req',
  paused: false,
  elapsed: 30,
}

type Handler = (url: string, init?: RequestInit) => Response

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

function controlCalls(calls: Array<{ url: string; body: Record<string, unknown> | null }>) {
  return calls.filter((c) => c.url.startsWith('/api/control'))
}

function setTokenUrl(token: string): void {
  window.history.replaceState(null, '', `/?token=${token}`)
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.useRealTimers()
  window.history.replaceState(null, '', '/')
})

describe('player + queue (Checkpoint 3)', () => {
  it('renders backend-confirmed now-playing data from the snapshot', async () => {
    setTokenUrl('good-token')
    mockBackend({
      '/api/session': () => jsonResponse(200, SESSION),
      '/api/now': () => jsonResponse(200, { ok: true, now: NOW_PLAYING }),
    })

    render(<App />)

    expect(await screen.findByText('Now playing')).toBeTruthy()
    expect(screen.getAllByText('Test Song').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Test Uploader').length).toBeGreaterThan(0)
    // elapsed 30s of 180s
    expect(screen.getByText('0:30')).toBeTruthy()
    expect(screen.getByText('3:00')).toBeTruthy()
  })

  it('control accepted: server message shown and piggybacked now applies', async () => {
    setTokenUrl('good-token')
    let now = NOW_PLAYING
    const calls = mockBackend({
      '/api/session': () => jsonResponse(200, SESSION),
      '/api/now': () => jsonResponse(200, { ok: true, now }),
      '/api/control': () => {
        now = { ...NOW_PLAYING, paused: true }
        return jsonResponse(200, { ok: true, message: '**Paused** the track.', now })
      },
    })

    render(<App />)
    expect(await screen.findByText('Now playing')).toBeTruthy()

    // transport toggle + bottom bar both expose "Pause" while playing
    fireEvent.click(screen.getAllByRole('button', { name: 'Pause' })[0])

    // accepted: the cleaned server message lands in the status line
    expect(await screen.findByText('Paused the track.')).toBeTruthy()
    // confirmed: the piggybacked snapshot flips the toggle back to "Play"
    expect(screen.getAllByRole('button', { name: 'Play' }).length).toBeGreaterThan(0)
    expect(controlCalls(calls)[0]?.body).toMatchObject({ action: 'toggle' })
  })

  it('control rejected: shows the server message as an error', async () => {
    setTokenUrl('good-token')
    mockBackend({
      '/api/session': () => jsonResponse(200, SESSION),
      '/api/now': () => jsonResponse(200, { ok: true, now: NOW_PLAYING }),
      '/api/control': () => jsonResponse(400, { ok: false, error: 'Nothing is playing right now.' }),
    })

    render(<App />)
    expect(await screen.findByText('Now playing')).toBeTruthy()

    fireEvent.click(screen.getAllByRole('button', { name: 'Skip to the next track' })[0])

    expect(await screen.findByText('Nothing is playing right now.')).toBeTruthy()
    const readout = document.querySelector('.readout')
    expect(readout?.className).toContain('readout--err')
  })

  it('queue rows render from the snapshot and remove targets the stable qid', async () => {
    setTokenUrl('good-token')
    const now: NowState = {
      ...NOW_IDLE,
      queue_len: 2,
      queue: [
        { title: 'Song A', duration: 100, requester: 'Req A', qid: 'q-1' },
        { title: 'Song B', duration: 200, requester: 'Req B', qid: 'q-2' },
      ],
    }
    const calls = mockBackend({
      '/api/session': () => jsonResponse(200, SESSION),
      '/api/now': () => jsonResponse(200, { ok: true, now }),
      '/api/control': () => jsonResponse(200, { ok: true, message: 'Removed Song B.' }),
    })

    render(<App />)

    expect(await screen.findByText('Song A')).toBeTruthy()
    expect(screen.getByText('Song B')).toBeTruthy()
    expect(screen.getByText('2 tracks')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Remove Song B from the queue' }))
    expect(await screen.findByText('Removed Song B.')).toBeTruthy()

    // the stable id travels to the backend, never a shifted index
    expect(controlCalls(calls)[0]?.body).toMatchObject({ action: 'remove', qid: 'q-2' })
  })

  it('clear requires a second confirming tap within the window', async () => {
    setTokenUrl('good-token')
    const calls = mockBackend({
      '/api/session': () => jsonResponse(200, SESSION),
      '/api/now': () => jsonResponse(200, { ok: true, now: NOW_IDLE }),
      '/api/control': () => jsonResponse(200, { ok: true, message: 'Queue cleared.' }),
    })

    render(<App />)
    expect(await screen.findByText(/Nothing up next/)).toBeTruthy()

    const clear = screen.getByRole('button', { name: 'Clear' })
    fireEvent.click(clear)

    // first tap arms only: no request went out
    expect(screen.getByRole('button', { name: 'Sure?' })).toBeTruthy()
    expect(controlCalls(calls)).toHaveLength(0)

    fireEvent.click(screen.getByRole('button', { name: 'Sure?' }))
    expect(controlCalls(calls)).toHaveLength(1)
    expect(controlCalls(calls)[0]?.body).toMatchObject({ action: 'clear' })
  })

  it('a mid-session 403 escalates to the expired phase', async () => {
    setTokenUrl('good-token')
    mockBackend({
      '/api/session': () => jsonResponse(200, SESSION),
      '/api/now': () => jsonResponse(403, { ok: false, error: 'Link expired.' }),
    })

    render(<App />)

    expect(await screen.findByRole('alert')).toBeTruthy()
    expect(screen.getByText('Link expired.')).toBeTruthy()
  })

  it('volume change posts volume_set with the captured level after 150ms', async () => {
    vi.useFakeTimers()
    setTokenUrl('good-token')
    let now = NOW_PLAYING
    const calls = mockBackend({
      '/api/session': () => jsonResponse(200, SESSION),
      '/api/now': () => jsonResponse(200, { ok: true, now }),
      '/api/control': () => {
        now = { ...NOW_PLAYING, volume: 120 }
        return jsonResponse(200, { ok: true, message: 'Volume set to 120%.', now })
      },
    })

    render(<App />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(screen.getAllByText('Test Song').length).toBeGreaterThan(0)

    const slider = screen.getByRole('slider', { name: 'Bot volume' }) as HTMLInputElement
    await act(async () => {
      slider.value = '120'
      slider.dispatchEvent(new Event('input', { bubbles: true }))
      slider.dispatchEvent(new Event('change', { bubbles: true }))
    })

    // not yet: the 150ms delay has not elapsed
    expect(controlCalls(calls)).toHaveLength(0)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(150)
    })

    const controls = controlCalls(calls)
    expect(controls).toHaveLength(1)
    expect(controls[0]?.body).toMatchObject({ action: 'volume_set', level: 120 })
    // confirmed: the slider follows the server value
    expect((screen.getByRole('slider', { name: 'Bot volume' }) as HTMLInputElement).value).toBe('120')
  })
})
