import type { Config } from "tailwindcss"

const config: Config = {
  content: [
    "./app/**/*.{js,ts,jsx,tsx,mdx}",
    "./components/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        // Court-checker palette
        canvas: "#fcfaf6",       // warm off-white background
        ink: "#1a1d1a",          // primary text (almost-black with green tint)
        ink2: "#4a4d48",         // secondary text (warm gray)
        ink3: "#7a7770",         // tertiary text (muted)
        line: "#e7e3da",         // borders / dividers (warm)
        line2: "#d4cfc1",        // stronger borders
        court: "#1f4d35",        // forest green (primary accent)
        courtdark: "#143524",
        courtlight: "#e8f0eb",   // tinted bg for "open" state
        clay: "#c44e3d",         // coral/clay (danger / closed)
        claylight: "#fae8e3",
        amber: "#b8853d",        // warning / pending
        amberlight: "#f7eedd",
      },
      fontFamily: {
        sans: ["var(--font-geist-sans)", "system-ui", "sans-serif"],
        display: ["var(--font-fraunces)", "Georgia", "serif"],
        mono: ["var(--font-geist-mono)", "ui-monospace", "monospace"],
      },
    },
  },
  plugins: [],
}

export default config
