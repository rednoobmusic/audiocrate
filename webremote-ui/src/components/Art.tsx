import { useEffect, useState } from 'react'
import { IonIcon } from '@ionic/react'
import { musicalNotes } from 'ionicons/icons'
import { type ArtSize } from '../api'
import { usePlayer } from '../store'

/** Cover art with a graceful fallback — plenty of chiptune files have none. */
export function Art({ path, artKey, className = '', size = 128 }: {
  path: string
  artKey: string
  className?: string
  /** Pixel size to fetch. List rows want 128 (2× a 44 px slot); only the
   *  Now Playing hero needs 640. */
  size?: ArtSize
}) {
  const [failed, setFailed] = useState(false)
  const { lib } = usePlayer()
  // Local mode serves pre-generated JPEGs by manifest id; server mode hits
  // /api/art. Routing through the active source keeps this component ignorant.
  const src = artKey ? lib.artUrl(path, artKey, size) : ''

  // A new track resets the failure flag, or one missing cover poisons the slot.
  useEffect(() => setFailed(false), [src])

  if (!src || failed) {
    return (
      <div className={`ac-art ac-art-fallback ${className}`}>
        <IonIcon icon={musicalNotes} style={{ fontSize: '38%' }} />
      </div>
    )
  }
  return (
    <img
      className={`ac-art ${className}`}
      src={src}
      alt=""
      loading="lazy"
      decoding="async"
      onError={() => setFailed(true)}
    />
  )
}
