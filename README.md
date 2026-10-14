# OverteInAShell

Overte live link in Blender: use Blender as the scene editor for an
[Overte](https://overte.org) domain. Move, scale, add and delete entities in
Blender and see the change in-world, and the other way round.

> Work in progress. See the pull requests for the build-up.

## Layout

| Path | What it is |
|---|---|
| `overte_live_link/` | The Blender add-on / extension (zip this folder to install) |
| `overte_live_link/wsserver.py` | Dependency-free WebSocket + HTTP server the Overte side connects to |
| `overte_live_link/convert.py` | Blender (Z-up) ⇄ Overte (Y-up) transform conversion |
| `scripts/run_local_domain.sh` | Start/stop a throwaway local Overte domain for development |
| `tests/` | pytest suite (unit tests; `-m overte` ones need a local domain) |

## Development

```bash
pip install -r requirements-dev.txt   # bpy from PyPI = headless Blender for tests
pytest -q -m "not overte"
```

A local domain needs the `overte-server` package (Ubuntu/Debian `.deb` from
https://public.overte.org/build/overte/release/), installed to `/opt/overte`:

```bash
scripts/run_local_domain.sh start
scripts/run_local_domain.sh status
scripts/run_local_domain.sh stop
```
