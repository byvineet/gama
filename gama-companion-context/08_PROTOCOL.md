# Gama Companion Protocol v0.1

Transport: WebSocket
Encoding: UTF-8 JSON

## Request

```json
{
  "type": "command",
  "id": "request-id",
  "command": "open_app",
  "args": {
    "app_name": "Chrome",
    "new_window": false
  }
}
```

## Success response

```json
{
  "type": "command_result",
  "id": "request-id",
  "success": true,
  "result": {
    "message": "..."
  }
}
```

## Error response

```json
{
  "type": "command_result",
  "id": "request-id",
  "success": false,
  "error": {
    "code": "UNKNOWN_COMMAND",
    "message": "Unknown command"
  }
}
```

## Authentication

```json
{
  "type": "auth",
  "protocol_version": "0.1",
  "device_id": "android-01",
  "device_name": "Vineet's Phone",
  "credential": "..."
}
```

The exact credential mechanism should be chosen during bridge implementation. Do not hardcode a secret in the Android app.

## Event

```json
{
  "type": "event",
  "event": "gama_state_changed",
  "data": {}
}
```

## Initial commands

```text
get_capabilities
get_desktop_state
chat
open_app
memory_get
memory_set
tasks_list (only if a real task adapter exists)
```

## Heartbeat

```json
{"type":"ping"}
```

```json
{"type":"pong"}
```

## Sync

After authentication/reconnection:

```json
{"type":"sync_request","id":"sync-1"}
```

The server returns a bounded snapshot of current companion-safe state.

## Error codes

```text
AUTH_FAILED
NOT_AUTHENTICATED
INVALID_MESSAGE
INVALID_ARGUMENTS
UNKNOWN_COMMAND
COMMAND_NOT_ALLOWED
COMMAND_FAILED
DEVICE_NOT_PAIRED
DEVICE_REVOKED
UNSUPPORTED_VERSION
INTERNAL_ERROR
UNSUPPORTED_CAPABILITY
```

## Protocol rule

The JSON protocol is the stable boundary. Android must never rely on Python module names, classes or internal Gama objects.
