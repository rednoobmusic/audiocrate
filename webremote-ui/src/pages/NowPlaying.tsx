import { useCallback, useEffect, useRef, useState } from 'react'
import {
  IonActionSheet, IonButton, IonButtons, IonContent, IonHeader, IonIcon, IonPage,
  IonRange, IonTitle, IonToolbar,
} from '@ionic/react'
import {
  ellipsisHorizontal, flash, pause, play, playSkipBack, playSkipForward,
  pulse, repeat, shuffle as shuffleIcon, stop, volumeHigh, volumeLow,
} from 'ionicons/icons'
import { fmtTime } from '../api'
import { usePlayer } from '../store'
import { Art } from '../components/Art'
import { WaveSeek } from '../components/WaveSeek'

const WAVE_PREF = 'audiocrate:waveform'

/* Matches Player.mode: 0 sequential, 1 repeat-all, 2 repeat-one. The desktop
   abbreviates these to SEQ/REP/RP1; on a phone there's room to just say it. */
const MODE_LABEL = ['IN ORDER', 'REPEAT', 'REPEAT 1']

export function NowPlaying({ onLogout }: { onLogout: () => void }) {
  const { state, position, cmd, seekTo } = usePlayer()
  const [menu, setMenu] = useState(false)
  const [subsongs, setSubsongs] = useState(false)
  // While dragging we show the thumb's value, not the (still-advancing) clock.
  // Driven by knob-move events, NOT by ionInput: ionInput also fires when the
  // value prop changes programmatically, which latched `dragging` on the first
  // poll and froze the elapsed time for the rest of the session.
  const [dragging, setDragging] = useState<number | null>(null)
  const isDragging = useRef(false)

  // Same treatment for volume: live-applied while dragging, throttled.
  const [volDrag, setVolDrag] = useState<number | null>(null)
  const volDragging = useRef(false)
  const lastVolSend = useRef(0)

  // Waveform vs plain slider, remembered per device.
  const [wave, setWave] = useState(() => localStorage.getItem(WAVE_PREF) === '1')
  // Set when the current file has no decodable waveform (chiptunes), so we fall
  // back for this track only without forgetting the preference.
  const [waveFailed, setWaveFailed] = useState(false)
  const trackPath = state?.path ?? ''
  useEffect(() => setWaveFailed(false), [trackPath])

  const toggleWave = useCallback(() => {
    setWave(v => {
      localStorage.setItem(WAVE_PREF, v ? '0' : '1')
      return !v
    })
  }, [])

  const onUnavailable = useCallback(() => setWaveFailed(true), [])

  if (!state) return <IonPage><IonContent /></IonPage>

  const shown = dragging ?? position
  const dur = state.dur_ms

  return (
    <IonPage>
      <IonHeader>
        <IonToolbar>
          <IonTitle>NOW PLAYING</IonTitle>
          <IonButtons slot="end">
            <IonButton onClick={() => setMenu(true)} aria-label="More">
              <IonIcon slot="icon-only" icon={ellipsisHorizontal} />
            </IonButton>
          </IonButtons>
        </IonToolbar>
      </IonHeader>

      <IonContent scrollY={false}>
        {!state.has_track ? (
          <div className="ac-empty">
            Nothing queued.<br />Pick something from the Library tab.
          </div>
        ) : (
          <div className="ac-now">
            <div className="ac-now-art">
              <Art path={state.path} artKey={state.art} size={640} />
            </div>

            <div className="ac-now-meta">
              <p className="ac-now-title">{state.title}</p>
              <p className="ac-now-sub">{state.artist || 'Unknown artist'}</p>
              {state.album && <p className="ac-now-album">{state.album}</p>}
            </div>

            <div>
              {wave && !waveFailed ? (
                <WaveSeek
                  path={state.path}
                  artKey={state.art}
                  position={shown}
                  duration={dur}
                  onSeek={seekTo}
                  onUnavailable={onUnavailable}
                />
              ) : (
                <IonRange
                  className="ac-seek"
                  min={0}
                  max={Math.max(1, dur)}
                  /* With an unknown duration max collapses to 1, and any
                     non-zero position would clamp to it — pinning the knob to
                     the far right instead of the start. */
                  value={dur > 0 ? Math.min(shown, dur) : 0}
                  disabled={dur <= 0}
                  onIonKnobMoveStart={() => { isDragging.current = true }}
                  onIonInput={e => {
                    if (isDragging.current) setDragging(Number(e.detail.value))
                  }}
                  onIonKnobMoveEnd={e => {
                    isDragging.current = false
                    seekTo(Number(e.detail.value))
                    setDragging(null)
                  }}
                />
              )}
              <div className="ac-times">
                <span>{fmtTime(shown)}</span>
                <span>{dur > 0 ? `-${fmtTime(Math.max(0, dur - shown))}` : '--:--'}</span>
              </div>
            </div>

            <div className="ac-transport">
              <button onClick={() => cmd('prev')} aria-label="Previous">
                <IonIcon icon={playSkipBack} style={{ fontSize: 30 }} />
              </button>
              <button
                className="ac-play-btn"
                onClick={() => cmd('toggle')}
                aria-label={state.playing ? 'Pause' : 'Play'}
              >
                <IonIcon icon={state.playing ? pause : play} style={{ fontSize: 32 }} />
              </button>
              <button onClick={() => cmd('next')} aria-label="Next">
                <IonIcon icon={playSkipForward} style={{ fontSize: 30 }} />
              </button>
            </div>

            <div className="ac-toggles">
              <button
                className={`ac-toggle ${state.shuffle ? 'on' : ''}`}
                onClick={() => cmd('shuffle', !state.shuffle)}
              >
                <IonIcon icon={shuffleIcon} style={{ fontSize: 19 }} />
                SHUFFLE
              </button>
              <button
                className={`ac-toggle ${state.mode !== 0 ? 'on' : ''}`}
                onClick={() => cmd('mode', (state.mode + 1) % 3)}
              >
                <IonIcon icon={repeat} style={{ fontSize: 19 }} />
                {MODE_LABEL[state.mode] ?? 'IN ORDER'}
              </button>
              {/* Enhance is real in both modes now: the desktop DSP chain
                  remotely, an equivalent Web Audio EQ locally. */}
              <button
                className={`ac-toggle ${state.enhance ? 'on' : ''}`}
                onClick={() => cmd('enhance', !state.enhance)}
              >
                <IonIcon icon={flash} style={{ fontSize: 19 }} />
                ENHANCE
              </button>
            </div>

            {state.subsongs > 1 && (
              <button className="ac-subsong-btn" onClick={() => setSubsongs(true)}>
                Subsong {state.subsong + 1} / {state.subsongs}
              </button>
            )}

            <div className="ac-volume">
              <IonIcon icon={volumeLow} />
              <IonRange
                min={0}
                max={1}
                step={0.01}
                /* Local value wins while dragging (and briefly after), or the
                   ~900ms poll snaps the knob back to a stale volume mid-drag,
                   which is what made it jitter. */
                value={volDrag ?? state.volume}
                style={{ flex: 1 }}
                onIonKnobMoveStart={() => { volDragging.current = true }}
                onIonInput={e => {
                  if (!volDragging.current) return
                  const v = Number(e.detail.value)
                  setVolDrag(v)
                  // Apply live rather than only on release, throttled so a
                  // drag doesn't fire a request per pixel.
                  const now = Date.now()
                  if (now - lastVolSend.current > 80) {
                    lastVolSend.current = now
                    cmd('volume', v)
                  }
                }}
                onIonKnobMoveEnd={e => {
                  volDragging.current = false
                  const v = Number(e.detail.value)
                  cmd('volume', v)
                  setVolDrag(v)
                  // Hold the local value until the server's snapshot catches up.
                  window.setTimeout(() => setVolDrag(null), 900)
                }}
              />
              <IonIcon icon={volumeHigh} />
            </div>
          </div>
        )}

        <IonActionSheet
          isOpen={menu}
          onDidDismiss={() => setMenu(false)}
          header={state.error || undefined}
          buttons={[
            {
              text: wave ? 'Use plain seek bar' : 'Use waveform seek bar',
              icon: pulse,
              handler: toggleWave,
            },
            { text: 'Stop playback', icon: stop, handler: () => { cmd('stop') } },
            { text: 'Clear queue', role: 'destructive', handler: () => { cmd('clear') } },
            { text: 'Sign out this device', handler: onLogout },
            { text: 'Cancel', role: 'cancel' },
          ]}
        />

        <IonActionSheet
          isOpen={subsongs}
          onDidDismiss={() => setSubsongs(false)}
          header="Subsong"
          buttons={[
            ...Array.from({ length: state.subsongs }, (_, i) => ({
              text: `Subsong ${i + 1}${i === state.subsong ? '  ●' : ''}`,
              handler: () => { cmd('subsong', i) },
            })),
            { text: 'Cancel', role: 'cancel' as const },
          ]}
        />
      </IonContent>
    </IonPage>
  )
}
