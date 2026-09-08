import { useCallback, useEffect, useRef, useState } from 'react'
import {
  IonContent, IonHeader, IonInfiniteScroll, IonInfiniteScrollContent, IonItem,
  IonLabel, IonList, IonPage, IonSearchbar, IonSegment, IonSegmentButton,
  IonSpinner, IonTitle, IonToolbar,
} from '@ionic/react'
import { useHistory } from 'react-router-dom'
import { type Group, type LibraryTrack } from '../api'
import { usePlayer } from '../store'
import { Art } from '../components/Art'
import { TrackRow } from '../components/TrackRow'

type Tab = 'songs' | 'albums' | 'artists'
const PAGE = 100

/** Library root: search + a three-way segment. Albums and artists drill into
 *  their own page rather than expanding inline, so no screen gets crowded. */
export function Library() {
  const [tab, setTab] = useState<Tab>('songs')
  const [q, setQ] = useState('')
  const [songs, setSongs] = useState<LibraryTrack[]>([])
  const [total, setTotal] = useState(0)
  const [groups, setGroups] = useState<Group[]>([])
  const [loading, setLoading] = useState(true)
  const history = useHistory()
  const { state, cmd, lib } = usePlayer()

  // Debounce so typing doesn't fire a query per keystroke.
  const debounced = useRef<number>()
  const [term, setTerm] = useState('')
  useEffect(() => {
    window.clearTimeout(debounced.current)
    debounced.current = window.setTimeout(() => setTerm(q.trim()), 220)
    return () => window.clearTimeout(debounced.current)
  }, [q])

  const load = useCallback(async () => {
    setLoading(true)
    try {
      if (tab === 'songs') {
        const page = await lib.library({ q: term, limit: PAGE, offset: 0 })
        setSongs(page.items)
        setTotal(page.total)
      } else {
        const res = tab === 'albums' ? await lib.albums(term) : await lib.artists(term)
        setGroups(res.items)
      }
    } catch {
      /* handled globally */
    } finally {
      setLoading(false)
    }
  }, [tab, term, lib])

  useEffect(() => { load() }, [load])

  const loadMore = async () => {
    if (songs.length >= total) return
    const page = await lib.library({ q: term, limit: PAGE, offset: songs.length })
    setSongs(prev => [...prev, ...page.items])
  }

  const openGroup = (g: Group) =>
    history.push(`/library/${tab === 'albums' ? 'album' : 'artist'}/${encodeURIComponent(g.name)}`)

  return (
    <IonPage>
      <IonHeader>
        <IonToolbar>
          <IonTitle>LIBRARY</IonTitle>
        </IonToolbar>
        {/* Separate toolbars — sharing one lays them out side by side and
            squeezes the segment labels down to "GS" / "ART". */}
        <IonToolbar>
          <IonSearchbar
            value={q}
            placeholder="Search library"
            debounce={0}
            onIonInput={e => setQ(e.detail.value ?? '')}
          />
        </IonToolbar>
        <IonToolbar>
          <IonSegment value={tab} onIonChange={e => setTab(e.detail.value as Tab)}>
            <IonSegmentButton value="songs">Songs</IonSegmentButton>
            <IonSegmentButton value="albums">Albums</IonSegmentButton>
            <IonSegmentButton value="artists">Artists</IonSegmentButton>
          </IonSegment>
        </IonToolbar>
      </IonHeader>

      <IonContent>
        {loading && (
          <div className="ac-empty"><IonSpinner name="dots" /></div>
        )}

        {!loading && tab === 'songs' && (
          songs.length === 0 ? (
            <div className="ac-empty">
              {term ? `Nothing matching “${term}”.` : 'Library is empty — add folders on the desktop.'}
            </div>
          ) : (
            <>
              <IonList>
                {songs.map(t => (
                  <TrackRow
                    key={t.path}
                    path={t.path}
                    artKey={t.art}
                    title={t.title}
                    artist={t.artist}
                    dur={t.dur}
                    active={state?.path === t.path}
                    onClick={() => cmd('enqueue_play', [t.path])}
                  />
                ))}
              </IonList>
              <IonInfiniteScroll
                disabled={songs.length >= total}
                onIonInfinite={async e => { await loadMore(); (e.target as HTMLIonInfiniteScrollElement).complete() }}
              >
                <IonInfiniteScrollContent loadingSpinner="dots" />
              </IonInfiniteScroll>
            </>
          )
        )}

        {!loading && tab !== 'songs' && (
          groups.length === 0 ? (
            <div className="ac-empty">No {tab} found.</div>
          ) : (
            <IonList>
              {groups.map(g => (
                <IonItem key={g.name} button detail onClick={() => openGroup(g)} lines="full">
                  <Art path={g.path} artKey={g.art} className="ac-group-art" />
                  <IonLabel>
                    <h3>{g.name}</h3>
                    <p>
                      {tab === 'albums' && g.artist ? `${g.artist} — ` : ''}
                      {g.count} track{g.count === 1 ? '' : 's'}
                    </p>
                  </IonLabel>
                </IonItem>
              ))}
            </IonList>
          )
        )}
      </IonContent>
    </IonPage>
  )
}
