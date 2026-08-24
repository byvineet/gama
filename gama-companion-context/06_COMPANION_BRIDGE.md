# Gama Companion Bridge

## Goal

Add a thin network adapter to the existing Python Gama process. The bridge is the only component the Android app needs to understand.

## Recommended structure

```text
companion/
  __init__.py
  server.py
  protocol.py
  auth.py
  session.py
  capabilities.py
  adapters/
    commands.py
    state.py
    memory.py
    chat.py
```

Use the repository's existing conventions if another location is more appropriate.

## Responsibilities

### server.py
- Start/stop WebSocket server.
- Accept connections.
- Never block Gama's audio/UI loop.
- Create a session per paired device.

### protocol.py
- Parse/validate JSON messages.
- Serialize only JSON-safe primitives.
- Validate protocol version and request IDs.

### auth.py
- Device pairing/authentication.
- Persistent per-device credential.
- Device revocation.
- Never log credentials.

### session.py
- Track connected devices.
- Heartbeat.
- Reconnection/session state.
- Route requests to adapters.

### capabilities.py
- Explicit companion allowlist.
- Return stable capability metadata.
- Do not expose all 60+ Gama tools automatically.

### adapters/commands.py
- Convert protocol command -> existing Gama tool execution.
- For `open_app`, use the existing `core/tool_dispatch` path rather than calling the action module directly.
- Preserve execution queue, verification, risk and confirmation behavior.

### adapters/state.py
- Subscribe to the EventBus.
- Convert selected events to stable JSON events.
- Provide current state snapshot on connection/sync.

### adapters/memory.py
- Wrap `memory_manager` public methods.
- Keep desktop memory authoritative.

### adapters/chat.py
- Find the actual stable entry point into the current Gama conversation/runtime.
- Do not create a parallel Gemini implementation.
- If the current conversation system cannot safely accept an external text request yet, implement an adapter interface and mark it pending rather than guessing.

## Threading/async rule

Gama contains audio, Qt and asyncio activity. The bridge must not perform long/blocking work on a hot audio or Qt callback. Use a dedicated async loop/thread or the project's existing execution facilities as appropriate.

## First end-to-end milestone

```text
Android
  |
  | open_app Chrome
  v
WebSocket server
  |
  v
commands adapter
  |
  v
core/tool_dispatch._execute_tool(...)
  |
  v
existing ExecutionQueue / capability checks
  |
  v
ToolRegistry -> open_app handler
  |
  v
Chrome
  |
  v
command_result -> Android
```

## Do not do

- Do not expose arbitrary Python.
- Do not expose arbitrary shell commands.
- Do not bypass `ExecutionQueue`/risk checks.
- Do not serialize internal class instances.
- Do not let a companion disconnect crash Gama.
- Do not make Android a second Gama runtime.
