import { useEffect, useState } from 'react'
import { ArrowUp } from 'lucide-react'

/** Back-to-top affordance for the long page (legacy behavior). */
export function ScrollTop() {
  const [visible, setVisible] = useState(false)

  useEffect(() => {
    const onScroll = () => setVisible(window.scrollY >= 400)
    onScroll()
    window.addEventListener('scroll', onScroll, { passive: true })
    return () => window.removeEventListener('scroll', onScroll)
  }, [])

  return (
    <button
      type="button"
      className="footbtn"
      aria-label="Back to top"
      title="Back to top"
      hidden={!visible}
      onClick={() => {
        const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches
        window.scrollTo({ top: 0, behavior: reduced ? 'auto' : 'smooth' })
      }}
    >
      <ArrowUp size={18} aria-hidden="true" />
    </button>
  )
}
