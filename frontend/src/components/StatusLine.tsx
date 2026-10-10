import { LoaderCircle } from 'lucide-react'
import type { StatusUpdate } from '../hooks/useMusicSession'

/**
 * Reusable inline feedback line. Request outcomes (accepted / rejected) land
 * here; per-control pending feedback lives on the controls themselves.
 */
export function StatusLine({ status }: { status: StatusUpdate | null }) {
  return (
    <p
      className={status?.kind === 'err' ? 'readout readout--err' : 'readout'}
      role="status"
      aria-live="polite"
      aria-atomic="true"
    >
      {status?.kind === 'busy' && <LoaderCircle size={13} className="spin" aria-hidden="true" />}{' '}
      {status ? status.message : 'Warming up…'}
    </p>
  )
}
