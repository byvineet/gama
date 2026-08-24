# Gama Command Pipeline

## Actual execution path

The important existing modules are:

```text
core/tool_declarations.py
        |
        | tool schema/name
        v
core/tool_dispatch.py
        |
        | _execute_tool(name, args)
        v
core/execution_queue.py
        |
        | retry / verification / outcome handling
        v
CapabilityManager / risk gate
        |
        v
core/tool_registry.py
        |
        | ToolRegistry.dispatch(name, args)
        v
registered handler
        |
        v
actions/* or other subsystem
```

`core/tool_registry.py` documents `ToolRegistry.dispatch(name, args)` as the central name-to-handler dispatch operation. Unknown tools return an `Unknown tool` result and handler exceptions are converted into `Tool failed` results.

`core/tool_dispatch.py` wraps tool execution in performance instrumentation, checks fast-intent deduplication, runs through the shared execution queue, updates working memory, and records tool categories.

## Example: open_app

The existing tool declaration defines `open_app` with:
- `app_name` — required string
- `new_window` — optional boolean

The dispatch binding maps it to:

```python
open_app(args.get("app_name", ""), new_window=bool(args.get("new_window", False)))
```

The Android companion should therefore request the existing tool by name and arguments. It should not implement Windows app discovery or launching itself.

Example companion request:

```json
{
  "type": "command",
  "id": "req-123",
  "command": "open_app",
  "args": {
    "app_name": "Chrome",
    "new_window": false
  }
}
```

The bridge should pass the request into the same safe execution path used by Gama itself. It must not directly import `actions.open_app` and bypass the queue/risk layer.

## Risk model

`core/capability_manager.py` classifies tools into SAFE, LOW, MEDIUM, HIGH and DESTRUCTIVE. MEDIUM/HIGH/DESTRUCTIVE actions can require confidence and user confirmation. The companion must preserve these checks.

## Companion principle

The bridge is an adapter from external protocol -> existing Gama command execution. It is not a second dispatcher.
