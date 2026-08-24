# Gama State and Events

## EventBus

`state_engine/event_bus.py` contains a process-wide singleton `event_bus`.

Core API:

```python
event_bus.subscribe(event_name, callback)
event_bus.unsubscribe(event_name, callback)
event_bus.publish(event_name, **data)
```

Events are represented by an `Event` dataclass:
- `name`
- `timestamp`
- `data`

The bus is thread-safe and callbacks are isolated from publisher failures.

## Companion event adapter

The bridge should subscribe to an allowlist. Never forward wildcard `*` events to Android in production.

Initial useful event classes should include only events that are confirmed in the current repository and useful externally, for example:

```text
command started/completed
speech started/finished/interrupted
thinking/processing state
Gama state changes
memory updates
```

Before implementing each event, inspect the actual publisher in the repository and define a stable JSON payload. Internal event payloads must not be forwarded as arbitrary Python objects.

## State

`state_engine` contains the state manager and typed event/state infrastructure. Android should receive a serialized snapshot, not a reference to the internal state manager.

Suggested external representation:

```json
{
  "gama_state": "READY",
  "activity": "IDLE",
  "timestamp": 0
}
```

The exact enum/string values must be derived from the current source at bridge implementation time.

## Reconnection

After a companion reconnects, the bridge should send a fresh state/sync snapshot rather than attempting to replay every internal event.
