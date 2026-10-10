/** Shared display helpers mirrored from the legacy picker (web/pick.html). */

// No emoji ever reaches the interface — not ours, not the bot's, not YouTube's.
const EMOJI_RE = /[\p{Extended_Pictographic}\u{FE0F}\u{200D}]/gu

export function noEmoji(s: string | null | undefined): string {
  return String(s ?? '')
    .replace(EMOJI_RE, '')
    .replace(/\s{2,}/g, ' ')
    .trim()
}

/** Server messages carry markdown bold + emoji; strip both before display. */
export function cleanMsg(s: string | null | undefined): string {
  return noEmoji(String(s ?? '').replace(/\*\*/g, ''))
}

/**
 * Format seconds as h:mm:ss / m:ss. Unlike the legacy helper, 0 renders as
 * "0:00" — callers decide when a zero duration means "LIVE" instead.
 */
export function fmtDur(s: number | null | undefined): string {
  const total = Math.round(s || 0)
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const sec = total % 60
  return (h ? `${h}:${String(m).padStart(2, '0')}` : `${m}`) + ':' + String(sec).padStart(2, '0')
}
