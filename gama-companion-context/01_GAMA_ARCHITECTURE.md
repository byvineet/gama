# Gama Architecture — Companion-Relevant View

## 1. Runtime

`main.py` is the desktop entry point. It initializes environment/data paths, logging, diagnostics, performance instrumentation, configuration, memory, wake-word handling, voice components, and the `GamaAssistant` runtime.

`GamaAssistant` is the live assistant object. It owns controllers including:
- `AudioController`
- `SessionController`
- `UIController`
- `SleepController`
- `BargeInController`
- `WakeController`
- `AudioStreamController`
- `LiveSessionController`
- `ToolController`

The Android companion should not depend on these controller classes directly.

## 2. AI / Conversation

Gama uses Gemini Live for the real-time voice/conversation path. Tool declarations are maintained separately in `core/tool_declarations.py`. The companion should submit text/voice-originated user requests into an existing Gama conversation entry point rather than create a second AI personality.

## 3. Tool subsystem

The relevant chain is:

```text
Gemini / fast intent
      |
      v
core/tool_dispatch.py
      |
      v
core/execution_queue.py
      |
      v
CapabilityManager / confidence + risk checks
      |
      v
core/tool_registry.py
      |
      v
registered handler
      |
      v
actions/* or another existing subsystem
```

`core/tool_registry.py` is an O(1) name-to-handler registry. Each `ToolEntry` stores name, handler, risk, description, optional verifier, retryability and category.

`core/tool_dispatch.py` contains the actual handler bindings and `_execute_tool()` wrapper. It also updates working memory and records tool categories after execution.

## 4. Risk/security

`core/capability_manager.py` gates execution. SAFE and LOW tools pass through subject to circuit breaking; MEDIUM/HIGH/DESTRUCTIVE actions are confidence/risk gated. The companion must reuse this path rather than bypass it.

## 5. State

`state_engine/event_bus.py` provides a process-wide, thread-safe publish/subscribe EventBus. It has `subscribe`, `unsubscribe`, and `publish`; events contain `name`, `timestamp`, and arbitrary `data`.

This is the preferred source for companion-safe Gama state/events. The Companion Bridge should subscribe to an explicit allowlist instead of exposing every internal event.

## 6. Memory

`memory/memory_manager.py` provides the persistent memory interface. Memory is JSON-backed and thread-safe. Public operations include:
- `load_memory()`
- `save_memory(memory)`
- `update_memory(new_data)`
- `get_memory(category, key)`
- `set_memory(category, key, value)`
- `format_memory_for_prompt(query=None)`
- `clear_memory()`

Default categories are `identity`, `preferences`, `projects`, `relationships`, `wishes`, and `notes`.

The desktop memory remains authoritative. Android may cache synchronized values locally.

## 7. Existing actions

The repository has many existing action modules. `core/tool_dispatch.py` includes bindings for, among others, `open_app`, `edge_search`, `computer_settings`, `system_info`, `desktop_context`, automation, terminal, keyboard/mouse actions, process management, file operations, memory-related tools, media, browser control, and more.

Do not expose all of them automatically to Android. Create a companion capability allowlist.

## 8. Companion boundary

There is currently no dedicated companion WebSocket layer in the repository. Add a thin adapter rather than modifying the internal command architecture.

Recommended boundary:

```text
Gama core
   |
   +-- ToolRegistry / ToolDispatch
   +-- EventBus
   +-- MemoryManager
   +-- conversation/runtime adapter
   |
Companion Bridge
   |
WebSocket + JSON protocol
   |
Android
```

## 9. Source-of-truth rule

The Python repository is authoritative. These documents describe integration points, not replacement implementations.
