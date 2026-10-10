/** Error taxonomy for the centralized API client. */

/** Non-2xx (or a 200 body with ok:false) response from the backend. */
export class ApiError extends Error {
  /** HTTP status; 0 when a 200 body carried ok:false. */
  readonly status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

/** HTTP 403 — the session token is invalid or the link expired. */
export class ExpiredSessionError extends ApiError {
  constructor(message = 'Link expired. Run /music mode:web again.') {
    super(message, 403)
    this.name = 'ExpiredSessionError'
  }
}

/** fetch itself rejected — backend unreachable, tunnel down, or offline. */
export class NetworkError extends Error {
  constructor(message = 'Cannot reach the bot. Check your connection and try again.', options?: ErrorOptions) {
    super(message, options)
    this.name = 'NetworkError'
  }
}

export function isExpiredSession(error: unknown): error is ExpiredSessionError {
  return error instanceof ExpiredSessionError
}

export function isNetworkError(error: unknown): error is NetworkError {
  return error instanceof NetworkError
}

/** Best-effort user-facing message from any thrown value. */
export function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message
  return String(error)
}
