# Gama Nexus

Gama’s **visual output channel** (replaces the permanent voice orb).

Think of it as:

- **VOICE** = Gama’s audio output  
- **CANVAS** = Gama’s visual output  

This is **not** a fixed dashboard. Gama decides what to show, composes built-in components, and can invent custom scenes from SVG primitives.

## Architecture

```
USER → Gemini Live → Tool / Router → Display Controller
                                         │
                    ┌────────────────────┴────────────────────┐
                    │                                         │
              Built-in scene                           Custom scene
                    │                                         │
                    │                              Gemini 3.1 Flash-Lite
                    │                                         │
                    │                                   Canvas DSL
                    │                                         │
                    │                              Pydantic validation
                    │                                         │
                    └────────────────────┬────────────────────┘
                                         ▼
                                   Scene Manager
                                         ▼
                                  React Renderer
                                         ▼
                                    GAMA CANVAS
```

- **Gemini Live** coordinates; it does **not** emit React/HTML/JS.
- **Gemini 3.1 Flash-Lite** is used only for genuinely custom visuals (`canvas_visual` tool).
- **Pydantic** (`actions/visual_schema.py`) validates every custom scene before it reaches React.
- **React** renders validated DSL only — never `eval`, never arbitrary event handlers.

## Protocol (WebSocket)

```json
{
  "channel": "display",
  "action": "show",
  "scene": {
    "id": "weather-main",
    "type": "weather",
    "layer": 1,
    "data": { "location": "Orai", "temperature": 31, "condition": "Partly cloudy" },
    "transition": { "enter": "fade", "exit": "dissolve", "duration": 300 }
  }
}
```

### Actions
`show` · `update` · `replace` · `remove` · `clear` · `push` · `pop` · `animate`

### Scene types
`idle` · `weather` · `tasks` · `goals` · `reminders` · `alerts` · `timer` · `pomodoro` · `system` · `status` · `information` · `list` · `chart` · `progress` · `confirm` · `image` · `custom_svg` · `dsl` · `scene` · `compose` · …

### Layers
0 ambient/idle · 1 main · 2 info · 3 alerts · 4 overlays

## Built-in vs custom

| Request | Path |
|--------|------|
| “Show today’s tasks” | `display_stage` → native `Tasks` component (no extra LLM) |
| “Show weather + goals” | native composition |
| “Create a futuristic radar of my processes” | `canvas_visual` → Flash-Lite → Pydantic → `custom_svg` |

## Custom SVG (safe, no JS)

```json
{
  "id": "hud-1",
  "type": "custom_svg",
  "data": {
    "viewBox": "0 0 1000 600",
    "elements": [
      { "type": "circle", "cx": 500, "cy": 300, "r": 120, "stroke": "#38bdf8", "fill": "none", "strokeWidth": 2 },
      { "type": "text", "x": 500, "y": 160, "text": "SYSTEM", "textAnchor": "middle", "fill": "#7dd3fc", "fontSize": 22 }
    ]
  }
}
```

Allowed primitives: `g`, `text`, `line`, `circle`, `ellipse`, `rect`, `path`, `polygon`, `polyline`, `image`

## Python helpers

```python
from actions.display_stage import canvas_show, canvas_clear, canvas_custom_svg, canvas_command
from actions.canvas_visual import generate_and_show

canvas_show({
  "id": "weather-main",
  "type": "weather",
  "data": {"location": "Orai", "temperature": 31, "condition": "Partly cloudy"},
})

canvas_clear()

# Creative custom visual (Flash-Lite + Pydantic)
generate_and_show("Create a minimal futuristic system monitor for CPU and RAM")
```

Legacy `push_display({mode: "weather", ...})` is dual-written to this protocol automatically.

## Security

- Model output never executes.
- Pydantic rejects unknown primitives, scripts, and unsafe `href`s.
- React `SVGValidator` is a second defense-in-depth pass.
- Element count, text length, and depth are capped.
- Malformed AI output cannot crash the HUD.
