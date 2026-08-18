# Gama Rive Avatar

Place your file at: `web_ui/public/avatar.riv`

## Recommended State Machine inputs

| Name | Type | Purpose |
|------|------|---------|
| `talk` or `mouth` | Number 0–100 (or 0–1) | Mouth open amount from TTS amplitude |
| `isTalking` | Boolean | Optional speaking flag |
| `blink` | Trigger or Boolean | Auto-fired every ~2–5s |

## Quick setup in Rive Editor

1. Draw a simple face (or import illustration).
2. Create mouth shapes: closed → open (timeline or blend).
3. Create a blink animation (eyes scale Y → 0 → 1).
4. State Machine:
   - Number input `talk` drives mouth blend/open.
   - Trigger `blink` plays blink one-shot on a layer.
5. Export `.riv` → save as `web_ui/public/avatar.riv`.

Gama maps live TTS amplitude → `talk` while `speaking` is true.
