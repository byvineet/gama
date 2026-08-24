# Android Companion Architecture

## Role

The Android app is a lightweight client, not a port of Gama.

Recommended stack:
- Kotlin
- Jetpack Compose
- Coroutines
- WebSocket client
- DataStore for settings/credentials metadata
- Room only if structured local cache becomes necessary

Keep dependencies minimal.

## Layers

```text
Compose UI
   |
ViewModel
   |
Domain/use cases
   |
Repository
   |\
   | +--> WebSocket transport
   |
   +----> local cache
```

## Initial screens

- Home/connection
- Chat
- Commands
- Memory
- Settings/pairing

Do not replicate the desktop HUD.

## Connection lifecycle

```text
DISCONNECTED
   -> CONNECTING
   -> AUTHENTICATING
   -> CONNECTED
   -> SYNCING
   -> READY
```

On network loss, reconnect with bounded exponential backoff.

## Android -> Gama

Use the protocol in `08_PROTOCOL.md`. The app sends typed commands such as `chat`, `open_app`, `memory_get`, and `memory_set`.

## Gama -> Android

The app receives:
- command results
- Gama state events
- streamed response events when implemented
- memory/task updates
- explicit phone-action requests

## Phone actions

Desktop-originated phone actions must be an explicit Android capability. Validate them on Android. Never treat a desktop message as arbitrary code.

## Voice

Voice can be added after text/chat is stable. The protocol should be extensible for audio/voice messages without coupling the Android app to Gama's desktop wake-word implementation.

## Offline

Display cached state/chat/tasks where useful. Operations requiring Gama should show unavailable/offline status instead of pretending they executed.
