import { useCallback, useEffect, useState } from 'react'
import { LoaderCircle, RefreshCw, TriangleAlert } from 'lucide-react'
import { ApiClient } from './lib/api-client'
import { ExpiredSessionError, errorMessage } from './lib/errors'
import { PlayerView } from './components/PlayerView'
import type { SessionInfo } from './types/api'

/** Request pending / rejected / backend-confirmed are distinct phases. */
type SessionState =
  | { phase: 'loading' }
  | { phase: 'ready'; client: ApiClient; session: SessionInfo }
  | { phase: 'expired'; message: string }
  | { phase: 'error'; message: string }

function readToken(): string | null {
  return new URLSearchParams(window.location.search).get('token')
}

export default function App() {
  const [state, setState] = useState<SessionState>({ phase: 'loading' })
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    const token = readToken()
    if (!token) {
      setState({
        phase: 'expired',
        message: 'Missing session token. Run /music mode:web again to get a fresh link.',
      })
      return
    }

    const client = new ApiClient(token)
    let cancelled = false
    setState({ phase: 'loading' })

    client
      .session()
      .then((session) => {
        if (!cancelled) setState({ phase: 'ready', client, session })
      })
      .catch((error: unknown) => {
        if (cancelled) return
        if (error instanceof ExpiredSessionError) {
          setState({ phase: 'expired', message: errorMessage(error) })
        } else {
          // Network and other failures are recoverable via retry.
          setState({ phase: 'error', message: errorMessage(error) })
        }
      })

    return () => {
      cancelled = true
    }
  }, [attempt])

  const retry = useCallback(() => setAttempt((n) => n + 1), [])
  const handleExpired = useCallback((message: string) => setState({ phase: 'expired', message }), [])

  if (state.phase === 'loading') {
    return (
      <main className="app-shell">
        <div className="state-card state-card--center" role="status" aria-live="polite">
          <LoaderCircle size={28} className="state-icon spin" aria-hidden="true" />
          <h1>Checking your session…</h1>
        </div>
      </main>
    )
  }

  if (state.phase === 'expired') {
    return (
      <main className="app-shell">
        <div className="state-card state-card--center" role="alert">
          <TriangleAlert size={28} className="state-icon state-icon--danger" aria-hidden="true" />
          <h1>Session unavailable</h1>
          <p>{state.message}</p>
        </div>
      </main>
    )
  }

  if (state.phase === 'error') {
    return (
      <main className="app-shell">
        <div className="state-card state-card--center" role="alert">
          <TriangleAlert size={28} className="state-icon state-icon--danger" aria-hidden="true" />
          <h1>Something went wrong</h1>
          <p>{state.message}</p>
          <button type="button" className="button" onClick={retry}>
            <RefreshCw size={14} aria-hidden="true" /> Retry
          </button>
        </div>
      </main>
    )
  }

  // A 403 from any later call (poll, control, queue) escalates here too.
  return <PlayerView client={state.client} session={state.session} onExpired={handleExpired} />
}
