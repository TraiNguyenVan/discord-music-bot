import { useEffect, useState } from 'react'
import { CircleCheck } from 'lucide-react'
import type { SessionInfo } from '../types/api'
import type { ControlAction, ControlParams, NowState } from '../types/music'
import type { StatusUpdate } from '../hooks/useMusicSession'
import { VoicePill } from './VoicePill'

export interface AppHeaderProps {
  session: SessionInfo
  now: NowState | null
  pending: ReadonlySet<string>
  onControl: (key: string, action: ControlAction, params?: ControlParams) => void
  onStatus: (update: StatusUpdate) => void
}

/**
 * Heartbeat-driven sessions never expire (`expires_in: null` → the link lives
 * while it is used); legacy countdown sessions show a quiet timer that only
 * becomes a countdown in the final ~5 minutes.
 */
function ExpiryTimer({ expiresIn }: { expiresIn: number | null }) {
  const [label, setLabel] = useState(() => (expiresIn === null ? 'live while in use' : ''))
  const [urgent, setUrgent] = useState(false)

  useEffect(() => {
    if (expiresIn === null) {
      setLabel('live while in use')
      setUrgent(false)
      return
    }
    const expiresAt = Date.now() + expiresIn * 1000
    const tick = () => {
      const left = expiresAt - Date.now()
      if (left <= 0) {
        setLabel('link expired')
        setUrgent(true)
        return
      }
      if (left <= 5 * 60 * 1000) {
        const s = Math.ceil(left / 1000)
        setLabel(`Link expires in ${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`)
        setUrgent(true)
      } else {
        setLabel('live while in use')
        setUrgent(false)
      }
    }
    tick()
    const id = window.setInterval(tick, 1000)
    return () => window.clearInterval(id)
  }, [expiresIn])

  return <span className={urgent ? 'expiry expiry--urgent' : 'expiry'}>{label}</span>
}

export function AppHeader({ session, now, pending, onControl, onStatus }: AppHeaderProps) {
  const connected = !!now?.connected
  const lampClass =
    'lamp' + (connected ? (now?.playing ? ' lamp--live' : ' lamp--on') : '')

  return (
    <header className="app-header">
      <div className="brand">
        <span className={lampClass} aria-hidden="true" />
        <h1>Music Picker</h1>
      </div>
      <div className="session-side">
        <VoicePill
          now={now}
          pending={pending.has('join') || pending.has('leave')}
          onConnect={() => onControl('join', 'join')}
          onLeave={() => onControl('leave', 'leave')}
          onArm={() =>
            onStatus({ message: 'Press “Leave?” again within 5 seconds to disconnect the bot.', kind: 'ok' })
          }
        />
        <span className="session-badge" role="status">
          <CircleCheck size={14} className="state-icon--ok" aria-hidden="true" />
          <span>{session.guild}</span>
          {' — '}
          <span>{session.user}</span>
          {' · '}
          <ExpiryTimer expiresIn={session.expires_in} />
        </span>
      </div>
    </header>
  )
}
