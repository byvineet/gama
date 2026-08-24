# Master Prompt — Build the Gama Android Companion

You are building the Android companion for an existing Python desktop AI assistant named Gama.

## Critical context rule

You do NOT need the full Gama Python repository to build the Android client. The folder `gama-companion-context/` is a generated architecture/context package derived from the real Gama repository.

Read these files before coding:

1. `00_README.md`
2. `01_GAMA_ARCHITECTURE.md`
3. `02_COMMAND_PIPELINE.md`
4. `03_TOOL_API.md`
5. `04_STATE_AND_EVENTS.md`
6. `05_MEMORY_API.md`
7. `06_COMPANION_BRIDGE.md`
8. `07_ANDROID_ARCHITECTURE.md`
9. `08_PROTOCOL.md`

The documents describe the actual existing integration points. Do not invent a different Gama architecture.

## Goal

Create a lightweight native Android app that connects to the Gama desktop application through the Companion Bridge using the protocol in `08_PROTOCOL.md`.

The Android app should support:
- pairing/authentication
- persistent connection
- reconnection
- connection state
- text chat with Gama
- sending supported Gama commands
- opening desktop applications through Gama
- reading/writing shared Gama memory
- capability discovery
- receiving selected Gama events
- local caching where useful
- a clean path for future voice support
- a clean path for future Gama -> phone commands

## Architecture

```text
Android UI
  |
ViewModel/domain
  |
Repository
  |
WebSocket transport
  |
Gama Companion Bridge
  |
Existing Gama Python systems
```

The Android app must not implement Gemini as a second assistant unless explicitly requested later.

## First milestone

Make this exact flow work:

```text
Android
  -> authenticate
  -> get_capabilities
  -> open_app("Chrome")
  -> receive command_result
```

Then implement:

```text
Android
  -> chat("Hello Gama")
  -> existing Gama conversation system
  -> response
```

Then:

```text
Android
  -> memory_set
  -> Gama memory_manager
  -> memory_get
```

## Do not bypass Gama

The existing Gama command path is important:

```text
core/tool_dispatch.py
 -> core/execution_queue.py
 -> capability/risk checks
 -> core/tool_registry.py
 -> registered handler
```

The future desktop Companion Bridge must preserve this path. The Android app only sees the JSON protocol.

## Android technology

Use Kotlin + Jetpack Compose + Coroutines. Use a lightweight WebSocket client. Use DataStore for small persistent settings. Add Room only if a structured cache actually needs it.

Do not add heavy dependencies without justification.

## UI

Create a lightweight premium companion UI, not a copy of the desktop HUD.

Initial navigation:
- Home
- Chat
- Commands
- Memory
- Settings

Show connection status prominently but unobtrusively.

## Security

Never expose arbitrary Python, shell, filesystem or unrestricted Android execution.

Only execute commands explicitly exposed by `get_capabilities`.

Never log credentials or memory values.

## Unknown/missing Gama implementation

If you need to know how an Android feature maps to Python, consult the context documents. If they do not define a stable mapping, do NOT invent one. Create a clean interface and mark the desktop integration as pending.

## Scope discipline

Do not build everything at once.

Implement and test in this order:

1. project skeleton
2. protocol models
3. WebSocket connection
4. authentication state
5. get_capabilities
6. open_app command UI
7. command result handling
8. chat
9. memory
10. event/state synchronization
11. phone-command framework
12. voice

## Expected deliverable

Produce a buildable Android project with clear separation between:
- transport
- protocol models
- repository
- domain
- UI
- local persistence

Include a short `README` explaining how to run the Android client against the Gama Companion Bridge.

Do not claim the app can connect to Gama until the protocol/client implementation is actually complete.
