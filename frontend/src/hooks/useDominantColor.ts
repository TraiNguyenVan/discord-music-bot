import { useEffect, useState } from 'react'

/**
 * Average color of an image, darkened for the now-playing header tint —
 * the Spotify-style "the art colors the room around it" effect. Sampling
 * failures (CORS-tainted canvas, blocked loads) simply keep the null
 * fallback, and the hero falls back to the default green-tinted gradient.
 */
export function useDominantColor(url: string | null | undefined): string | null {
  const [color, setColor] = useState<string | null>(null)

  useEffect(() => {
    if (!url) {
      setColor(null)
      return
    }
    let alive = true
    const img = new Image()
    img.crossOrigin = 'anonymous'
    img.onload = () => {
      if (!alive) return
      try {
        const canvas = document.createElement('canvas')
        canvas.width = 16
        canvas.height = 16
        const ctx = canvas.getContext('2d')
        if (!ctx) return
        ctx.drawImage(img, 0, 0, 16, 16)
        const { data } = ctx.getImageData(0, 0, 16, 16)
        // Thumbnail letterbox bars are pure black and drag a plain average
        // down to near-black; average only the brighter half of pixels so
        // the tint carries the artwork's actual color.
        const px: Array<[number, number, number]> = []
        for (let i = 0; i < data.length; i += 4) {
          px.push([data[i], data[i + 1], data[i + 2]])
        }
        if (!px.length) return
        px.sort((a, b) => b[0] + b[1] + b[2] - (a[0] + a[1] + a[2]))
        const bright = px.slice(0, Math.max(1, Math.ceil(px.length * 0.5)))
        let r = 0
        let g = 0
        let b = 0
        for (const [pr, pg, pb] of bright) {
          r += pr
          g += pg
          b += pb
        }
        // Sink toward dark so white text on top keeps 4.5:1+ contrast.
        const sink = (v: number) => Math.round(Math.min(190, Math.max(12, v * 0.55 + 10)))
        setColor(`rgb(${sink(r / bright.length)}, ${sink(g / bright.length)}, ${sink(b / bright.length)})`)
      } catch {
        /* tainted canvas — keep the fallback tint */
      }
    }
    img.src = url
    return () => {
      alive = false
    }
  }, [url])

  return color
}
