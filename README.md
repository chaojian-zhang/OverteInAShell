# OverteInAShell

Overte live link in Blender: use Blender as the scene editor for an
[Overte](https://overte.org) domain. Move, scale, add and delete entities in
Blender and see the change in-world, and the other way round.

## How it works

```
Blender (add-on)                         Overte domain
┌──────────────────────────┐   WebSocket   ┌───────────────────────────────┐
│ Link server :9420        │◄──────────────│ overte_bridge.js              │
│  · serves the bridge .js │──── HTTP ────►│  persistent script (agent)    │
│  · syncs scene ⇄ entities│               │  or Interface client script   │
└──────────────────────────┘               └───────────────────────────────┘
```

The add-on runs a small WebSocket + HTTP server. The bridge script runs inside
Overte, connects out to it, streams entity changes to Blender and applies
Blender's edits with the normal `Entities` API, so permissions, locks and
other users all behave as they would in-world.

## Quick start

1. Zip the `overte_live_link/` folder and install it in Blender 4.2+
   (*Edit → Preferences → Get Extensions → Install from Disk*), or symlink it
   into your add-ons directory.
2. In the 3D Viewport sidebar (**N**), open the **Overte** tab and press
   **Start Live Link**.
3. Run the bridge in Overte, either:
   * **as a domain Persistent Script** (always on, needs domain admin): domain
     settings → *Content → Scripts → Persistent Scripts*, add
     `http://<blender-machine>:9420/overte_bridge.js`. If the domain is on another
     machine, set *Listen address* to `0.0.0.0` before starting; or
   * **in Interface** (your own permissions): *Edit → Running Scripts → From URL*,
     `http://127.0.0.1:9420/overte_bridge.js`.
4. The domain's entities appear in Blender's **Overte** collection. Edit away.

### What syncs

| Overte | Blender | Direction |
|---|---|---|
| position, rotation, dimensions | object transform (scale = size) | both |
| parent | parent | both |
| name | object name | both |
| color (Box, Sphere, Shape) | object material colour | both |
| Light: color, intensity, falloff radius, spot cutoff/exponent | light data (100 W = intensity 1) | both |
| visible | render visibility (camera icon) | both |
| locked | location/rotation/scale locks | both |
| create / delete | add (Add → Overte Entity or sidebar menu) / delete | both |
| Shift+D duplicate | clones the entity with all its properties | Blender → Overte |

Box, Sphere and Shape entities appear as unit meshes; lights as lights; other
types (Zone, Model, Text, …) as cube empties sized to their dimensions.
Properties the add-on doesn't map are left untouched in Overte.

## Layout

| Path | What it is |
|---|---|
| `overte_live_link/` | The Blender add-on / extension |
| `overte_live_link/bridge/overte_bridge.js` | The script that runs inside Overte |
| `overte_live_link/scene_sync.py` | Scene ⇄ entity sync (per-field three-way merge) |
| `overte_live_link/session.py` | Server, bridge selection, request routing (no `bpy`) |
| `overte_live_link/wsserver.py` | Dependency-free WebSocket + HTTP server |
| `overte_live_link/convert.py` | Blender (Z-up) ⇄ Overte (Y-up) conversion |
| `scripts/run_local_domain.sh` | Start/stop a throwaway local Overte domain |
| `tests/` | Unit tests, plus end-to-end tests (`-m overte`) against a real domain |

## Development

```bash
pip install -r requirements-dev.txt   # bpy from PyPI = headless Blender for tests
pytest -q -m "not overte"             # unit tests
pytest -q -m overte                   # end-to-end, needs the Overte server
```

The end-to-end tests need the `overte-server` package (Ubuntu/Debian `.deb`
from https://public.overte.org/build/overte/release/) in `/opt/overte`. They
start a fresh local domain running the bridge plus an independent "probe"
agent that checks what the entity server really holds.

```bash
scripts/run_local_domain.sh start http://127.0.0.1:9420/overte_bridge.js
scripts/run_local_domain.sh status
scripts/run_local_domain.sh stop
```
