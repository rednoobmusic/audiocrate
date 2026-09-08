/** Manifest-backed data source — the local-mode twin of the server API.
 *
 * Same method shapes as `api`, so pages don't care which mode they're in. All
 * filtering happens in memory: the manifest is a few hundred KB and lives in
 * one fetch, which is the whole point of taking load off the server.
 */
import type { Group, LibraryPage, LibraryTrack, Playlist, PlaylistTrack } from '../api'
import type { LocalEngine, ManifestTrack } from './engine'

export interface DataSource {
  library(opts?: { q?: string; album?: string; artist?: string; limit?: number; offset?: number }): Promise<LibraryPage>
  albums(q?: string): Promise<{ items: Group[] }>
  artists(q?: string): Promise<{ items: Group[] }>
  playlists(): Promise<{ items: Playlist[] }>
  playlist(name: string): Promise<{ name: string; items: PlaylistTrack[] }>
  artUrl(idOrPath: string, key: string, size: 96 | 128 | 256 | 640): string
  waveform(path: string, key: string): Promise<{ peaks: number[] }>
}

const norm = (s: string) => s.toLowerCase()

export function localSource(engine: LocalEngine): DataSource {
  const all = (): ManifestTrack[] => engine.manifest?.tracks ?? []
  const index = () => new Map(all().map(t => [t.id, t]))

  const toTrack = (t: ManifestTrack): LibraryTrack => ({
    // `path` doubles as the row key and the enqueue handle; in local mode the
    // engine addresses tracks by manifest id.
    path:   t.id,
    id:     t.id,
    title:  t.title,
    artist: t.artist,
    album:  t.album,
    dur:    t.dur,
    art:    t.art ? t.id : '',
  })

  const group = (key: 'album' | 'artist', q = '') => {
    const map = new Map<string, { count: number; artist: string; sample: ManifestTrack }>()
    for (const t of all()) {
      const name = t[key]
      if (!name) continue
      if (q && !norm(name).includes(norm(q))) continue
      const cur = map.get(name)
      if (cur) cur.count++
      else map.set(name, { count: 1, artist: t.artist, sample: t })
    }
    return {
      items: [...map.entries()]
        .sort((a, b) => a[0].localeCompare(b[0]))
        .map(([name, v]) => ({
          name,
          count: v.count,
          artist: v.artist,
          path: v.sample.id,
          art: v.sample.art ? v.sample.id : '',
        })),
    }
  }

  return {
    async library(opts = {}) {
      const { q = '', album = '', artist = '', limit = 200, offset = 0 } = opts
      let items = all()
      if (album) items = items.filter(t => t.album === album)
      if (artist) items = items.filter(t => t.artist === artist)
      if (q) {
        const needle = norm(q)
        items = items.filter(t =>
          norm(t.title).includes(needle) ||
          norm(t.artist).includes(needle) ||
          norm(t.album).includes(needle))
      }
      return { total: items.length, items: items.slice(offset, offset + limit).map(toTrack) }
    },

    async albums(q = '') { return group('album', q) },
    async artists(q = '') { return group('artist', q) },

    async playlists() {
      return {
        items: (engine.manifest?.playlists ?? []).map(p => ({
          name: p.name,
          // Count what can actually be played, not what the desktop list holds.
          count: p.items.filter(it => index().has(it.id)).length,
        })),
      }
    },

    async playlist(name: string) {
      const pl = engine.manifest?.playlists?.find(p => p.name === name)
      const byId = index()
      const items: PlaylistTrack[] = (pl?.items ?? []).map(it => {
        const t = byId.get(it.id)
        return t
          ? {
              path: t.id, title: t.title, artist: t.artist, album: t.album,
              dur: t.dur, art: t.art ? t.id : '', missing: false,
            }
          : {
              // Not in this build (no renderer, or the file moved) — listed and
              // greyed rather than quietly dropped.
              path: '', title: it.name, artist: '', album: '',
              dur: 0, art: '', missing: true,
            }
      })
      return { name, items }
    },

    artUrl(id: string, _key: string, size) {
      if (!id) return ''
      const px = size >= 256 ? 640 : 256      // only two sizes are generated
      return `${engine.base}/media/art/${id}-${px}.jpg`
    },

    // No precomputed envelopes in the static build; the plain seek bar is used.
    async waveform() { throw new Error('waveform unavailable in local mode') },
  }
}
