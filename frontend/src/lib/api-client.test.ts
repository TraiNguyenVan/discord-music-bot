import { describe, expect, it, vi } from 'vitest'
import { ApiClient } from './api-client'
import { ApiError, ExpiredSessionError, NetworkError } from './errors'

type FetchMock = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('ApiClient', () => {
  it('sends the token as a query parameter on GET and parses the response', async () => {
    const fetchMock = vi.fn<FetchMock>(async (input) => {
      expect(String(input)).toBe('/api/session?token=tok123')
      return jsonResponse(200, { ok: true, guild: 'G', user: 'U', queue_len: 2, expires_in: null })
    })
    const client = new ApiClient('tok123', fetchMock as unknown as typeof fetch)

    const session = await client.session()

    expect(session).toEqual({ ok: true, guild: 'G', user: 'U', queue_len: 2, expires_in: null })
  })

  it('sends the token in the JSON body on POST', async () => {
    const fetchMock = vi.fn<FetchMock>(async (_input, init) => {
      expect(init?.method).toBe('POST')
      expect(JSON.parse(String(init?.body))).toEqual({ token: 'tok123', action: 'pause' })
      return jsonResponse(200, { ok: true, message: 'Paused.' })
    })
    const client = new ApiClient('tok123', fetchMock as unknown as typeof fetch)

    const res = await client.control('pause')

    expect(res).toEqual({ ok: true, message: 'Paused.' })
  })

  it('maps HTTP 403 to ExpiredSessionError with the server message', async () => {
    const fetchMock = vi.fn<FetchMock>(async () =>
      jsonResponse(403, { ok: false, error: 'Link expired. Run /music mode:web again.' }),
    )
    const client = new ApiClient('tok', fetchMock as unknown as typeof fetch)

    const error = await client.now().then(
      () => null,
      (e: unknown) => e,
    )

    expect(error).toBeInstanceOf(ExpiredSessionError)
    expect((error as Error).message).toBe('Link expired. Run /music mode:web again.')
  })

  it('maps HTTP 400 to ApiError with the server message', async () => {
    const fetchMock = vi.fn<FetchMock>(async () =>
      jsonResponse(400, { ok: false, error: 'That track is no longer in the queue.' }),
    )
    const client = new ApiClient('tok', fetchMock as unknown as typeof fetch)

    const error = await client.control('remove', { qid: 'q1' }).then(
      () => null,
      (e: unknown) => e,
    )

    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).status).toBe(400)
    expect((error as Error).message).toBe('That track is no longer in the queue.')
  })

  it('maps transport failure to NetworkError', async () => {
    const fetchMock = vi.fn<FetchMock>(async () => {
      throw new TypeError('Failed to fetch')
    })
    const client = new ApiClient('tok', fetchMock as unknown as typeof fetch)

    const error = await client.now().then(
      () => null,
      (e: unknown) => e,
    )

    expect(error).toBeInstanceOf(NetworkError)
    expect(error).not.toBeInstanceOf(ApiError)
  })

  it('falls back to a status-based message for non-JSON error responses', async () => {
    const fetchMock = vi.fn<FetchMock>(async () => new Response('<html>gateway error</html>', { status: 502 }))
    const client = new ApiClient('tok', fetchMock as unknown as typeof fetch)

    const error = await client.search('test').then(
      () => null,
      (e: unknown) => e,
    )

    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).status).toBe(502)
    expect((error as Error).message).toContain('temporarily unavailable')
  })
})
