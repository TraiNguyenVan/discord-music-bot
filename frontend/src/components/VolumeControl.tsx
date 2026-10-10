import { useEffect, useRef, useState } from 'react'
import type { CSSProperties } from 'react'
import { Minus, Plus } from 'lucide-react'

interface VolumeControlProps {
  /** Backend-confirmed level, 0–200. */
  volume: number
  pending: ReadonlySet<string>
  /** Posts volume_set; the returned promise resolves when the request ends. */
  onSet: (level: number) => Promise<void>
  onDelta: (delta: number) => void
}

/**
 * Bot volume: ±10 delta buttons and a 0–200 slider. Native `change` (release)
 * schedules the volume_set 150ms out, capturing the value first — a state
 * poll landing before the timeout must never cause a stale level to post.
 * While the pointer is on the slider the local value wins so the 2s poll
 * cannot snap the thumb back mid-drag.
 */
export function VolumeControl({ volume, pending, onSet, onDelta }: VolumeControlProps) {
  const [local, setLocal] = useState<number | null>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const timerRef = useRef<number | null>(null)
  const shown = local ?? (Number.isFinite(volume) ? volume : 50)

  useEffect(
    () => () => {
      if (timerRef.current !== null) window.clearTimeout(timerRef.current)
    },
    [],
  )

  useEffect(() => {
    const el = inputRef.current
    if (!el) return

    const onNativeInput = () => setLocal(Number(el.value) || 0)
    const onNativeChange = () => {
      if (timerRef.current !== null) window.clearTimeout(timerRef.current)
      // capture now, post later: see the comment above
      const level = Number(el.value) || 0
      timerRef.current = window.setTimeout(() => {
        timerRef.current = null
        void onSet(level).finally(() => {
          // only hand control back to the server when no newer edit is waiting
          if (timerRef.current === null) setLocal(null)
        })
      }, 150)
    }

    el.addEventListener('input', onNativeInput)
    el.addEventListener('change', onNativeChange)
    return () => {
      el.removeEventListener('input', onNativeInput)
      el.removeEventListener('change', onNativeChange)
    }
  }, [onSet])

  return (
    <div className="vgroup" role="group" aria-label="Bot volume">
      <button
        type="button"
        className="iconbtn"
        aria-label="Volume down by 10"
        title="Volume down"
        disabled={pending.has('volume_delta')}
        onClick={() => onDelta(-10)}
      >
        <Minus size={16} aria-hidden="true" />
      </button>
      <label className="sr-only" htmlFor="bot-volume">
        Bot volume
      </label>
      <input
        ref={inputRef}
        id="bot-volume"
        name="bot-volume"
        type="range"
        min={0}
        max={200}
        value={shown}
        autoComplete="off"
        disabled={pending.has('volume_set')}
        style={{ '--fill': `${shown / 2}%` } as CSSProperties}
      />
      <button
        type="button"
        className="iconbtn"
        aria-label="Volume up by 10"
        title="Volume up"
        disabled={pending.has('volume_delta')}
        onClick={() => onDelta(10)}
      >
        <Plus size={16} aria-hidden="true" />
      </button>
      <span className="pct tnum">{shown}%</span>
    </div>
  )
}
