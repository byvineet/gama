/**
 * Sanitize custom SVG scene definitions — declarative only, no script/events.
 */

import type { SvgElement, CustomSvgData } from "../DisplayProtocol";

const ALLOWED_TYPES = new Set([
  "g", "text", "line", "circle", "ellipse", "rect", "path",
  "polygon", "polyline", "image",
]);

const FORBIDDEN_ATTR_RE = /^(on|javascript:|data-js)/i;

function cleanString(v: unknown, max = 4000): string | undefined {
  if (typeof v !== "string") return undefined;
  const s = v.slice(0, max);
  if (/javascript\s*:/i.test(s) || /<\s*script/i.test(s)) return undefined;
  return s;
}

function cleanNumber(v: unknown): number | undefined {
  if (typeof v === "number" && Number.isFinite(v)) return v;
  if (typeof v === "string" && v.trim() !== "" && Number.isFinite(Number(v))) {
    return Number(v);
  }
  return undefined;
}

export function sanitizeSvgElement(raw: unknown, depth = 0): SvgElement | null {
  if (!raw || typeof raw !== "object" || depth > 12) return null;
  const el = raw as Record<string, unknown>;
  const type = String(el.type || "");
  if (!ALLOWED_TYPES.has(type)) return null;

  // reject any event-handler style keys
  for (const key of Object.keys(el)) {
    if (FORBIDDEN_ATTR_RE.test(key)) return null;
  }

  const out: SvgElement = { type: type as SvgElement["type"] };

  if (el.id) out.id = cleanString(el.id, 64);
  for (const k of ["x", "y", "cx", "cy", "r", "rx", "ry", "width", "height", "x1", "y1", "x2", "y2"] as const) {
    const n = cleanNumber(el[k]);
    if (n != null) (out as unknown as Record<string, unknown>)[k] = n;
  }
  const d = cleanString(el.d, 8000);
  if (d) out.d = d;
  const points = cleanString(el.points, 4000);
  if (points) out.points = points;
  const text = cleanString(el.text, 2000);
  if (text) out.text = text;

  for (const k of ["fill", "stroke", "fontFamily", "fontWeight", "transform", "className"] as const) {
    const s = cleanString(el[k], 200);
    if (s) (out as unknown as Record<string, unknown>)[k] = s;
  }
  const sw = el.strokeWidth;
  if (sw != null) out.strokeWidth = cleanNumber(sw) ?? cleanString(sw, 32);
  const op = el.opacity;
  if (op != null) out.opacity = cleanNumber(op) ?? cleanString(op, 16);
  const fs = el.fontSize;
  if (fs != null) out.fontSize = cleanNumber(fs) ?? cleanString(fs, 32);
  if (el.textAnchor === "start" || el.textAnchor === "middle" || el.textAnchor === "end") {
    out.textAnchor = el.textAnchor;
  }

  // images: data:, relative, or http(s) — never javascript:
  if (type === "image") {
    const href = cleanString(el.href, 500000);
    if (
      href &&
      (href.startsWith("data:image/") ||
        href.startsWith("https://") ||
        href.startsWith("http://") ||
        href.startsWith("/") ||
        href.startsWith("./") ||
        href.startsWith("blob:"))
    ) {
      out.href = href;
    } else {
      return null; // reject unsafe image sources
    }
  }

  if (Array.isArray(el.children)) {
    const kids = el.children
      .map((c) => sanitizeSvgElement(c, depth + 1))
      .filter(Boolean) as SvgElement[];
    if (kids.length) out.children = kids;
  }

  return out;
}

export function sanitizeCustomSvg(data: unknown): CustomSvgData | null {
  if (!data || typeof data !== "object") return null;
  const d = data as Record<string, unknown>;
  const viewBox = cleanString(d.viewBox, 64) || "0 0 1000 600";
  const elementsIn = Array.isArray(d.elements) ? d.elements : [];
  const elements = elementsIn
    .map((e) => sanitizeSvgElement(e))
    .filter(Boolean) as SvgElement[];
  return {
    viewBox,
    width: cleanString(d.width, 32) ?? cleanNumber(d.width) ?? "100%",
    height: cleanString(d.height, 32) ?? cleanNumber(d.height) ?? "100%",
    background: cleanString(d.background, 64),
    elements,
  };
}
