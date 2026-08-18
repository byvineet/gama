import { D2_THEME_COLORS, type D2ThemeId } from "../core/D2State";

export function resolvePrimary(themeId: D2ThemeId, gamaAccent: string): string {
  if (themeId === "gama") return gamaAccent || "#008FFF";
  return D2_THEME_COLORS[themeId] || gamaAccent;
}

export function deriveGlow(hex: string, alpha = 0.35): string {
  const rgb = hexToRgb(hex);
  if (!rgb) return `rgba(0, 143, 255, ${alpha})`;
  return `rgba(${rgb.r}, ${rgb.g}, ${rgb.b}, ${alpha})`;
}

export function hexToRgb(hex: string): { r: number; g: number; b: number } | null {
  const m = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex.trim());
  if (!m) return null;
  return { r: parseInt(m[1], 16), g: parseInt(m[2], 16), b: parseInt(m[3], 16) };
}
