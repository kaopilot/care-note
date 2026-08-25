/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      // Role colours are semantic, not decorative: the brief requires
      // AI-scribed entries be visually distinct from human-authored ones.
      // Fixing the palette here in Phase 0 means every later phase inherits
      // the same visual grammar instead of inventing one per component.
      colors: {
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
      },
    },
  },
  plugins: [],
}
