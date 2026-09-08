import { useEffect, useState } from 'react'
import {
  IonContent, IonHeader, IonItem, IonLabel, IonList, IonPage,
  IonSpinner, IonTitle, IonToolbar,
} from '@ionic/react'
import { useHistory } from 'react-router-dom'
import { usePlayer } from '../store'
import { type Playlist } from '../api'

/** Saved playlists from the desktop's playlists.json. Tapping one opens it so
 *  you can read the contents — queueing the whole thing blind was the only
 *  option before. */
export function Playlists() {
  const [items, setItems] = useState<Playlist[]>([])
  const [loading, setLoading] = useState(true)
  const history = useHistory()
  const { lib } = usePlayer()

  useEffect(() => {
    lib.playlists()
      .then(r => setItems(r.items))
      .catch(() => setItems([]))
      .finally(() => setLoading(false))
  }, [lib])

  return (
    <IonPage>
      <IonHeader>
        <IonToolbar><IonTitle>PLAYLISTS</IonTitle></IonToolbar>
      </IonHeader>

      <IonContent>
        {loading ? (
          <div className="ac-empty"><IonSpinner name="dots" /></div>
        ) : items.length === 0 ? (
          <div className="ac-empty">
            No saved playlists.<br />Save one on the desktop and it shows up here.
          </div>
        ) : (
          <IonList>
            {items.map(p => (
              <IonItem
                key={p.name}
                button
                detail
                lines="full"
                onClick={() => history.push(`/playlists/${encodeURIComponent(p.name)}`)}
              >
                <IonLabel>
                  <h3>{p.name}</h3>
                  <p>{p.count} track{p.count === 1 ? '' : 's'}</p>
                </IonLabel>
              </IonItem>
            ))}
          </IonList>
        )}
      </IonContent>
    </IonPage>
  )
}
