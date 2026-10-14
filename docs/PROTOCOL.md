# Blender ⇄ bridge protocol (version 1)

The Blender add-on is the server. The bridge script (`overte_bridge.js`)
connects to `ws://<host>:<port>/ws?token=<token>` and both sides exchange JSON
text messages. The only binary frames carry file content for `upload` and
`fetched`. Entity properties use Overte's own names and units
(`position {x,y,z}`, `rotation {x,y,z,w}`, `color {red,green,blue}`, …).

`GET /overte_bridge.js?token=<token>` on the same port returns the bridge
script with the WebSocket URL filled in. A wrong or missing token gets
`403` on both the script and the WebSocket.

## Bridge → Blender

| `type` | Fields | Meaning |
|---|---|---|
| `hello` | `version`, `mode` (`agent`/`interface`), `canRez`, `canAdjustLocks`, `canWriteAssets` | First message after connecting. The first bridge to say hello becomes *active*; others wait on standby and take over if it leaves. |
| `changes` | `upserts` (entity property objects incl. `id`, `type`, `lastEdited`), `deletes` (ids), `full`, `ids` | Entities that changed since the last report. With `full: true` it is a snapshot and `ids` lists every entity. |
| `added` | `req`, `items: [{tmp, id, props}]` | Reply to `add`. `id` is `null` if Overte refused. |
| `edited` | `req`, `results: [{id, ok, props}]` | Reply to `edit`. When `ok` is false, `props` is the entity's real state, so Blender can revert. |
| `uploaded` | `req`, `url`, `hash`, `error` | Reply to `upload`. |
| `fetched` | `req`, `size`, `error` | Reply to `fetch`, followed by one binary frame with the content when `error` is null. |
| `pong` | `req` | Reply to `ping`. |
| `error` | `req`, `message`, `about` | A command failed. |

## Blender → bridge

| `type` | Fields | Meaning |
|---|---|---|
| `configure` | `center`, `radius`, `pollMs` | Region to watch and minimum poll interval. |
| `sync` | — | Request a full snapshot. The bridge answers once its entity tree has stopped changing for a moment, so entities still arriving after connecting are not reported missing. |
| `edit` | `edits: [{id, props}]` | Edit entities. `position`/`rotation` are world-space. |
| `add` | `items: [{tmp, props, cloneOf?}]` | Create entities. With `cloneOf`, the source entity's properties are copied first and `props` override them. |
| `delete` | `ids` | Delete entities. |
| `upload` | `req`, `path`, `size` | The next binary frame is stored on the asset server at `path` (`Assets.putAsset`). |
| `fetch` | `req`, `url` | Download an `atp:` asset (`Assets.getAsset`). |
| `ping` | `req` | Liveness check. |

## Bridge behaviour worth knowing

* Change detection polls `lastEdited`; Overte has no "entity edited" signal.
  The poll interval adapts: the next poll waits at least three times as long
  as the last one took.
* Edits the bridge makes itself update its `lastEdited` record, so they are
  not echoed back to Blender.
* `Entities.editEntity` converts world `position`/`rotation` to local using
  the `parentID` *in the edit*. The bridge adds the entity's current parent
  to transform edits.
* The entity server rejects edits to locked entities, and applies only the
  lock change when unlocking. The bridge rejects edits to locked entities
  itself, and splits unlock-and-edit into two edits.
* An agent's entity tree can drop an entity for about a second after an
  edit. A deletion is reported only after the entity has been gone for 4 s.
