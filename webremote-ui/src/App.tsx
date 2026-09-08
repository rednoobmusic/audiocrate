import { useCallback, useEffect, useState } from 'react'
import {
  IonApp, IonIcon, IonLabel, IonRouterOutlet, IonSpinner, IonTabBar, IonTabButton,
  IonTabs, IonToast, setupIonicReact,
} from '@ionic/react'
import { IonReactRouter } from '@ionic/react-router'
import { Redirect, Route, useLocation } from 'react-router-dom'
import { albums, discOutline, list, musicalNote } from 'ionicons/icons'
import { api, AUTH_LOST } from './api'
import { PlayerProvider, TOAST } from './store'
import { LocalEngine } from './local/engine'
import { MiniPlayer } from './components/MiniPlayer'
import { Login } from './pages/Login'
import { NowPlaying } from './pages/NowPlaying'
import { Library } from './pages/Library'
import { GroupDetail } from './pages/GroupDetail'
import { Queue } from './pages/Queue'
import { Playlists } from './pages/Playlists'
import { PlaylistDetail } from './pages/PlaylistDetail'

setupIonicReact({ mode: 'ios' })

/** The mini player floats just above the tab bar on every tab except Now
 *  Playing, where it would duplicate the full transport. It sits outside
 *  IonTabs (fixed-positioned) — nesting it in the tab bar breaks tab layout. */
function Chrome() {
  const { pathname } = useLocation()
  if (pathname === '/now') return null
  return <div className="ac-mini-dock"><MiniPlayer /></div>
}

function Shell({ onLogout }: { onLogout: () => void }) {
  return (
    <>
      <IonTabs>
      {/* These MUST use `render`, not JSX children. IonRouterOutlet's stack
          manager clones a child element once and reuses it, so a page written
          as <Route><Page/></Route> renders with the values it had at mount and
          never updates again — the seek bar froze at whatever position it was
          created with while the mini player (outside the outlet) kept ticking. */}
      <IonRouterOutlet>
        <Route exact path="/now" render={() => <NowPlaying onLogout={onLogout} />} />
        <Route exact path="/library" render={() => <Library />} />
        <Route exact path="/library/album/:name" render={() => <GroupDetail kind="album" />} />
        <Route exact path="/library/artist/:name" render={() => <GroupDetail kind="artist" />} />
        <Route exact path="/queue" render={() => <Queue />} />
        <Route exact path="/playlists" render={() => <Playlists />} />
        <Route exact path="/playlists/:name" render={() => <PlaylistDetail />} />
        <Route exact path="/" render={() => <Redirect to="/now" />} />
      </IonRouterOutlet>

      <IonTabBar slot="bottom">
        <IonTabButton tab="now" href="/now">
          <IonIcon icon={discOutline} />
          <IonLabel>NOW</IonLabel>
        </IonTabButton>
        <IonTabButton tab="library" href="/library">
          <IonIcon icon={musicalNote} />
          <IonLabel>LIBRARY</IonLabel>
        </IonTabButton>
        <IonTabButton tab="queue" href="/queue">
          <IonIcon icon={list} />
          <IonLabel>QUEUE</IonLabel>
        </IonTabButton>
        <IonTabButton tab="playlists" href="/playlists">
          <IonIcon icon={albums} />
          <IonLabel>LISTS</IonLabel>
        </IonTabButton>
      </IonTabBar>
      </IonTabs>
      <Chrome />
    </>
  )
}

type Boot =
  | { phase: 'checking' }
  | { phase: 'remote'; authed: boolean }
  | { phase: 'local'; engine: LocalEngine }
  | { phase: 'dead'; reason: string }

export default function App() {
  const [boot, setBoot] = useState<Boot>({ phase: 'checking' })
  const [toast, setToast] = useState('')

  useEffect(() => {
    const onToast = (e: Event) => setToast((e as CustomEvent<string>).detail)
    window.addEventListener(TOAST, onToast)
    return () => window.removeEventListener(TOAST, onToast)
  }, [])

  // Mode detection: prefer driving the desktop when it answers, otherwise fall
  // back to playing the static library locally. That's what lets the same URL
  // work at home and with this machine powered off.
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const s = await api.session()
        if (!cancelled) setBoot({ phase: 'remote', authed: s.authed })
        return
      } catch {
        /* no desktop API reachable — try the static library */
      }
      const engine = new LocalEngine(import.meta.env.BASE_URL)
      try {
        await engine.load()
        if (!cancelled) setBoot({ phase: 'local', engine })
      } catch {
        if (!cancelled) {
          setBoot({
            phase: 'dead',
            reason: 'No player reachable and no offline library published here.',
          })
        }
      }
    })()
    return () => { cancelled = true }
  }, [])

  // NOTE: the desktop accent is deliberately NOT synced here. This UI is
  // monochrome by design, and painting the desktop's purple over it would
  // break the look. /api/theme still exists if that's ever wanted back.

  useEffect(() => {
    const onLost = () => setBoot(b => b.phase === 'remote' ? { phase: 'remote', authed: false } : b)
    window.addEventListener(AUTH_LOST, onLost)
    return () => window.removeEventListener(AUTH_LOST, onLost)
  }, [])

  const logout = useCallback(async () => {
    await api.logout().catch(() => {})
    setBoot({ phase: 'remote', authed: false })
  }, [])

  if (boot.phase === 'checking') {
    return (
      <IonApp>
        <div className="ac-empty" style={{ paddingTop: '45vh' }}><IonSpinner name="dots" /></div>
      </IonApp>
    )
  }

  if (boot.phase === 'dead') {
    return <IonApp><div className="ac-empty" style={{ paddingTop: '40vh' }}>{boot.reason}</div></IonApp>
  }

  if (boot.phase === 'remote' && !boot.authed) {
    return (
      <IonApp>
        <Login onAuthed={() => setBoot({ phase: 'remote', authed: true })} />
      </IonApp>
    )
  }

  const mode = boot.phase === 'local' ? 'local' : 'remote'
  const engine = boot.phase === 'local' ? boot.engine : null

  return (
    <IonApp>
      <PlayerProvider mode={mode} engine={engine}>
        {/* Without a basename the router pushes root-level paths (/queue), so
            navigating away from a subpath deploy produces URLs that belong to
            the main site and break on refresh or share. BASE_URL carries a
            trailing slash; react-router wants it without. */}
        <IonReactRouter basename={import.meta.env.BASE_URL.replace(/\/$/, '') || undefined}>
          <Shell onLogout={logout} />
        </IonReactRouter>
        <IonToast
          isOpen={Boolean(toast)}
          message={toast}
          duration={1800}
          position="top"
          onDidDismiss={() => setToast('')}
        />
      </PlayerProvider>
    </IonApp>
  )
}
