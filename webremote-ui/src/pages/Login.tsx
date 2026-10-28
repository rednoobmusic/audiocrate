import { useCallback, useEffect, useState } from 'react'
import { IonContent, IonIcon, IonPage } from '@ionic/react'
import { backspace } from 'ionicons/icons'
import { api } from '../api'

const PIN_LEN = 6

/** PIN gate. On success the server sets a 90-day signed cookie, so this screen
 *  is a once-per-device thing rather than a login you keep repeating. */
export function Login({ onAuthed }: { onAuthed: () => void }) {
  const [pin, setPin] = useState('')
  const [err, setErr] = useState('')
  const [shake, setShake] = useState(false)
  const [busy, setBusy] = useState(false)
  const [lockout, setLockout] = useState(0)

  // Count the lockout down live rather than leaving a stale "try again in 300s".
  useEffect(() => {
    if (lockout <= 0) return
    const t = window.setInterval(() => setLockout(v => Math.max(0, v - 1)), 1000)
    return () => window.clearInterval(t)
  }, [lockout])

  const submit = useCallback(async (value: string) => {
    setBusy(true)
    try {
      await api.login(value)
      setErr('')
      onAuthed()
    } catch {
      const s = await api.session().catch(() => ({ authed: false, lockout: 0 }))
      setLockout(s.lockout)
      setErr(s.lockout > 0 ? 'Too many attempts' : 'Wrong PIN')
      setShake(true)
      window.setTimeout(() => setShake(false), 340)
      window.setTimeout(() => setPin(''), 240)
      if (navigator.vibrate) navigator.vibrate(80)
    } finally {
      setBusy(false)
    }
  }, [onAuthed])

  const tap = (digit: string) => {
    if (busy || lockout > 0 || pin.length >= PIN_LEN) return
    const next = pin + digit
    setPin(next)
    setErr('')
    if (next.length === PIN_LEN) submit(next)
  }

  const keys = ['1', '2', '3', '4', '5', '6', '7', '8', '9']

  return (
    <IonPage>
      <IonContent scrollY={false}>
        <div className="ac-login">
          <h1>AUDIOCRATE</h1>
          <p className="sub">
            {lockout > 0 ? `Locked, retry in ${lockout}s` : 'Enter PIN to continue'}
          </p>

          <div className={`ac-pips ${shake ? 'ac-shake' : ''}`}>
            {Array.from({ length: PIN_LEN }, (_, i) => (
              <div key={i} className={`ac-pip ${i < pin.length ? 'on' : ''} ${err ? 'err' : ''}`} />
            ))}
          </div>

          <div className="ac-login-err">{err}</div>

          <div className="ac-keypad">
            {keys.map(k => (
              <button key={k} className="ac-key" onClick={() => tap(k)} disabled={lockout > 0}>
                {k}
              </button>
            ))}
            <button className="ac-key blank" disabled />
            <button className="ac-key" onClick={() => tap('0')} disabled={lockout > 0}>0</button>
            <button
              className="ac-key"
              aria-label="Delete"
              onClick={() => { setPin(p => p.slice(0, -1)); setErr('') }}
            >
              <IonIcon icon={backspace} style={{ fontSize: 22 }} />
            </button>
          </div>

          <p className="sub" style={{ marginTop: 12, textAlign: 'center', maxWidth: 260 }}>
            This device stays signed in for 90 days.
          </p>
        </div>
      </IonContent>
    </IonPage>
  )
}
