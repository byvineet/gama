# Gama Memory API

## Source

`memory/memory_manager.py` is the existing long-term memory implementation.

## Storage

Memory is JSON-backed at the Gama base directory under:

```text
memory/long_term.json
```

The manager uses a process lock and atomic replacement when saving.

## Categories

The default memory schema contains:

```text
identity
preferences
projects
relationships
wishes
notes
```

## Public functions

```python
load_memory() -> dict
save_memory(memory: dict) -> None
update_memory(new_data: dict) -> None
get_memory(category: str, key: str) -> Optional[str]
set_memory(category: str, key: str, value: str) -> None
format_memory_for_prompt(query: Optional[str] = None) -> str
clear_memory() -> None
```

Values are capped by the current manager (`MAX_VALUE_LENGTH = 380`). The bridge must not bypass these constraints.

## Android model

Desktop Gama remains authoritative. Android should have a small local cache for display/offline use, but writes must go through the bridge:

```text
Android local cache
        |
        v
Companion Bridge
        |
        v
memory_manager.set_memory()
```

For synchronization, the bridge can send serialized memory entries or a bounded snapshot. Do not copy the entire memory database to the phone unless the user explicitly needs that feature.

## Privacy

Memory may contain personal information. The bridge should authenticate the device and expose only the minimum memory operations required. Avoid logging memory values.
