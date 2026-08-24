# Companion Tool API

## Purpose

This is the initial Android-safe capability surface. It is intentionally smaller than Gama's complete tool registry.

## Required v0.1 commands

### open_app

Existing Gama tool.

Arguments:
- `app_name`: string, required
- `new_window`: boolean, optional/default false

Example:

```json
{"type":"command","id":"1","command":"open_app","args":{"app_name":"Chrome","new_window":false}}
```

### get_desktop_state

Adapter over existing desktop/state information. Return only a stable, serializable snapshot useful to Android.

Suggested result fields:
- connection status
- Gama primary/activity state when available
- current timestamp

Do not expose internal Python objects.

### chat

Accepts a user message and sends it through the existing Gama conversation/AI path.

Arguments:
- `message`: string, required
- optional `conversation_id` if the existing conversation layer can support it

The bridge must not create a second Gemini client just for Android unless the architecture later explicitly requires it.

### memory_get

Adapter over `memory.memory_manager.get_memory(category, key)`.

Arguments:
- `category`: string
- `key`: string

### memory_set

Adapter over `memory.memory_manager.set_memory(category, key, value)`.

Arguments:
- `category`: string
- `key`: string
- `value`: string

The bridge should validate category/key/value size before calling the memory manager.

### tasks_list

Expose the existing task/reminder subsystem only after identifying the actual task API in the repository. Do not invent a Python function name. If no stable API is found, report `UNSUPPORTED_CAPABILITY` until an adapter is implemented.

### get_capabilities

Return the explicit companion allowlist and feature flags. This prevents Android from assuming that every desktop Gama tool is remotely callable.

## Do not expose by default

The repository contains higher-risk tools including terminal execution, advanced automation, computer agent, process control, keyboard/mouse control, destructive file actions and system power operations. These must not be exposed to Android merely because they exist in `ToolRegistry`.

If a future companion feature needs one of them, add an explicit capability with the same Gama risk/confirmation model.

## Tool metadata source

`core/tool_registry.py` provides runtime `ToolEntry` metadata and `list_tools()`. `core/tool_declarations.py` contains Gemini-facing schemas. The Companion Bridge should use its own stable allowlist rather than blindly serializing the entire Gemini schema list.
