/** Server-backed data source — thin adapter so `api` satisfies DataSource. */
import { api, artUrl } from '../api'
import type { DataSource } from './source'

export const remoteSource: DataSource = {
  library: opts => api.library(opts),
  albums: q => api.albums(q),
  artists: q => api.artists(q),
  playlists: () => api.playlists(),
  playlist: name => api.playlist(name),
  artUrl: (path, key, size) => artUrl(path, key, size),
  waveform: (path, key) => api.waveform(path, key),
}
