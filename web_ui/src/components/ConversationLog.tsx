import { FormEvent, useEffect, useRef, useState } from "react";
import type { LogEntry } from "../types/gama";

function stripEmoji(s: string) {
  return s
    .replace(
      /[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}\u{FE00}-\u{FE0F}\u{200D}]/gu,
      "",
    )
    .replace(/\s{2,}/g, " ")
    .trim();
}

type Props = {
  entries: LogEntry[];
  onSend?: (text: string) => void;
  connected?: boolean;
};

export function ConversationLog({ entries, onSend, connected }: Props) {
  const listRef = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);
  const [draft, setDraft] = useState("");

  const clean = (entries || [])
    .map((e) => ({ ...e, text: stripEmoji(e.text || "") }))
    .filter((e) => {
      const role = String(e.role || "").toLowerCase();
      return e.text && (role === "user" || role === "gama");
    });

  const onScroll = () => {
    const el = listRef.current;
    if (!el) return;
    stickToBottom.current =
      el.scrollHeight - el.scrollTop - el.clientHeight < 48;
  };

  useEffect(() => {
    const el = listRef.current;
    if (!el || !stickToBottom.current) return;
    el.scrollTop = el.scrollHeight;
  }, [clean.length]);

  const submit = (e?: FormEvent) => {
    e?.preventDefault();
    const t = draft.trim();
    if (!t || !onSend) return;
    onSend(t);
    setDraft("");
  };

  return (
    <div className="conv-root">
      <div className="conv-header">
        <span className="conv-title">CONVERSATION</span>
        <span className="conv-count">{clean.length}</span>
      </div>

      <div
        ref={listRef}
        onScroll={onScroll}
        className="conversation-scroll conv-list"
      >
        {clean.length === 0 && (
          <p className="conv-empty">Say something or type below…</p>
        )}
        {clean.map((e, i) => {
          const isUser = String(e.role).toLowerCase() === "user";
          return (
            <div
              key={`${e.ts ?? i}-${i}`}
              className={`conv-bubble ${isUser ? "user" : "gama"}`}
            >
              <div className="conv-role">{isUser ? "YOU" : "G A M A"}</div>
              <div className="conv-text">{e.text}</div>
            </div>
          );
        })}
      </div>

      <form className="conv-input-row" onSubmit={submit}>
        <input
          className="conv-input"
          value={draft}
          onChange={(ev) => setDraft(ev.target.value)}
          placeholder="Type or speak…"
          autoComplete="off"
          disabled={connected === false}
        />
        <button
          type="submit"
          className="conv-send"
          title="Send"
          disabled={!draft.trim() || connected === false}
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M12 19V5M5 12l7-7 7 7" />
          </svg>
        </button>
      </form>
    </div>
  );
}
