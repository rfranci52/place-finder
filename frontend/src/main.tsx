import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
// Leaflet's stylesheet first, so the overrides in index.css win at equal specificity.
import 'leaflet/dist/leaflet.css'
import './index.css'
import App from './App.tsx'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
