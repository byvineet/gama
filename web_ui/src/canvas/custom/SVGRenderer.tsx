import type { CSSProperties } from "react";
import type { SvgElement, CustomSvgData } from "../DisplayProtocol";
import { sanitizeCustomSvg } from "./SVGValidator";

function renderEl(el: SvgElement, key: string | number): React.ReactNode {
  const common: Record<string, unknown> = {};
  if (el.fill != null) common.fill = el.fill;
  if (el.stroke != null) common.stroke = el.stroke;
  if (el.strokeWidth != null) common.strokeWidth = el.strokeWidth;
  if (el.opacity != null) common.opacity = el.opacity;
  if (el.transform) common.transform = el.transform;
  if (el.className) common.className = el.className;

  const kids = el.children?.map((c, i) => renderEl(c, `${key}-${i}`));

  switch (el.type) {
    case "g":
      return <g key={key} {...common}>{kids}</g>;
    case "text":
      return (
        <text
          key={key}
          x={el.x}
          y={el.y}
          fontSize={el.fontSize}
          fontFamily={el.fontFamily || "Inter, system-ui, sans-serif"}
          fontWeight={el.fontWeight}
          textAnchor={el.textAnchor || "start"}
          {...common}
        >
          {el.text}
          {kids}
        </text>
      );
    case "line":
      return <line key={key} x1={el.x1} y1={el.y1} x2={el.x2} y2={el.y2} {...common} />;
    case "circle":
      return <circle key={key} cx={el.cx} cy={el.cy} r={el.r} {...common} />;
    case "ellipse":
      return <ellipse key={key} cx={el.cx} cy={el.cy} rx={el.rx} ry={el.ry} {...common} />;
    case "rect":
      return (
        <rect
          key={key}
          x={el.x}
          y={el.y}
          width={el.width}
          height={el.height}
          rx={el.rx}
          ry={el.ry}
          {...common}
        />
      );
    case "path":
      return <path key={key} d={el.d} {...common} />;
    case "polygon":
      return <polygon key={key} points={el.points} {...common} />;
    case "polyline":
      return <polyline key={key} points={el.points} fill="none" {...common} />;
    case "image":
      return (
        <image
          key={key}
          href={el.href}
          x={el.x}
          y={el.y}
          width={el.width}
          height={el.height}
          {...common}
        />
      );
    default:
      return null;
  }
}

export function SVGRenderer({ data }: { data: unknown }) {
  const safe = sanitizeCustomSvg(data);
  if (!safe) {
    return <div className="gc-fallback">Invalid custom visualization</div>;
  }
  const style: CSSProperties = {
    width: "100%",
    height: "100%",
    maxHeight: "100%",
    background: safe.background || "transparent",
  };
  return (
    <svg
      className="gc-svg"
      viewBox={safe.viewBox}
      width={safe.width}
      height={safe.height}
      style={style}
      preserveAspectRatio="xMidYMid meet"
    >
      {safe.elements?.map((el, i) => renderEl(el, i))}
    </svg>
  );
}
