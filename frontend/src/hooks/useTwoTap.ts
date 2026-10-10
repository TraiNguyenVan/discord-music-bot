import { useCallback, useEffect, useRef, useState } from 'react'

/**
 * Legacy two-tap armed confirm: the first tap arms the control, the second
 * tap within the window fires it, and an armed control disarms itself when
 * the window lapses. Used by Clear and by voice Leave so a stray tap never
 * destroys queue state or drops the bot.
 */
export function useTwoTap(onConfirm: () => void, onArm?: () => void, seconds = 5) {
  const [armed, setArmed] = useState(false)
  const armedRef = useRef(false)
  const timerRef = useRef<number | null>(null)
  const confirmRef = useRef(onConfirm)
  const armRef = useRef(onArm)

  useEffect(() => {
    confirmRef.current = onConfirm
    armRef.current = onArm
  })

  // An armed control never outlives its component.
  useEffect(
    () => () => {
      if (timerRef.current !== null) window.clearTimeout(timerRef.current)
    },
    [],
  )

  const click = useCallback(() => {
    if (!armedRef.current) {
      armedRef.current = true
      setArmed(true)
      armRef.current?.()
      timerRef.current = window.setTimeout(() => {
        armedRef.current = false
        setArmed(false)
        timerRef.current = null
      }, seconds * 1000)
      return
    }
    if (timerRef.current !== null) window.clearTimeout(timerRef.current)
    timerRef.current = null
    armedRef.current = false
    setArmed(false)
    confirmRef.current()
  }, [seconds])

  const disarm = useCallback(() => {
    if (timerRef.current !== null) window.clearTimeout(timerRef.current)
    timerRef.current = null
    armedRef.current = false
    setArmed(false)
  }, [])

  return { armed, click, disarm }
}
