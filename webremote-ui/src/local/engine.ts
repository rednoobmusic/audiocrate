/** Local playback engine — the phone plays the music itself.
 *
 * Used when the desktop API isn't reachable (e.g. on rednoobmusic.com/audiocrate
 * with this machine powered off). Audio comes from the static library built by
 * tools/build_web_library.py: one manifest plus opus files.
 *
 * The graph is MediaElementSource → [EQ chain] → gain → analyser → destination,
 * so the DSP and VIZ tabs have something real to hook into rather than a remote
 * control for the desktop's chain.
 *
 * It deliberately exposes the same shape as the server's /api/state snapshot, so
 * every page renders from one contract regardless of which mode is active.
 */
import type { PlayerState, QueueView } from '../api'

export interface ManifestTrack {
  id: string
  title: string
  artist: string
  album: string
  dur: number
  size: number
  src: string
  art: boolean
}

export interface ManifestPlaylist {
  name: string
  /** `id` is empty-resolving when the track isn't in this build; `name` is the
   *  original filename stem so it can still be listed as unavailable. */
  items: { id: string; name: string }[]
}

export interface Manifest {
  version: number
  bitrate: string
  count: number
  tracks: ManifestTrack[]
  playlists?: ManifestPlaylist[]
}

const MODE_NAMES = ['SEQ', 'REP', 'RP1']
const STATE_KEY = 'audiocrate:local-state'

/** Enhance chain, mirroring core/dsp_chain.py's Enhancer EQ stage.
 *  Same frequencies and gains the desktop uses, so the phone sounds like
 *  AudioCrate rather than a generic "loudness" curve. */
const ENHANCE_EQ: { type: BiquadFilterType; freq: number; gain: number; q: number }[] = [
  { type: 'lowshelf',  freq: 90,    gain: 2.5, q: 0.707 },  // bass body, not sub
  { type: 'peaking',   freq: 250,   gain: 1.2, q: 0.9 },    // low-mid warmth
  { type: 'peaking',   freq: 4200,  gain: 1.8, q: 1.1 },    // presence
  { type: 'peaking',   freq: 6000,  gain: 1.5, q: 1.2 },    // bite
  { type: 'highshelf', freq: 9500,  gain: 4.5, q: 0.707 },  // air
  { type: 'highshelf', freq: 14000, gain: 3.0, q: 0.707 },  // crystal
]
// The shelves add level; trim so Enhance changes tone, not loudness.
const ENHANCE_TRIM_DB = -4.5

export class LocalEngine {
  readonly base: string
  manifest: Manifest | null = null

  private audio: HTMLAudioElement
  private ctx: AudioContext | null = null
  private srcNode: MediaElementAudioSourceNode | null = null
  private gain: GainNode | null = null
  private analyser: AnalyserNode | null = null

  private queue: ManifestTrack[] = []
  private index = -1
  private shuffleOn = false
  private mode = 0
  private order: number[] = []
  private listeners = new Set<() => void>()
  private saveTimer: number | undefined
  private enhanceOn = false
  private eqNodes: BiquadFilterNode[] = []
  private makeup: GainNode | null = null

  constructor(base: string) {
    this.base = base.replace(/\/$/, '')
    this.audio = new Audio()
    this.audio.preload = 'metadata'
    this.audio.crossOrigin = 'anonymous'

    this.audio.addEventListener('ended', () => this.next(true))
    this.audio.addEventListener('timeupdate', () => this.emit())
    this.audio.addEventListener('loadedmetadata', () => this.emit())
    this.audio.addEventListener('play', () => this.emit())
    this.audio.addEventListener('pause', () => this.emit())
    this.audio.addEventListener('error', () => {
      // A single unplayable file shouldn't strand the queue.
      console.warn('local playback error', this.current?.id)
      this.next(true)
    })
  }

  // ── setup ───────────────────────────────────────────────────────────────

  async load(): Promise<Manifest> {
    const res = await fetch(`${this.base}/media/manifest.json`, { cache: 'no-cache' })
    if (!res.ok) throw new Error(`manifest ${res.status}`)
    this.manifest = await res.json()
    this.restore()
    return this.manifest!
  }

  /** Build the Web Audio graph. Must happen after a user gesture on iOS, so it's
   *  called lazily from the first play() rather than at construction. */
  private ensureGraph() {
    if (this.ctx) return
    const Ctor = window.AudioContext || (window as any).webkitAudioContext
    if (!Ctor) return
    this.ctx = new Ctor()
    this.srcNode = this.ctx.createMediaElementSource(this.audio)
    this.gain = this.ctx.createGain()
    this.analyser = this.ctx.createAnalyser()
    this.analyser.fftSize = 2048
    this.analyser.smoothingTimeConstant = 0

    // Build the EQ once and leave it in the path; bypass is done by flattening
    // the gains, which avoids reconnect clicks when toggling mid-playback.
    this.eqNodes = ENHANCE_EQ.map(spec => {
      const f = this.ctx!.createBiquadFilter()
      f.type = spec.type
      f.frequency.value = spec.freq
      f.Q.value = spec.q
      f.gain.value = 0
      return f
    })
    this.makeup = this.ctx.createGain()
    this.makeup.gain.value = 1

    let node: AudioNode = this.srcNode
    for (const f of this.eqNodes) { node.connect(f); node = f }
    node.connect(this.makeup)
    this.makeup.connect(this.gain)
    this.gain.connect(this.analyser)
    this.analyser.connect(this.ctx.destination)

    this.applyEnhance()
  }

  private applyEnhance() {
    if (!this.ctx || !this.makeup) return
    const t = this.ctx.currentTime
    this.eqNodes.forEach((f, i) => {
      // Ramp rather than jump, so toggling doesn't click.
      f.gain.setTargetAtTime(this.enhanceOn ? ENHANCE_EQ[i].gain : 0, t, 0.02)
    })
    const lin = this.enhanceOn ? Math.pow(10, ENHANCE_TRIM_DB / 20) : 1
    this.makeup.gain.setTargetAtTime(lin, t, 0.02)
  }

  get enhance(): boolean { return this.enhanceOn }

  setEnhance(on: boolean) {
    this.enhanceOn = on
    this.ensureGraph()
    this.applyEnhance()
    this.emit()
  }

  /** Exposed for the VIZ tab. Null until playback has started once. */
  get analyserNode(): AnalyserNode | null { return this.analyser }
  get audioContext(): AudioContext | null { return this.ctx }
  /** Insert point for the DSP tab's EQ chain: source → <here> → gain. */
  get sourceNode(): MediaElementAudioSourceNode | null { return this.srcNode }
  get gainNode(): GainNode | null { return this.gain }

  subscribe(fn: () => void): () => void {
    this.listeners.add(fn)
    return () => this.listeners.delete(fn)
  }

  private emit() {
    this.listeners.forEach(fn => fn())
    this.scheduleSave()
  }

  // ── persistence ─────────────────────────────────────────────────────────

  /** Queue, play order, toggles and position survive a reload. Debounced
   *  because `timeupdate` fires several times a second. */
  private scheduleSave() {
    if (this.saveTimer !== undefined) return
    this.saveTimer = window.setTimeout(() => {
      this.saveTimer = undefined
      try {
        localStorage.setItem(STATE_KEY, JSON.stringify({
          v: 1,
          ids: this.queue.map(t => t.id),
          index: this.index,
          pos: this.audio.currentTime || 0,
          shuffle: this.shuffleOn,
          mode: this.mode,
          volume: this.audio.volume,
          enhance: this.enhanceOn,
        }))
      } catch { /* private mode / quota — not worth surfacing */ }
    }, 600)
  }

  /** Rebuild the session from localStorage. Restores paused at the saved
   *  position: browsers block autoplay without a gesture, and resuming audio
   *  unprompted on page load would be obnoxious anyway. */
  private restore() {
    let saved: any
    try {
      saved = JSON.parse(localStorage.getItem(STATE_KEY) || 'null')
    } catch { return }
    if (!saved || saved.v !== 1 || !Array.isArray(saved.ids)) return

    const byId = new Map((this.manifest?.tracks ?? []).map(t => [t.id, t]))
    this.queue = saved.ids.map((id: string) => byId.get(id)).filter(Boolean) as ManifestTrack[]
    this.shuffleOn = Boolean(saved.shuffle)
    this.mode = Number(saved.mode) || 0
    this.enhanceOn = Boolean(saved.enhance)
    if (typeof saved.volume === 'number') this.audio.volume = saved.volume

    this.index = Number.isInteger(saved.index) && saved.index >= 0 && saved.index < this.queue.length
      ? saved.index : (this.queue.length ? 0 : -1)
    if (this.shuffleOn) this.rebuildOrder()

    const t = this.queue[this.index]
    if (t) {
      this.audio.src = this.audioUrl(t)
      const pos = Number(saved.pos) || 0
      if (pos > 0) {
        const seek = () => {
          try { this.audio.currentTime = pos } catch { /* not seekable yet */ }
          this.audio.removeEventListener('loadedmetadata', seek)
          this.emit()
        }
        this.audio.addEventListener('loadedmetadata', seek)
      }
    }
  }

  // ── urls ────────────────────────────────────────────────────────────────

  audioUrl(t: ManifestTrack): string { return `${this.base}/media/audio/${t.id}.opus` }
  artUrl(t: ManifestTrack, size: 256 | 640 = 256): string {
    return t.art ? `${this.base}/media/art/${t.id}-${size}.jpg` : ''
  }

  get current(): ManifestTrack | undefined { return this.queue[this.index] }

  // ── state, shaped like the server snapshot ──────────────────────────────

  snapshot(): PlayerState {
    const t = this.current
    return {
      playing:   !this.audio.paused && !this.audio.ended,
      has_track: Boolean(t),
      title:     t?.title ?? '',
      artist:    t?.artist ?? '',
      album:     t?.album ?? '',
      path:      t ? this.audioUrl(t) : '',
      ext:       t?.src ?? '',
      index:     this.index,
      pos_ms:    Math.round((this.audio.currentTime || 0) * 1000),
      // Prefer the decoded duration; fall back to the manifest while loading.
      dur_ms:    Number.isFinite(this.audio.duration) && this.audio.duration > 0
                   ? Math.round(this.audio.duration * 1000)
                   : (t?.dur ?? 0),
      volume:    this.audio.volume,
      shuffle:   this.shuffleOn,
      mode:      this.mode,
      mode_name: MODE_NAMES[this.mode] ?? 'SEQ',
      queue_len: this.queue.length,
      enhance:   this.enhanceOn,
      sleep:     false,
      subsongs:  0,
      subsong:   0,
      art:       t?.art ? t.id : '',
      error:     '',
      ts:        Date.now(),
    }
  }

  queueView(): QueueView {
    return {
      items: this.queue.map((t, i) => ({
        i,
        path:   this.audioUrl(t),
        title:  t.title,
        artist: t.artist,
        album:  t.album,
        art:    t.art ? t.id : '',
        ext:    t.src,
      })),
      index: this.index,
      upcoming: this.upcoming(),
    }
  }

  private upcoming(limit = 200): number[] {
    const n = this.queue.length
    if (!n || this.index < 0) return []
    if (this.mode === 2) return [this.index]
    let rest: number[]
    if (this.shuffleOn) {
      const pos = this.order.indexOf(this.index)
      if (pos < 0) return []
      rest = this.order.slice(pos + 1)
      if (this.mode === 1) rest = rest.concat(this.order.slice(0, pos))
    } else {
      rest = Array.from({ length: n - this.index - 1 }, (_, k) => this.index + 1 + k)
      if (this.mode === 1) rest = rest.concat(Array.from({ length: this.index }, (_, k) => k))
    }
    return rest.slice(0, limit)
  }

  private rebuildOrder() {
    this.order = this.queue.map((_, i) => i)
    for (let i = this.order.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1))
      ;[this.order[i], this.order[j]] = [this.order[j], this.order[i]]
    }
    // Keep whatever is playing at the head so it isn't skipped mid-track.
    if (this.index >= 0) {
      const at = this.order.indexOf(this.index)
      if (at > 0) this.order.splice(0, 0, ...this.order.splice(at, 1))
    }
  }

  // ── transport ───────────────────────────────────────────────────────────

  async play() {
    this.ensureGraph()
    if (this.ctx?.state === 'suspended') await this.ctx.resume()
    if (this.index < 0 && this.queue.length) return this.playIndex(0)
    try { await this.audio.play() } catch { /* autoplay blocked until a gesture */ }
    this.emit()
  }

  pause() { this.audio.pause(); this.emit() }
  toggle() { this.audio.paused ? this.play() : this.pause() }
  stop() { this.audio.pause(); this.audio.currentTime = 0; this.emit() }

  async playIndex(i: number) {
    if (i < 0 || i >= this.queue.length) return
    this.index = i
    const t = this.queue[i]
    this.audio.src = this.audioUrl(t)
    this.audio.load()
    if (this.shuffleOn && !this.order.length) this.rebuildOrder()
    await this.play()
    this.emit()
  }

  next(auto = false) {
    if (!this.queue.length) return
    // Auto-advance honours repeat-one; an explicit tap always moves on.
    if (auto && this.mode === 2) { this.audio.currentTime = 0; return void this.play() }
    const up = this.upcoming(1)
    if (up.length) return void this.playIndex(up[0])
    if (auto) { this.audio.pause(); this.emit() }        // end of queue
    else this.playIndex(0)
  }

  prev() {
    if (!this.queue.length) return
    // Restart the track first, like every other player.
    if (this.audio.currentTime > 3) { this.audio.currentTime = 0; return this.emit() }
    const i = this.shuffleOn
      ? this.order[Math.max(0, this.order.indexOf(this.index) - 1)]
      : Math.max(0, this.index - 1)
    this.playIndex(i)
  }

  seekFrac(frac: number) {
    const d = this.audio.duration
    if (!Number.isFinite(d) || d <= 0) return
    this.audio.currentTime = Math.max(0, Math.min(d, frac * d))
    this.emit()
  }

  setVolume(v: number) { this.audio.volume = Math.max(0, Math.min(1, v)); this.emit() }

  setShuffle(on: boolean) {
    this.shuffleOn = on
    if (on) this.rebuildOrder()
    this.emit()
  }

  setMode(m: number) { this.mode = ((m % 3) + 3) % 3; this.emit() }

  // ── queue ───────────────────────────────────────────────────────────────

  /** Unique by id, matching the desktop player's unique-by-path queue.
   *  Returns how many were actually added. */
  enqueue(tracks: ManifestTrack[]): number {
    const have = new Set(this.queue.map(t => t.id))
    const add = tracks.filter(t => !have.has(t.id))
    this.queue.push(...add)
    if (this.shuffleOn) this.rebuildOrder()
    this.emit()
    return add.length
  }

  enqueuePlay(tracks: ManifestTrack[]) {
    this.enqueue(tracks)
    const first = tracks[0]
    if (!first) return
    const at = this.queue.findIndex(t => t.id === first.id)
    if (at >= 0) this.playIndex(at)
  }

  remove(i: number) {
    if (i < 0 || i >= this.queue.length) return
    this.queue.splice(i, 1)
    if (i < this.index) this.index--
    else if (i === this.index) {
      this.index = Math.min(this.index, this.queue.length - 1)
      if (this.index >= 0) this.playIndex(this.index)
      else this.stop()
    }
    if (this.shuffleOn) this.rebuildOrder()
    this.emit()
  }

  clear() {
    this.queue = []
    this.index = -1
    this.order = []
    this.audio.pause()
    this.audio.removeAttribute('src')
    this.emit()
  }
}
