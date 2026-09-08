import { useCallback, useEffect, useRef, useState } from 'react'
import { usePlayer } from '../store'

/** Waveform seek bar — the desktop's WaveformBar, shrunk for a thumb.
 *
 * Peaks come from the server (same RMS envelope the desktop draws) and are
 * cached there per file, so switching back to a track is instant. Formats
 * ffmpeg can't decode return 404; `onUnavailable` lets Now Playing drop back to
 * the plain slider instead of showing an empty box.
 */
export function WaveSeek({ path, artKey, position, duration, onSeek, onUnavailable }: {
  path: string
  artKey: string
  position: number
  duration: number
  onSeek: (ms: number) => void
  onUnavailable?: () => void
}) {
  const [peaks, setPeaks] = useState<number[] | null>(null)
  const [drag, setDrag] = useState<number | null>(null)
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const boxRef = useRef<HTMLDivElement>(null)
  const { lib } = usePlayer()

  useEffect(() => {
    if (!path) return
    let cancelled = false
    setPeaks(null)
    lib.waveform(path, artKey)
      .then(r => { if (!cancelled) setPeaks(r.peaks) })
      .catch(() => { if (!cancelled) onUnavailable?.() })
    return () => { cancelled = true }
    // onUnavailable is intentionally excluded — it changes identity every
    // render and would restart the fetch in a loop.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, artKey])

  const draw = useCallback(() => {
    const canvas = canvasRef.current
    const box = boxRef.current
    if (!canvas || !box || !peaks) return

    const dpr = window.devicePixelRatio || 1
    const w = box.clientWidth
    const h = box.clientHeight
    if (canvas.width !== w * dpr || canvas.height !== h * dpr) {
      canvas.width = w * dpr
      canvas.height = h * dpr
    }
    const ctx = canvas.getContext('2d')
    if (!ctx) return
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    ctx.clearRect(0, 0, w, h)

    const css = getComputedStyle(document.documentElement)
    const played = css.getPropertyValue('--ac-fg').trim() || '#fff'
    const ahead = css.getPropertyValue('--ac-fg-dimmer').trim() || '#666'

    const shown = drag ?? position
    const frac = duration > 0 ? Math.max(0, Math.min(1, shown / duration)) : 0

    // One bar per ~3px so the shape survives on a narrow screen.
    const barW = 3
    const gap = 1
    const slots = Math.max(1, Math.floor(w / barW))
    const mid = h / 2

    for (let i = 0; i < slots; i++) {
      const v = peaks[Math.floor((i / slots) * peaks.length)] ?? 0
      const amp = Math.max(1.5, (v / 100) * (h / 2))
      const x = i * barW
      ctx.fillStyle = (i / slots) <= frac ? played : ahead
      ctx.fillRect(x, mid - amp, barW - gap, amp * 2)
    }
  }, [peaks, position, duration, drag])

  useEffect(() => { draw() }, [draw])

  useEffect(() => {
    const onResize = () => draw()
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [draw])

  const fracFromEvent = (clientX: number): number => {
    const box = boxRef.current
    if (!box) return 0
    const r = box.getBoundingClientRect()
    return Math.max(0, Math.min(1, (clientX - r.left) / r.width))
  }

  if (!peaks) {
    return <div className="ac-wave" ref={boxRef}><div className="ac-wave-loading" /></div>
  }

  return (
    <div
      className="ac-wave"
      ref={boxRef}
      onPointerDown={e => {
        (e.target as HTMLElement).setPointerCapture(e.pointerId)
        setDrag(fracFromEvent(e.clientX) * duration)
      }}
      onPointerMove={e => { if (drag !== null) setDrag(fracFromEvent(e.clientX) * duration) }}
      onPointerUp={e => {
        const ms = fracFromEvent(e.clientX) * duration
        setDrag(null)
        if (duration > 0) onSeek(ms)
      }}
      onPointerCancel={() => setDrag(null)}
    >
      <canvas ref={canvasRef} className="ac-wave-canvas" />
    </div>
  )
}
