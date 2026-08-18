/**
 * Floating information cards — temporary, glass-like, compact.
 */
import { useMemo } from "react";
import type { D2Card } from "../core/D2State";
import { deriveGlow } from "../theme/D2Theme";

type Props = {
  cards: D2Card[];
  primaryColor: string;
  onDismiss?: (id: string) => void;
};

export function CardRenderer({ cards, primaryColor, onDismiss }: Props) {
  const glow = useMemo(() => deriveGlow(primaryColor, 0.25), [primaryColor]);

  if (!cards.length) return null;

  return (
    <div className="d2-cards-layer" aria-live="polite">
      {cards.map((card, i) => {
        const angle = card.angle ?? (i / cards.length) * Math.PI * 2;
        const x = 50 + Math.cos(angle) * 32;
        const y = 42 + Math.sin(angle) * 22;
        return (
          <article
            key={card.id}
            className="d2-card"
            style={
              {
                left: `${x}%`,
                top: `${y}%`,
                "--d2-accent": primaryColor,
                "--d2-glow": glow,
                animationDelay: `${i * 40}ms`,
              } as React.CSSProperties
            }
          >
            <header className="d2-card-head">
              <span className="d2-card-kind">{(card.kind || "info").toUpperCase()}</span>
              {onDismiss && (
                <button
                  type="button"
                  className="d2-card-close"
                  aria-label="Dismiss"
                  onClick={() => onDismiss(card.id)}
                >
                  ×
                </button>
              )}
            </header>
            <h3 className="d2-card-title">{card.title}</h3>
            {card.body && <p className="d2-card-body">{card.body}</p>}
            {card.meta && <footer className="d2-card-meta">{card.meta}</footer>}
          </article>
        );
      })}
    </div>
  );
}
