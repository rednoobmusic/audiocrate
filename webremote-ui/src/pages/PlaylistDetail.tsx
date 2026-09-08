import { useEffect, useState } from 'react'
import {
  IonBackButton, IonButton, IonButtons, IonContent, IonHeader, IonIcon, IonList,
  IonPage, IonSpinner, IonTitle, IonToolbar,
} from '@ionic/react'
import { useParams } from 'react-router-dom'
import { add, play } from 'ionicons/icons'
import { type PlaylistTrack } from '../api'
import { usePlayer } from '../store'
import { TrackRow } from '../components/TrackRow'

/** One saved playlist, listed in full. Tapping the playlist itself used to jump
 *  straight to queueing it — this lets you read what's in it first, and play or
 *  enqueue individual tracks. */
export function PlaylistDetail() {
  const { name } = useParams<{ name: string }>()
  const decoded = decodeURIComponent(name)
  const [items, setItems] = useState<PlaylistTrack[]>([])
  const [loading, setLoading] = useState(true)
  const { state, cmd, lib } = usePlayer()

  useEffect(() => {
    setLoading(true)
    lib.playlist(decoded)
      .then(r => setItems(r.items))
      .catch(() => setItems([]))
      .finally(() => setLoading(false))
  }, [decoded, lib])

  const playable = items.filter(t => !t.missing).map(t => t.path)
  const missing = items.length - playable.length

  return (
    <IonPage>
      <IonHeader>
        <IonToolbar>
          <IonButtons slot="start">
            <IonBackButton defaultHref="/playlists" text="" />
          </IonButtons>
          <IonTitle>{decoded}</IonTitle>
          <IonButtons slot="end">
            <IonButton
              disabled={!playable.length}
              onClick={() => cmd('enqueue', playable)}
              aria-label="Add all to queue"
            >
              <IonIcon slot="icon-only" icon={add} />
            </IonButton>
            <IonButton
              disabled={!playable.length}
              onClick={() => cmd('enqueue_play', playable)}
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
        ) : items.length === 0 ? (
          <div className="ac-empty">This playlist is empty.</div>
        ) : (
          <>
            <div className="ac-section-head">
              {items.length} track{items.length === 1 ? '' : 's'}
              {missing > 0 && ` · ${missing} unavailable`}
            </div>
            <IonList>
              {items.map((t, i) => (
                <TrackRow
                  key={`${t.path}-${i}`}
                  path={t.path}
                  artKey={t.art}
                  title={t.title}
                  artist={t.artist}
                  album={t.album}
                  dur={t.dur}
                  missing={t.missing}
                  active={state?.path === t.path}
                  onClick={t.missing ? undefined : () => cmd('enqueue_play', [t.path])}
                />
              ))}
            </IonList>
          </>
        )}
      </IonContent>
    </IonPage>
  )
}
