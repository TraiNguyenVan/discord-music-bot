import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import App from './App'

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function setTokenUrl(token: string): void {
  window.history.replaceState(null, '', `/?token=${token}`)
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  window.history.replaceState(null, '', '/')
})

describe('App session shell', () => {
  it('shows an actionable expired-session state on HTTP 403', async () => {
    setTokenUrl('stale-token')
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse(403, { ok: false, error: 'Link expired. Run /music mode:web again.' })),
    )

    render(<App />)

    expect(await screen.findByRole('alert')).toBeTruthy()
    expect(screen.getByText('Link expired. Run /music mode:web again.')).toBeTruthy()
    expect(screen.queryByText(/Checking your session/)).toBeNull()
  })

  it('shows an actionable error when the token is missing', async () => {
    render(<App />)

    expect(await screen.findByRole('alert')).toBeTruthy()
    expect(screen.getByText(/Missing session token/)).toBeTruthy()
  })

  it('shows backend-confirmed session data when validation succeeds', async () => {
    setTokenUrl('good-token')
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse(200, { ok: true, guild: 'Test Guild', user: 'Tester', queue_len: 3, expires_in: null }),
      ),
    )

    render(<App />)

    expect(await screen.findByText('Test Guild')).toBeTruthy()
    expect(screen.getByText('Tester')).toBeTruthy()
    // heartbeat-driven session: no countdown, the link lives while in use
    expect(screen.getByText('live while in use')).toBeTruthy()
  })

  it('recovers via retry after a network failure', async () => {
    setTokenUrl('good-token')
    const fetchMock = vi.fn<() => Promise<Response>>(async () => {
      throw new TypeError('Failed to fetch')
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<App />)

    expect(await screen.findByRole('alert')).toBeTruthy()
    expect(screen.getByText(/Cannot reach the bot/)).toBeTruthy()

    fetchMock.mockImplementation(async () =>
      jsonResponse(200, { ok: true, guild: 'Test Guild', user: 'Tester', queue_len: 0, expires_in: null }),
    )
    fireEvent.click(screen.getByRole('button', { name: /Retry/ }))

    await waitFor(() => {
      expect(screen.getByText('Test Guild')).toBeTruthy()
    })
  })
})
