# G.A.M.A Tests

## Run locally

```bash
pip install -r requirements-dev.txt
export PYTHONPATH=.
export GAMA_DATA=/tmp/gama_data
pytest tests/ -q --timeout=60 -m "not windows and not network"
```

## Markers

| Marker | Meaning |
|---|---|
| `integration` | Cross-module flows (registry + plugins, etc.) |
| `windows` | Needs Windows APIs / GUI |
| `network` | Needs network |
| `slow` | Longer-running |

## Notes

- Heaviest suites (protocol_system) can be skipped with `--ignore` if needed.
- Coverage currently focuses on core plumbing (registry, paths, protocols,
  notifications). memory/, security/, automation/, knowledge/, music/,
  learning/ have no tests yet.
