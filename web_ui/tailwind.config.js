/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        gama: {
          bg: "#05070d",
          panel: "#0a0f1a",
          card: "#0f172a",
          border: "#1e293b",
          text: "#e2e8f0",
          muted: "#64748b",
          accent: "#38bdf8",
        },
      },
      fontFamily: {
        sans: ["Inter", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "ui-monospace", "monospace"],
      },
    },
  },
  plugins: [],
};
