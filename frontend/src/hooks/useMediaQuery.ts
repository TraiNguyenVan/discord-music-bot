import { useEffect, useState } from 'react'

/**
 * Media query as state. Environments without `matchMedia` (jsdom, very old
 * webviews) always answer "no" — callers must treat false as the safe
 * compact layout, which is also what narrow windows get.
 */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() => {
    if (typeof window.matchMedia !== 'function') return false
    try {
      return window.matchMedia(query).matches
    } catch {
      return false
    }
  })

  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return
    let m: MediaQueryList
    try {
      m = window.matchMedia(query)
    } catch {
      return
    }
    const onChange = () => setMatches(m.matches)
    onChange()
    m.addEventListener('change', onChange)
    return () => m.removeEventListener('change', onChange)
  }, [query])

  return matches
}

/** prefers-reduced-motion as a hook (falls back to false without matchMedia). */
export function useReducedMotion(): boolean {
  return useMediaQuery('(prefers-reduced-motion: reduce)')
}
