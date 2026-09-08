import { useEffect, useState } from 'react'
import {
  IonBackButton, IonButton, IonButtons, IonContent, IonHeader, IonIcon, IonList,
  IonPage, IonSpinner, IonTitle, IonToolbar,
} from '@ionic/react'
import { useParams } from 'react-router-dom'
import { add, play } from 'ionicons/icons'
import { type LibraryTrack } from '../api'
import { usePlayer } from '../store'
import { TrackRow } from '../components/TrackRow'

/** One album or one artist. Pushed onto the Library tab's stack, so iOS's
 *  swipe-back gesture works and the tab bar stays put. */
export function GroupDetail({ kind }: { kind: 'album' | 'artist' }) {
  const { name } = useParams<{ name: string }>()
  const decoded = decodeURIComponent(name)
  const [tracks, setTracks] = useState<LibraryTrack[]>([])
  const [loading, setLoading] = useState(true)
  const { state, cmd, lib } = usePlayer()

  useEffect(() => {
    setLoading(true)
    lib.library(kind === 'album' ? { album: decoded, limit: 500 } : { artist: decoded, limit: 500 })
      .then(p => setTracks(p.items))
      .catch(() => setTracks([]))
      .finally(() => setLoading(false))
  }, [kind, decoded, lib])

  const paths = tracks.map(t => t.path)

  return (
    <IonPage>
      <IonHeader>
        <IonToolbar>
          <IonButtons slot="start">
            <IonBackButton defaultHref="/library" text="" />
          </IonButtons>
          <IonTitle>{decoded}</IonTitle>
          <IonButtons slot="end">
            <IonButton
              disabled={!paths.length}
              onClick={() => cmd('enqueue', paths)}
              aria-label="Add all to queue"
            >
              <IonIcon slot="icon-only" icon={add} />
            </IonButton>
            <IonButton
              disabled={!paths.length}
              onClick={() => cmd('enqueue_play', paths)}
              aria-label="Play all"
            >
              <IonIcon slot="icon-only" icon={play} />
            </IonButton>
          </IonButtons>
        </IonToolbar>
      </IonHeader>

      <IonContent>
        {loading ? (
          <div className="ac-empty"><IonSpinner name="dots" /></div>
        ) : tracks.length === 0 ? (
          <div className="ac-empty">No tracks here.</div>
        ) : (
          <IonList>
            {tracks.map(t => (
              <TrackRow
                key={t.path}
                path={t.path}
                artKey={t.art}
                title={t.title}
                artist={kind === 'album' ? t.artist : t.album}
                dur={t.dur}
                active={state?.path === t.path}
                onClick={() => cmd('enqueue_play', [t.path])}
              />
            ))}
          </IonList>
        )}
      </IonContent>
    </IonPage>
  )
}
