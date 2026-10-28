import {
  IonButton, IonButtons, IonContent, IonHeader, IonIcon, IonItemOption,
  IonItemOptions, IonItemSliding, IonList, IonPage, IonTitle, IonToolbar,
} from '@ionic/react'
import { trash } from 'ionicons/icons'
import { usePlayer } from '../store'
import { TrackRow } from '../components/TrackRow'

/** The live queue in play order: what's on now, then what follows (which
 *  respects shuffle and repeat, because the server computes it with the same
 *  `upcoming()` the desktop Up Next panel uses). Swipe a row to remove it. */
export function Queue() {
  const { state, queue, cmd } = usePlayer()

  const byIndex = new Map(queue.items.map(t => [t.i, t]))
  const current = queue.index >= 0 ? byIndex.get(queue.index) : undefined
  const upNext = queue.upcoming.map(i => byIndex.get(i)).filter(Boolean)

  return (
    <IonPage>
      <IonHeader>
        <IonToolbar>
          <IonTitle>QUEUE</IonTitle>
          <IonButtons slot="end">
            <IonButton
              disabled={!queue.items.length}
              onClick={() => cmd('clear')}
              style={{ fontSize: 11, letterSpacing: '0.08em' }}
            >
              CLEAR
            </IonButton>
          </IonButtons>
        </IonToolbar>
      </IonHeader>

      <IonContent>
        {queue.items.length === 0 ? (
          <div className="ac-empty">Queue is empty.<br />Add tracks from the Library.</div>
        ) : (
          <>
            {current && (
              <>
                <div className="ac-section-head">Playing</div>
                <IonList>
                  <TrackRow
                    path={current.path}
                    artKey={current.art}
                    title={current.title}
                    artist={current.artist}
                    album={current.album}
                    active
                  />
                </IonList>
              </>
            )}

            <div className="ac-section-head">
              Up next: {upNext.length} {state?.shuffle ? '(shuffled)' : ''}
            </div>
            <IonList>
              {upNext.map((t, n) => (
                <IonItemSliding key={`${t!.i}-${n}`}>
                  <TrackRow
                    path={t!.path}
                    artKey={t!.art}
                    title={t!.title}
                    artist={t!.artist}
                    album={t!.album}
                    onClick={() => cmd('play_index', t!.i)}
                  />
                  <IonItemOptions side="end">
                    <IonItemOption color="danger" onClick={() => cmd('remove', t!.i)}>
                      <IonIcon slot="icon-only" icon={trash} />
                    </IonItemOption>
                  </IonItemOptions>
                </IonItemSliding>
              ))}
            </IonList>

            {upNext.length === 0 && (
              <div className="ac-empty">Nothing after this track.</div>
            )}
          </>
        )}
      </IonContent>
    </IonPage>
  )
}
