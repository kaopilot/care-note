import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App.jsx'
import './index.css'

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)

/**
 * Register the service worker so the app is installable — which is what makes
 * ambient capture usable on a phone at the bedside rather than only in a tab.
 *
 * Registered only in a production build. In dev, Vite serves modules that a
 * caching worker would fight with, and the resulting stale-bundle confusion
 * costs more than the feature is worth while iterating. See DECISIONS.md D-053
 * for what the worker deliberately does not cache.
 */
if ('serviceWorker' in navigator && import.meta.env.PROD) {
  window.addEventListener('load', () => {
    navigator.serviceWorker
      .register('/sw.js')
      .catch((error) => console.warn('Service worker registration failed:', error))
  })
}
