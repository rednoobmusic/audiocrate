/** Typed client for the webremote.py JSON API.
 *
 * Auth is a cookie, so every request just needs `credentials: 'same-origin'`.
 * A 401 anywhere means the session expired — we broadcast it so the app can
 * drop back to the PIN screen without every caller handling it.
 */

export interface PlayerState {
  playing: boolean
  has_track: boolean
  title: string
  artist: string
  album: string
  path: string
  ext: string
  index: number
  pos_ms: number
  dur_ms: number
  volume: number
  shuffle: boolean
  mode: number
  mode_name: string
  queue_len: number
  enhance: boolean
  sleep: boolean
  subsongs: number
  subsong: number
  art: string
  error: string
  ts: number
}

export interface QueueItem {
  i: number
  path: string
  title: string
  artist: string
  album: string
  art: string
  ext: string
}

export interface QueueView {
  items: QueueItem[]
  index: number
  upcoming: number[]
}

export interface LibraryTrack {
  /** Server mode: filesystem path. Local mode: manifest id. Either way it's the
   *  handle the current engine accepts for enqueueing. */
  path: string
  /** Set in local mode only. */
  id?: string
  title: string
  artist: string
  album: string
  dur: number
  art: string
}

export interface LibraryPage {
  total: number
  items: LibraryTrack[]
}

export interface Group {
  name: string
  count: number
  artist: string
  /** A member track — supplies the cover for the whole group. */
  path: string
  art: string
}

export interface Playlist {
  name: string
  count: number
}

export interface PlaylistTrack {
  path: string
  title: string
  artist: string
  album: string
  dur: number
  art: string
  /** File is gone from disk — listed but greyed, as the desktop does. */
  missing: boolean
}

export type Action =
  | 'play' | 'pause' | 'toggle' | 'stop' | 'next' | 'prev'
  | 'seek' | 'seek_delta' | 'volume' | 'shuffle' | 'mode'
  | 'play_index' | 'remove' | 'clear' | 'subsong'
  | 'enhance' | 'sleep' | 'enqueue' | 'enqueue_play'

export const AUTH_LOST = 'audiocrate:auth-lost'

class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message)
  }
}

/** API root, derived from the bundle's base path. Served from AudioCrate itself
 *  this is "/api"; built with AUDIOCRATE_BASE=/audiocrate/ it becomes
 *  "/audiocrate/api", which the site must proxy through to the desktop. */
export const API_BASE = `${import.meta.env.BASE_URL.replace(/\/$/, '')}/api`

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(API_BASE + path.replace(/^\/api/, ''), {
    credentials: 'same-origin',
    headers: init?.body ? { 'Content-Type': 'application/json' } : undefined,
    ...init,
  })
  if (res.status === 401) {
    window.dispatchEvent(new CustomEvent(AUTH_LOST))
    throw new ApiError(401, 'not authenticated')
  }
  if (!res.ok) throw new ApiError(res.status, `${path} → ${res.status}`)
  return (await res.json()) as T
}

const post = <T,>(path: string, body: unknown) =>
  request<T>(path, { method: 'POST', body: JSON.stringify(body) })

export const api = {
  session: () => request<{ authed: boolean; lockout: number }>('/api/session'),

  login: (pin: string) => post<{ ok: boolean }>('/api/auth', { pin }),

  logout: () => post<{ ok: boolean }>('/api/logout', {}),

  state: () => request<PlayerState>('/api/state'),

  queue: () => request<QueueView>('/api/queue'),

  cmd: (action: Action, value?: unknown) =>
    post<{ ok: boolean }>('/api/cmd', { action, value }),

  library: (opts: { q?: string; album?: string; artist?: string; limit?: number; offset?: number } = {}) => {
    const qs = new URLSearchParams()
    if (opts.q) qs.set('q', opts.q)
    if (opts.album) qs.set('album', opts.album)
    if (opts.artist) qs.set('artist', opts.artist)
    qs.set('limit', String(opts.limit ?? 200))
    qs.set('offset', String(opts.offset ?? 0))
    return request<LibraryPage>(`/api/library?${qs}`)
  },

  albums: (q = '') => request<{ items: Group[] }>(`/api/albums?q=${encodeURIComponent(q)}`),

  artists: (q = '') => request<{ items: Group[] }>(`/api/artists?q=${encodeURIComponent(q)}`),

  playlists: () => request<{ items: Playlist[] }>('/api/playlists'),

  playlist: (name: string) =>
    request<{ name: string; items: PlaylistTrack[] }>(
      `/api/playlist?name=${encodeURIComponent(name)}`),

  playPlaylist: (name: string, play = true) =>
    post<{ ok: boolean; count: number }>('/api/playlist/play', { name, play }),

  theme: () => request<{ accent: string; bg: string }>('/api/theme'),

  /** RMS envelope for the waveform seek bar. 404s for formats ffmpeg can't
   *  decode (chiptunes) — callers fall back to the plain slider. */
  waveform: (path: string, key: string) => {
    const qs = new URLSearchParams({ k: key })
    if (path) qs.set('p', path)
    return request<{ peaks: number[] }>(`/api/waveform?${qs}`)
  },
}

/** Sizes the server is willing to render — anything else is served unscaled. */
export type ArtSize = 96 | 128 | 256 | 640

/** Cover-art URL. `key` changes with the file's mtime, so this caches forever.
 *  Always ask for a size: embedded covers are often 500 kB+, which is absurd
 *  for a 44 px list row and painful over cellular. */
export function artUrl(path: string, key: string, size: ArtSize = 128): string {
  if (!key) return ''
  const qs = new URLSearchParams({ k: key, s: String(size) })
  if (path) qs.set('p', path)
  return `${API_BASE}/art?${qs}`
}

export function fmtTime(ms: number): string {
  if (!ms || ms < 0) return '0:00'
  const total = Math.floor(ms / 1000)
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  return h > 0
    ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
    : `${m}:${String(s).padStart(2, '0')}`
}

/** Chiptune formats get a badge in lists — they behave differently (renders,
 *  subsongs) and it's useful to spot them at a glance. */
export const CHIP_EXTS = new Set(['fur', 'sid', 'vgm', 'vgz', 'nsf', 'nsfe', 'gbs', 'spc', 'mod', 'xm', 's3m', 'it'])
