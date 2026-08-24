# Gama Companion Context

This directory is an AI-readable architecture package for building the Gama Android Companion without requiring an AI coding environment to ingest the entire Python repository.

## Source of truth
The real Gama implementation is the repository root. These documents describe the parts of that implementation relevant to a companion client.

## Core integration path

```text
Android Companion
      |
      | WebSocket / JSON protocol
      v
Companion Bridge (new adapter layer)
      |
      +--> core/tool_dispatch.py
      |       |
      |       +--> core/execution_queue.py
      |       +--> core/capability_manager.py
      |       +--> core/tool_registry.py
      |       +--> registered action handlers
      |
      +--> state_engine/event_bus.py
      |
      +--> memory/memory_manager.py
      |
      +--> existing Gama conversation/runtime
```

## Important rule
The Android app must not import or reproduce Gama's Python implementation. It communicates only with the Companion Bridge contract.

## Documents
- `01_GAMA_ARCHITECTURE.md` — actual repository architecture and responsibilities.
- `02_COMMAND_PIPELINE.md` — actual tool execution path.
- `03_TOOL_API.md` — companion-safe command surface and existing tool metadata.
- `04_STATE_AND_EVENTS.md` — EventBus/state integration.
- `05_MEMORY_API.md` — actual persistent memory API.
- `06_COMPANION_BRIDGE.md` — implementation design for the new bridge.
- `07_ANDROID_ARCHITECTURE.md` — Android-side architecture.
- `08_PROTOCOL.md` — stable JSON/WebSocket contract.
- `MASTER_AI_STUDIO_PROMPT.md` — prompt for an AI coding agent that cannot inspect the Python repository directly.

## Verified repository facts
- Entry point: `main.py`.
- `main.py` constructs `GamaAssistant` and uses controllers for audio, session, UI, sleep, barge-in, wake, live session and tool control.
- Tool dispatch was extracted into `core/tool_dispatch.py`.
- Tool registration is centralized in `core/tool_registry.py`.
- Tool declarations are in `core/tool_declarations.py`.
- Tool execution passes through `core/execution_queue.py` and capability/risk checks.
- Process-wide events are provided by `state_engine/event_bus.py`.
- Persistent long-term memory is provided by `memory/memory_manager.py`.
- The repository currently has no companion WebSocket layer; that is the new integration boundary to add.

## Do not infer
Do not invent a different Gama architecture from these documents. If an implementation detail conflicts with the actual source, the source wins.
