import type { NowState } from '../types/music'
import { useTwoTap } from '../hooks/useTwoTap'
import { noEmoji } from '../lib/format'

interface VoicePillProps {
  now: NowState | null
  pending: boolean
  onConnect: () => void
  onLeave: () => void
  onArm: () => void
}

/**
 * Voice status pill: "Connect bot" joins directly; Leave hides behind the
 * same two-tap confirm Clear uses — a stray tap never drops the bot. The
 * `connected` / `voice_channel` facts come only from the backend snapshot.
 */
export function VoicePill({ now, pending, onConnect, onLeave, onArm }: VoicePillProps) {
  const { armed, click } = useTwoTap(onLeave, onArm)
  const connected = !!now?.connected
  const label = armed
    ? 'Leave?'
    : connected
      ? `In ${noEmoji(now?.voice_channel || 'voice')}`
      : 'Connect bot'

  return (
    <button
      type="button"
      className={armed ? 'pill pill--armed' : 'pill'}
      disabled={pending}
      onClick={() => {
        if (!connected) {
          onConnect()
          return
        }
        click()
      }}
    >
      {label}
    </button>
  )
}
