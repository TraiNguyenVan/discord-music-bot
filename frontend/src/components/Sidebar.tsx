import { ListMusic, Music2, Search } from 'lucide-react'
import type { MouseEvent } from 'react'
import type { NowState } from '../types/music'

interface SidebarProps {
  now: NowState | null
}

/** Smooth-scroll to an in-page section, honoring reduced motion. */
function jumpTo(id: string) {
  return (event: MouseEvent<HTMLAnchorElement>) => {
    event.preventDefault()
    const target = document.getElementById(id)
    if (!target) return
    const reduced = typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches
    target.scrollIntoView({ behavior: reduced ? 'auto' : 'smooth', block: 'start' })
  }
}

/**
 * The desktop sidebar: brand mark, section nav, and the room status lamp.
 * Hidden below 1000px — the topbar carries identity there.
 */
export function Sidebar({ now }: SidebarProps) {
  const connected = !!now?.connected
  const lampClass = 'lamp' + (connected ? (now?.playing ? ' lamp--live' : ' lamp--on') : '')

  return (
    <aside className="sidebar">
      <div className="brand">
        <span className="brand-mark" aria-hidden="true">
          <Music2 size={16} />
        </span>
        <span className="brand-name">Music Picker</span>
        <span className={lampClass} aria-hidden="true" title={connected ? 'Bot connected' : 'Bot not in voice'} />
      </div>

      <nav className="snav" aria-label="Sections">
        <a href="#find" onClick={jumpTo('find')}>
          <Search size={20} aria-hidden="true" />
          Search
        </a>
        <a href="#queue" onClick={jumpTo('queue')}>
          <ListMusic size={20} aria-hidden="true" />
          Queue
        </a>
      </nav>

      <p className="snote">Queue music for the room. Previews stay on this device, muted, until you unmute them.</p>
    </aside>
  )
}
