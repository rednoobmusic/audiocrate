/** Player state shared across every page.
 *
 * The server snapshot refreshes ~2.5×/s, which is plenty for metadata but
 * choppy for a seek bar — so position is interpolated locally between polls and
 * resynced on each one. Polling stops when the tab is hidden (backgrounding the
 * PWA shouldn't burn battery or cellular data).
 */
import {
  createContext, useCallback, useContext, useEffect, useMemo, useRef, useState,
  type ReactNode,
} from 'react'
import { api, AUTH_LOST, type Action, type PlayerState, type QueueView } from './api'
import { LocalEngine } from './local/engine'
import { localSource, type DataSource } from './local/source'
import { remoteSource } from './local/remoteSource'

export type Mode = 'remote' | 'local'

const POLL_MS = 900
const TICK_MS = 250

/** Fire-and-forget UI confirmation; App listens and shows an IonToast. */
export const TOAST = 'audiocrate:toast'

interface Ctx {
  state: PlayerState | null
  queue: QueueView
  /** Interpolated playback position — use this for the seek bar, not state.pos_ms. */
  position: number
  cmd: (action: Action, value?: unknown) => Promise<void>
  seekTo: (ms: number) => void
  refresh: () => void
  refreshQueue: () => void
  /** Which engine is driving playback. */
  mode: Mode
  /** Library/art access for the active mode. */
  lib: DataSource
  /** The local engine, when mode === 'local' — the DSP/VIZ tabs need its nodes. */
  engine: LocalEngine | null
}

const PlayerContext = createContext<Ctx | null>(null)

export function usePlayer(): Ctx {
  const ctx = useContext(PlayerContext)
  if (!ctx) throw new Error('usePlayer outside PlayerProvider')
  return ctx
}

export function PlayerProvider({ mode, engine, children }: {
  mode: Mode
  engine: LocalEngine | null
  children: ReactNode
}) {
  const isLocal = mode === 'local' && engine !== null
  const lib = useMemo(
    () => (isLocal ? localSource(engine!) : remoteSource),
    [isLocal, engine],
  )

  const [state, setState] = useState<PlayerState | null>(null)
  const [queue, setQueue] = useState<QueueView>({ items: [], index: -1, upcoming: [] })
  const [position, setPosition] = useState(0)

  // Local clock: last known server position + when we learned it.
  const anchor = useRef({ pos: 0, at: Date.now(), playing: false })
  // Set while a drag/seek is in flight so a stale poll can't yank the thumb back.
  const seekGuard = useRef(0)
  const queueSig = useRef('')
  // Which track the local clock is anchored to, so we can spot track changes.
  const lastTrack = useRef('')
  // A track switch takes the player ~300ms to land. Until it does, /api/state
  // still describes the OLD track — anchoring to that snapshot is what made the
  // bar jump to the previous track's position after pressing next.
  const pendingFrom = useRef<string | null>(null)
  const pendingUntil = useRef(0)
  const switchAt = useRef(0)
  // Duration, mirrored into a ref so the interval closure sees current values.
  const durRef = useRef(0)
  const queueLen = useRef(0)

  const refresh = useCallback(async () => {
    // Local mode reads straight from the engine — no polling, no network.
    if (isLocal) {
      const s = engine!.snapshot()
      setState(s)
      durRef.current = s.dur_ms
      queueLen.current = s.queue_len
      setPosition(s.pos_ms)
      anchor.current = { pos: s.pos_ms, at: Date.now(), playing: s.playing }
      setQueue(engine!.queueView())
      return
    }
    try {
      const s = await api.state()
      setState(s)
      durRef.current = s.dur_ms
      queueLen.current = s.queue_len

      // Still the track we just skipped away from → the switch hasn't landed.
      // Hold the local clock at 0 rather than adopting its position.
      const stale = pendingFrom.current !== null
        && s.path === pendingFrom.current
        && Date.now() < pendingUntil.current
      if (stale) return

      // The switch landed (or timed out): drop the pending state and any seek
      // guard, since a new track invalidates an in-flight seek.
      const settled = pendingFrom.current !== null
      if (settled) {
        // The engine briefly reports the previous track's cursor against the
        // new track's metadata. A track that just started cannot be further in
        // than the time since we asked for it, so treat that as stale and wait
        // for the next poll rather than jumping into the middle of the song.
        const since = Date.now() - switchAt.current
        if (Date.now() < pendingUntil.current && s.pos_ms > since + 2500) return
        pendingFrom.current = null
        seekGuard.current = 0
      }

      const changed = s.path !== lastTrack.current
      if (changed) {
        lastTrack.current = s.path
        seekGuard.current = 0
      }
      if (settled || changed || Date.now() > seekGuard.current) {
        anchor.current = { pos: s.pos_ms, at: Date.now(), playing: s.playing }
        setPosition(s.pos_ms)
      }
      // Only refetch the queue when it actually changed.
      const sig = `${s.queue_len}:${s.index}:${s.shuffle}:${s.mode}`
      if (sig !== queueSig.current) {
        queueSig.current = sig
        api.queue().then(setQueue).catch(() => {})
      }
    } catch {
      /* auth loss is broadcast by the client; transient errors just retry */
    }
  }, [isLocal, engine])

  const refreshQueue = useCallback(() => {
    if (isLocal) return setQueue(engine!.queueView())
    api.queue().then(setQueue).catch(() => {})
  }, [isLocal, engine])

  // In local mode the engine pushes changes instead of us polling for them.
  useEffect(() => {
    if (!isLocal) return
    return engine!.subscribe(refresh)
  }, [isLocal, engine, refresh])

  // Poll loop — paused while the page is hidden. Local mode is event-driven.
  useEffect(() => {
    if (isLocal) { refresh(); return }
    let timer: number | undefined
    const start = () => {
      stop()
      refresh()
      timer = window.setInterval(refresh, POLL_MS)
    }
    const stop = () => {
      if (timer !== undefined) window.clearInterval(timer)
      timer = undefined
    }
    const onVis = () => (document.hidden ? stop() : start())
    start()
    document.addEventListener('visibilitychange', onVis)
    return () => {
      stop()
      document.removeEventListener('visibilitychange', onVis)
    }
  }, [refresh, isLocal])

  // Interpolate between polls so the seek bar moves smoothly.
  useEffect(() => {
    const t = window.setInterval(() => {
      const a = anchor.current
      if (!a.playing || Date.now() < seekGuard.current) return
      // Don't run past the end of the track while waiting for the next poll.
      const dur = durRef.current
      const next = a.pos + (Date.now() - a.at)
      setPosition(dur > 0 ? Math.min(next, dur) : next)
    }, TICK_MS)
    return () => window.clearInterval(t)
  }, [])

  const cmd = useCallback(async (action: Action, value?: unknown) => {
    // Optimistic UI: the button should respond now, not in 900ms.
    setState(prev => {
      if (!prev) return prev
      switch (action) {
        case 'toggle': return { ...prev, playing: !prev.playing }
        case 'play':   return { ...prev, playing: true }
        case 'pause':
        case 'stop':   return { ...prev, playing: false }
        case 'shuffle': return { ...prev, shuffle: Boolean(value) }
        case 'mode':   return { ...prev, mode: Number(value) }
        case 'volume': return { ...prev, volume: Number(value) }
        case 'enhance': return { ...prev, enhance: Boolean(value) }
        case 'sleep':  return { ...prev, sleep: Boolean(value) }
        default: return prev
      }
    })
    if (action === 'toggle' || action === 'play' || action === 'pause') {
      anchor.current = {
        pos: position,
        at: Date.now(),
        playing: action === 'pause' ? false : action === 'play' ? true : !anchor.current.playing,
      }
    }

    // Anything that starts a different track rewinds the local clock to zero
    // right away. Without this the bar keeps counting up from the previous
    // track's position until a poll lands, then jumps backwards.
    const TRACK_CHANGING = ['next', 'prev', 'play_index', 'subsong', 'enqueue_play']

    // Local mode drives the engine directly: no request, no stale-snapshot
    // dance, so none of the pending/anchor bookkeeping below applies.
    if (isLocal) {
      const e = engine!
      const byId = (ids: unknown) => {
        const list = Array.isArray(ids) ? ids.map(String) : [String(ids)]
        const all = e.manifest?.tracks ?? []
        const index = new Map(all.map(t => [t.id, t]))
        return list.map(id => index.get(id)).filter(Boolean) as NonNullable<ReturnType<typeof index.get>>[]
      }
      switch (action) {
        case 'play':       await e.play(); break
        case 'pause':      e.pause(); break
        case 'toggle':     e.toggle(); break
        case 'stop':       e.stop(); break
        case 'next':       e.next(); break
        case 'prev':       e.prev(); break
        case 'seek':       e.seekFrac(Number(value)); break
        case 'volume':     e.setVolume(Number(value)); break
        case 'shuffle':    e.setShuffle(Boolean(value)); break
        case 'mode':       e.setMode(Number(value)); break
        case 'play_index': e.playIndex(Number(value)); break
        case 'enhance':    e.setEnhance(Boolean(value)); break
        case 'remove':     e.remove(Number(value)); break
        case 'clear':      e.clear(); break
        case 'enqueue': {
          const added = e.enqueue(byId(value))
          window.dispatchEvent(new CustomEvent(TOAST, {
            detail: added > 0
              ? `Added ${added} track${added === 1 ? '' : 's'} to queue`
              : 'Already in the queue',
          }))
          break
        }
        case 'enqueue_play': e.enqueuePlay(byId(value)); break
        default: break
      }
      return
    }

    if (TRACK_CHANGING.includes(action)) {
      seekGuard.current = 0
      lastTrack.current = ''
      pendingFrom.current = state?.path ?? ''
      pendingUntil.current = Date.now() + 5000   // chiptune renders can be slow
      switchAt.current = Date.now()
      anchor.current = { pos: 0, at: Date.now(), playing: true }
      setPosition(0)
    }

    try {
      const before = queueLen.current
      await api.cmd(action, value)
      // Enqueueing has no visible effect on the current screen, so without a
      // confirmation it reads as "the button did nothing". Report the real
      // delta rather than the number sent: the player's queue is unique by
      // path, so re-adding a playlist that's already queued legitimately adds
      // nothing, and claiming otherwise would be a lie.
      if (action === 'enqueue') {
        let added = 0
        for (let i = 0; i < 8; i++) {          // bulk adds take a moment to land
          await new Promise(r => setTimeout(r, 250))
          const s = await api.state().catch(() => null)
          if (!s) break
          queueLen.current = s.queue_len
          added = s.queue_len - before
          if (added > 0) break
        }
        window.dispatchEvent(new CustomEvent(TOAST, {
          detail: added > 0
            ? `Added ${added} track${added === 1 ? '' : 's'} to queue`
            : 'Already in the queue',
        }))
      }
    } finally {
      // Track-changing commands need a prompt resync; the rest ride the poll.
      // Two probes: the first catches a fast switch, the second covers chiptune
      // renders and other slow loads that aren't ready yet at 150ms.
      if (TRACK_CHANGING.includes(action) || ['remove', 'clear', 'enqueue'].includes(action)) {
        queueSig.current = ''
        window.setTimeout(refresh, 150)
        window.setTimeout(refresh, 700)
      }
    }
  }, [position, refresh, isLocal, engine, state?.path])

  const seekTo = useCallback((ms: number) => {
    const dur = state?.dur_ms ?? 0
    if (dur <= 0) return
    const clamped = Math.max(0, Math.min(dur, ms))
    setPosition(clamped)
    anchor.current = { pos: clamped, at: Date.now(), playing: anchor.current.playing }
    if (isLocal) return engine!.seekFrac(clamped / dur)
    seekGuard.current = Date.now() + 900        // ignore polls until the server catches up
    api.cmd('seek', clamped / dur).catch(() => {})
  }, [state?.dur_ms, isLocal, engine])

  // A session that expires mid-use should bounce to the PIN screen instantly.
  useEffect(() => {
    const onLost = () => setState(null)
    window.addEventListener(AUTH_LOST, onLost)
    return () => window.removeEventListener(AUTH_LOST, onLost)
  }, [])

  const value = useMemo(
    () => ({ state, queue, position, cmd, seekTo, refresh, refreshQueue, mode, lib, engine }),
    [state, queue, position, cmd, seekTo, refresh, refreshQueue, mode, lib, engine],
  )

  return <PlayerContext.Provider value={value}>{children}</PlayerContext.Provider>
}
