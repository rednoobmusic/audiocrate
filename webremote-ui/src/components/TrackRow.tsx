import { IonItem, IonLabel } from '@ionic/react'
import { CHIP_EXTS, fmtTime } from '../api'
import { Art } from './Art'

function ext(path: string): string {
  const i = path.lastIndexOf('.')
  return i < 0 ? '' : path.slice(i + 1).toLowerCase()
}

export function TrackRow({ path, artKey, title, artist, album, dur, active, missing, onClick, children }: {
  path: string
  artKey: string
  title: string
  artist?: string
  album?: string
  dur?: number
  active?: boolean
  /** File is gone from disk — shown greyed and italic, like the desktop does. */
  missing?: boolean
  onClick?: () => void
  children?: React.ReactNode
}) {
  const e = ext(path)
  const sub = [artist, album].filter(Boolean).join(' - ')

  return (
    <IonItem
      button={Boolean(onClick)}
      detail={false}
      onClick={onClick}
      lines="full"
      className={missing ? 'ac-missing' : undefined}
    >
      <Art path={path} artKey={artKey} className="ac-row-art" />
      <IonLabel>
        <h3 style={active ? { textDecoration: 'underline' } : undefined}>
          {title}
          {CHIP_EXTS.has(e) && <span className="ac-badge">{e}</span>}
          {missing && <span className="ac-badge">missing</span>}
        </h3>
        {sub && <p>{sub}</p>}
      </IonLabel>
      {active && <div className="ac-playing-dot" slot="end" />}
      {!active && dur ? (
        <span slot="end" style={{ fontSize: 11, color: 'var(--ac-fg-dimmer)' }}>
          {fmtTime(dur)}
        </span>
      ) : null}
      {children}
    </IonItem>
  )
}
