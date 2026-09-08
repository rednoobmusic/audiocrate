import { IonIcon } from '@ionic/react'
import { useHistory } from 'react-router-dom'
import { pause, play, playSkipForward } from 'ionicons/icons'
import { usePlayer } from '../store'
import { Art } from './Art'

/** Persistent transport strip above the tab bar. Tap it to open Now Playing —
 *  the standard mobile-player affordance, so the other tabs never need their
 *  own controls. Hidden on Now Playing itself (it would be redundant). */
export function MiniPlayer() {
  const { state, position, cmd } = usePlayer()
  const history = useHistory()

  if (!state?.has_track) return null

  const pct = state.dur_ms > 0 ? Math.min(100, (position / state.dur_ms) * 100) : 0

  return (
    <div>
      <div className="ac-mini-progress"><div style={{ width: `${pct}%` }} /></div>
      <div className="ac-mini" onClick={() => history.push('/now')}>
        <Art path={state.path} artKey={state.art} />
        <div className="ac-mini-text">
          <div className="ac-mini-title">{state.title}</div>
          <div className="ac-mini-sub">{state.artist || state.album || '—'}</div>
        </div>
        <button
          aria-label={state.playing ? 'Pause' : 'Play'}
          onClick={e => { e.stopPropagation(); cmd('toggle') }}
        >
          <IonIcon icon={state.playing ? pause : play} style={{ fontSize: 24 }} />
        </button>
        <button
          aria-label="Next track"
          onClick={e => { e.stopPropagation(); cmd('next') }}
        >
          <IonIcon icon={playSkipForward} style={{ fontSize: 20 }} />
        </button>
      </div>
    </div>
  )
}
