import React from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'

/* Ionic core + the handful of utilities the pages actually use. Importing the
   full bundle would roughly double the payload for no benefit. */
import '@ionic/react/css/core.css'
import '@ionic/react/css/normalize.css'
import '@ionic/react/css/structure.css'
import '@ionic/react/css/typography.css'
import '@ionic/react/css/padding.css'
import '@ionic/react/css/flex-utils.css'
import '@ionic/react/css/palettes/dark.always.css'

import './theme.css'

createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
