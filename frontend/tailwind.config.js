/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      // Typography carries provenance in this interface. Human-authored notes
      // are set in the UI sans; machine-generated summaries and transcript text
      // are set in mono. That is not decoration — it means a clinician can tell
      // who wrote something from across the room, before reading a single
      // label, and it degrades gracefully for a reader who cannot see colour.
      //
      // System stacks rather than webfonts: the build must run offline for a
      // reviewer with no network, and every dependency has to earn its line in
      // ATTRIBUTION.txt.
      fontFamily: {
        sans: ['ui-sans-serif', 'system-ui', '-apple-system', 'Segoe UI', 'Roboto',
               'Helvetica Neue', 'Arial', 'sans-serif'],
        mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'Consolas',
               'Liberation Mono', 'monospace'],
      },
      colors: {
        // Role colours are semantic, not decorative: the brief requires
        // AI-scribed entries be visually distinct from human-authored ones.
        // Fixed in Phase 0 so every later phase inherits one visual grammar.
        role: {
          patient: '#0f766e',
          staff: '#7c3aed',
          clinician: '#1d4ed8',
          system: '#b45309',
        },
        risk: {
          none: '#64748b',
          low: '#0891b2',
          medium: '#ca8a04',
          high: '#ea580c',
          critical: '#be123c',
        },
        // Cool paper rather than warm cream: this is a clinical surface read
        // under fluorescent light between patients, not a reading experience.
        paper: '#eef1f5',
        ink: '#0f1720',
      },
    },
  },
  plugins: [],
}
