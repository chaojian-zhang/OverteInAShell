"""Two-way sync between Blender objects and Overte entities.

Each linked object stores its entity id in the ``overte_id`` custom property
(so links survive saving, undo and renames). For every linked entity we keep a
``Record`` holding, per *field*, the last value both sides agreed on:

* ``local``  - the field as computed from the Blender object at agreement time
* ``remote`` - the field as last reported by (or sent to) Overte

A local change is a field whose current value differs from ``local``; a remote
change is a field whose incoming value differs from ``remote``. Echoes of our
own edits therefore match ``remote`` and are ignored, and edits made on one
side never overwrite unrelated fields edited on the other.

Fields: ``transform`` (position, rotation, dimensions together), ``name``,
``parentID``, ``visible`` (render visibility), ``locked`` (transform locks),
``color``, light settings, ``modelURL``, ``text``.
"""

from __future__ import annotations

import math
import time
import uuid
from typing import Optional

import bmesh
import bpy
from mathutils import Matrix, Quaternion, Vector

from . import convert as cv

P_ID = "overte_id"
P_TMP = "overte_tmp"
P_TYPE = "overte_type"
P_SHAPE = "overte_shape"
P_BASE = "overte_base"
P_CENTER = "overte_center"
P_URL = "overte_model_url"
P_TEXT = "overte_text"
P_REG = "overte_registration"   # entity registrationPoint, when not the centre

NULL_ID = "{00000000-0000-0000-0000-000000000000}"
DEFAULT_REG = (0.5, 0.5, 0.5)
COLLECTION = "Overte"
MESH_PREFIX = "OverteUnit_"

# Overte light intensity 1.0 ~ a 100 W Blender point light.
LIGHT_WATTS_PER_INTENSITY = 100.0
# Ignore remote transform updates this long after sending one (in-flight echoes).
AUTHORITY_WINDOW = 0.75

COLOR_TYPES = {"Box", "Sphere", "Shape", "Light"}
PRIMITIVE_TYPES = {"Box", "Sphere", "Shape"}
DEFAULT_SHAPE = {"Box": "Cube", "Sphere": "Sphere"}


def norm_id(v) -> str:
    if not v:
        return NULL_ID
    s = str(v)
    if not s.startswith("{"):
        s = "{" + s + "}"
    return s.lower()


# --------------------------------------------------------------- equality
def field_equal(field: str, a, b) -> bool:
    if a is None or b is None:
        return a is b
    if field == "transform":
        return (cv.close(a[0], b[0], 1e-4) and cv.quat_close(a[1], b[1])
                and cv.close(a[2], b[2], 1e-4))
    if field in ("intensity", "falloffRadius", "cutoff", "exponent", "lineHeight"):
        return abs(a - b) <= 1e-3 * max(1.0, abs(a))
    if field == "color":
        return all(abs(x - y) <= 1 for x, y in zip(a, b))
    return a == b


def _vec(d, default=(0.0, 0.0, 0.0)):
    return cv.json_vec(d, default)


def remote_fields(props: dict) -> dict:
    """Overte entity properties -> comparable field values."""
    f = {}
    if "position" in props or "rotation" in props or "dimensions" in props:
        rot = cv.q_canonical(cv.json_quat(props.get("rotation")))
        dims = _vec(props.get("dimensions"), (1.0, 1.0, 1.0))
        reg = _vec(props.get("registrationPoint"), DEFAULT_REG)
        # Transforms are compared and stored as box centres.
        f["transform"] = (cv.position_to_center(_vec(props.get("position")), rot, dims, reg), rot, dims)
    if "name" in props:
        f["name"] = props.get("name") or ""
    if "parentID" in props:
        f["parentID"] = norm_id(props.get("parentID"))
    if "visible" in props:
        f["visible"] = bool(props["visible"])
    if "locked" in props:
        f["locked"] = bool(props["locked"])
    etype = props.get("type")
    if "color" in props and (etype in COLOR_TYPES or etype is None):
        c = props.get("color") or {}
        f["color"] = (int(c.get("red", 255)), int(c.get("green", 255)), int(c.get("blue", 255)))
    if etype == "Light" or (etype is None and "intensity" in props):
        for k in ("intensity", "falloffRadius", "cutoff", "exponent"):
            if k in props:
                f[k] = float(props[k])
        if "isSpotlight" in props:
            f["isSpotlight"] = bool(props["isSpotlight"])
    if "modelURL" in props and (etype in ("Model", None)):
        f["modelURL"] = props.get("modelURL") or ""
    if "text" in props and (etype in ("Text", None)):
        f["text"] = props.get("text") or ""
    return f


def fields_to_props(fields: dict, registration=None) -> dict:
    """Comparable field values -> Overte properties (for edit/add).

    ``registration`` is the entity's registrationPoint (default centre)."""
    p = {}
    reg = tuple(registration) if registration is not None else DEFAULT_REG
    for k, v in fields.items():
        if k == "transform":
            center, rot, dims = v
            p["position"] = cv.vec_json(cv.center_to_position(center, rot, dims, reg))
            p["rotation"] = cv.quat_json(rot)
            p["dimensions"] = cv.vec_json(dims)
        elif k == "color":
            p["color"] = {"red": v[0], "green": v[1], "blue": v[2]}
        else:
            p[k] = v
    return p


class Record:
    __slots__ = ("id", "type", "obj_name", "local", "remote", "sent_at")

    def __init__(self, eid: str, etype: str, obj_name: str):
        self.id = eid
        self.type = etype
        self.obj_name = obj_name
        self.local: dict = {}
        self.remote: dict = {}
        self.sent_at = 0.0


class SceneSync:
    def __init__(self, scene: Optional[bpy.types.Scene] = None, sync_deletes: bool = True,
                 radius: float = 16384.0, poll_ms: int = 100, log=print):
        self.scene_name = (scene or bpy.context.scene).name
        self.sync_deletes = sync_deletes
        self.radius = radius
        self.poll_ms = poll_ms
        self.log = log
        self.records: dict[str, Record] = {}
        self.remote_ids: set[str] = set()
        self.missing_ids: set[str] = set()   # linked in Blender, absent in the domain
        self.pending_adds: dict[str, float] = {}  # tmp id -> time sent
        self.synced = False                   # first full snapshot received
        self.stats = {"sent": 0, "received": 0}
        self.on_change = None                 # optional UI refresh callback
        self.session = None                   # set by LinkSession.attach
        from .models import ModelManager
        self.models = ModelManager(self)

    def attach(self, session) -> None:
        self.session = session

    @property
    def scene(self) -> bpy.types.Scene:
        return bpy.data.scenes.get(self.scene_name) or bpy.context.scene

    # ------------------------------------------------------------ session
    def on_bridge_connected(self, hello: dict) -> list:
        self.synced = False
        return [
            {"type": "configure", "center": {"x": 0, "y": 0, "z": 0}, "radius": self.radius,
             "pollMs": self.poll_ms},
            {"type": "sync"},
        ]

    def on_bridge_disconnected(self) -> None:
        self.synced = False
        self.pending_adds.clear()

    def request_resync(self) -> list:
        self.synced = False
        return [{"type": "sync"}]

    # ------------------------------------------------------------- lookup
    def _index(self) -> tuple[dict, list]:
        """id -> object for linked objects; also returns extra objects sharing an id."""
        by_id: dict[str, bpy.types.Object] = {}
        dupes = []
        for obj in self.scene.objects:
            eid = obj.get(P_ID)
            if not eid:
                continue
            eid = norm_id(eid)
            prev = by_id.get(eid)
            if prev is None:
                by_id[eid] = obj
                continue
            # Two objects carry the same id: the one the record knows is the
            # original, the other was duplicated in Blender.
            rec = self.records.get(eid)
            if rec and obj.name == rec.obj_name:
                by_id[eid] = obj
                dupes.append((eid, prev))
            else:
                dupes.append((eid, obj))
        return by_id, dupes

    def object_for(self, eid: str) -> Optional[bpy.types.Object]:
        eid = norm_id(eid)
        rec = self.records.get(eid)
        if rec:
            obj = self.scene.objects.get(rec.obj_name)
            if obj is not None and norm_id(obj.get(P_ID)) == eid:
                return obj
        for obj in self.scene.objects:
            if obj.get(P_ID) and norm_id(obj[P_ID]) == eid:
                return obj
        return None

    # ------------------------------------------------------- local state
    def _base_center(self, obj):
        base = tuple(obj.get(P_BASE, (1.0, 1.0, 1.0)))
        center = tuple(obj.get(P_CENTER, (0.0, 0.0, 0.0)))
        return base, center

    def local_fields(self, obj, etype: str) -> dict:
        loc, rot, scale = obj.matrix_world.decompose()
        base, center = self._base_center(obj)
        t = cv.object_to_entity(tuple(loc), tuple(rot), tuple(scale), base, center, etype)
        f = {
            "transform": (_vec(t["position"]), cv.json_quat(t["rotation"]), _vec(t["dimensions"])),
            "name": obj.name,
            "parentID": norm_id(obj.parent.get(P_ID)) if obj.parent is not None and obj.parent.get(P_ID) else NULL_ID,
            # Render visibility (camera icon) maps to Overte "visible"; viewport
            # hiding (H) stays a Blender-only working aid.
            "visible": not obj.hide_render,
            # Overte "locked" <-> all of Blender's transform locks.
            "locked": all(obj.lock_location) and all(obj.lock_rotation) and all(obj.lock_scale),
        }
        f["transform"] = (f["transform"][0], cv.q_canonical(f["transform"][1]), f["transform"][2])
        if etype == "Light" and obj.type == "LIGHT":
            light = obj.data
            f["color"] = tuple(cv.color_to_json(light.color).values())
            f["intensity"] = light.energy / LIGHT_WATTS_PER_INTENSITY
            f["falloffRadius"] = float(light.shadow_soft_size)
            f["isSpotlight"] = light.type == "SPOT"
            if light.type == "SPOT":
                f["cutoff"] = math.degrees(light.spot_size) / 2.0
                blend = min(max(light.spot_blend, 0.0), 0.99)
                f["exponent"] = blend / (1.0 - blend)
        elif etype in COLOR_TYPES:
            col = self._object_color(obj)
            if col is not None:
                f["color"] = tuple(cv.color_to_json(col).values())
        if etype == "Model":
            f["modelURL"] = str(obj.get(P_URL, ""))
        if etype == "Text":
            f["text"] = str(obj.get(P_TEXT, ""))
        return f

    def _object_color(self, obj):
        mat = obj.active_material
        if mat is None:
            return None
        node = _principled(mat)
        if node is not None:
            return tuple(node.inputs["Base Color"].default_value)[:3]
        return tuple(mat.diffuse_color)[:3]

    # ------------------------------------------------------ outgoing
    def collect_outgoing(self) -> list:
        self.models.tick()
        if not self.synced:
            return []
        now = time.time()
        # Make matrix_world current for objects changed since the last evaluation
        # (cheap when nothing is dirty).
        try:
            bpy.context.view_layer.update()
        except Exception:
            pass
        by_id, dupes = self._index()
        edits, adds, deletes = [], [], []

        # Rename tracking: the object carrying an id is authoritative.
        for eid, rec in self.records.items():
            obj = by_id.get(eid)
            if obj is not None:
                rec.obj_name = obj.name

        for eid, rec in list(self.records.items()):
            obj = by_id.get(eid)
            if obj is None:
                del self.records[eid]
                if self.sync_deletes:
                    deletes.append(eid)
                    self.remote_ids.discard(eid)
                continue
            cur = self.local_fields(obj, rec.type)
            changed = {k: v for k, v in cur.items() if not field_equal(k, v, rec.local.get(k))}
            if not changed:
                continue
            if "parentID" in changed:
                changed["transform"] = cur["transform"]  # world transform must accompany reparenting
            if "transform" in changed:
                rec.sent_at = now
            if "modelURL" in changed:
                self.models.maybe_load(obj, changed["modelURL"])
            rec.local.update(changed)
            rec.remote.update(changed)
            edits.append({"id": eid, "props": fields_to_props(changed, obj.get(P_REG))})

        # Objects duplicated in Blender become clones of the original entity.
        for src_id, obj in dupes:
            tmp = str(uuid.uuid4())
            del obj[P_ID]
            obj[P_TMP] = tmp
            etype = obj.get(P_TYPE, "Box")
            adds.append({"tmp": tmp, "cloneOf": src_id,
                         "props": fields_to_props(self.local_fields(obj, etype), obj.get(P_REG))})
            self.pending_adds[tmp] = now

        # New objects created with the add-on's operators.
        for obj in self.scene.objects:
            tmp = obj.get(P_TMP)
            if not tmp or obj.get(P_ID) or tmp in self.pending_adds:
                continue
            etype = obj.get(P_TYPE, "Box")
            props = fields_to_props(self.local_fields(obj, etype))
            props["type"] = etype
            if P_REG in obj:
                del obj[P_REG]  # new entities use the default (centre)
            if obj.get(P_SHAPE):
                props["shape"] = obj[P_SHAPE]
            adds.append({"tmp": tmp, "props": props})
            self.pending_adds[tmp] = now

        out = []
        if edits:
            out.append({"type": "edit", "edits": edits})
        if adds:
            out.append({"type": "add", "items": adds})
        if deletes:
            out.append({"type": "delete", "ids": deletes})
        self.stats["sent"] += len(edits) + len(adds) + len(deletes)
        if out and self.on_change:
            self.on_change()
        return out

    # ------------------------------------------------------ incoming
    def handle_message(self, msg: dict) -> list:
        t = msg.get("type")
        if t == "changes":
            self._apply_changes(msg)
        elif t == "added":
            self._on_added(msg)
        elif t == "edited":
            self._on_edited(msg)
        if self.on_change:
            self.on_change()
        return []

    def _apply_changes(self, msg: dict) -> None:
        upserts = msg.get("upserts") or []
        for p in upserts:
            p["id"] = norm_id(p.get("id"))
        if msg.get("full"):
            ids = {norm_id(i) for i in msg.get("ids") or []}
            self.remote_ids = ids
            by_id, _ = self._index()
            self.missing_ids = {eid for eid in by_id if eid not in ids}
            for eid in list(self.records):
                if eid not in ids:
                    del self.records[eid]
            self.synced = True
        for eid in msg.get("deletes") or []:
            eid = norm_id(eid)
            self.remote_ids.discard(eid)
            self.records.pop(eid, None)
            obj = self.object_for(eid)
            if obj is not None:
                _remove_object(obj)
        if not upserts:
            return
        now = time.time()
        touched = []
        # Pass 1: create/update objects (parents may arrive after children).
        for props in upserts:
            eid = props["id"]
            self.remote_ids.add(eid)
            self.missing_ids.discard(eid)
            obj = self.object_for(eid)
            rec = self.records.get(eid)
            fields = remote_fields(props)
            if obj is None:
                obj = self._create_object(props)
                rec = None
            _store_registration(obj, props)
            if rec is None:
                rec = Record(eid, props.get("type", "Box"), obj.name)
                self.records[eid] = rec
                obj[P_TYPE] = rec.type
                changed = fields  # adopt everything
            else:
                changed = {k: v for k, v in fields.items() if not field_equal(k, v, rec.remote.get(k))}
                if "transform" in changed and now - rec.sent_at < AUTHORITY_WINDOW:
                    del changed["transform"]  # our own in-flight edit is newer
            rec.remote.update(changed)
            touched.append((rec, obj, changed))
            self.stats["received"] += 1
        # Pass 2: parents, then everything else.
        for rec, obj, changed in touched:
            if "parentID" in changed:
                self._apply_parent(obj, changed["parentID"])
        # Parents before children: setting a child's world matrix uses its
        # parent's current world matrix.
        touched.sort(key=lambda t: _depth(t[1]))
        for rec, obj, changed in touched:
            self._apply_fields(obj, rec.type, {k: v for k, v in changed.items() if k != "parentID"})
        bpy.context.view_layer.update()
        for rec, obj, changed in touched:
            if rec.type == "Model" and changed.get("modelURL"):
                self.models.maybe_load(obj, changed["modelURL"])
        # Read back what Blender now has, including children that moved with a parent.
        refresh = {rec.id for rec, _, _ in touched}
        for rec, obj, _ in touched:
            for child in obj.children_recursive:
                cid = child.get(P_ID)
                if cid:
                    refresh.add(norm_id(cid))
        for eid in refresh:
            rec = self.records.get(eid)
            obj = self.object_for(eid)
            if rec and obj is not None:
                rec.obj_name = obj.name
                rec.local = self.local_fields(obj, rec.type)

    def _on_added(self, msg: dict) -> None:
        for item in msg.get("items") or []:
            tmp = item.get("tmp")
            self.pending_adds.pop(tmp, None)
            obj = next((o for o in self.scene.objects if o.get(P_TMP) == tmp), None)
            if obj is None:
                continue
            if not item.get("id"):
                self.log(f"Overte refused to create an entity for '{obj.name}' (no rez permission?)")
                del obj[P_TMP]
                continue
            eid = norm_id(item["id"])
            props = item.get("props") or {}
            del obj[P_TMP]
            obj[P_ID] = eid
            _store_registration(obj, props)
            etype = props.get("type") or obj.get(P_TYPE, "Box")
            obj[P_TYPE] = etype
            rec = Record(eid, etype, obj.name)
            # Agree on what Overte stored; any later Blender change diffs against it.
            fields = remote_fields(props) if props else self.local_fields(obj, etype)
            rec.remote = dict(fields)
            rec.local = dict(fields)
            self.records[eid] = rec
            self.remote_ids.add(eid)

    def _on_edited(self, msg: dict) -> None:
        for r in msg.get("results") or []:
            if r.get("ok") or not r.get("props"):
                continue
            eid = norm_id(r["id"])
            rec = self.records.get(eid)
            obj = self.object_for(eid)
            if rec is None or obj is None:
                continue
            self.log(f"Overte rejected an edit to '{obj.name}' (locked or no permission); reverting")
            fields = remote_fields(r["props"])
            _store_registration(obj, r["props"])
            rec.remote.update(fields)
            if "parentID" in fields:
                self._apply_parent(obj, fields["parentID"])
            self._apply_fields(obj, rec.type, {k: v for k, v in fields.items() if k != "parentID"})
            bpy.context.view_layer.update()
            rec.local = self.local_fields(obj, rec.type)

    # ------------------------------------------------- applying to Blender
    def _collection(self) -> bpy.types.Collection:
        return overte_collection(self.scene)

    def _create_object(self, props: dict) -> bpy.types.Object:
        etype = props.get("type", "Box")
        name = props.get("name") or f"{etype}_{props['id'][1:9]}"
        if etype in PRIMITIVE_TYPES:
            shape = props.get("shape") or DEFAULT_SHAPE.get(etype, "Cube")
            obj = bpy.data.objects.new(name, unit_mesh(shape))
            obj[P_SHAPE] = shape
            obj.material_slots[0].link = "OBJECT"
        elif etype == "Light":
            light = bpy.data.lights.new(name, "SPOT" if props.get("isSpotlight") else "POINT")
            obj = bpy.data.objects.new(name, light)
        else:
            obj = bpy.data.objects.new(name, None)
            obj.empty_display_type = "CUBE"
            obj.empty_display_size = 0.5  # unit cube: scale == dimensions
        obj[P_ID] = props["id"]
        obj[P_TYPE] = etype
        self._collection().objects.link(obj)
        return obj

    def _apply_parent(self, obj, parent_id: str) -> None:
        mw = obj.matrix_world.copy()
        parent = self.object_for(parent_id) if parent_id != NULL_ID else None
        if parent is obj:
            parent = None
        if obj.parent is not parent:
            obj.parent = parent
            if parent is not None:
                obj.matrix_parent_inverse = Matrix.Identity(4)
        obj.matrix_world = mw

    def _apply_fields(self, obj, etype: str, fields: dict) -> None:
        if "name" in fields and fields["name"] and obj.name != fields["name"]:
            obj.name = fields["name"]
        if "visible" in fields:
            obj.hide_render = not fields["visible"]
        if "locked" in fields:
            v = bool(fields["locked"])
            obj.lock_location = (v, v, v)
            obj.lock_rotation = (v, v, v)
            obj.lock_scale = (v, v, v)
        if "transform" in fields:
            pos, rot, dims = fields["transform"]
            props = {"position": cv.vec_json(pos), "rotation": cv.quat_json(rot), "dimensions": cv.vec_json(dims)}
            base, center = self._base_center(obj)
            loc, q, scale = cv.entity_to_object(props, base, center, etype)
            obj.matrix_world = Matrix.LocRotScale(Vector(loc), Quaternion(q), Vector(scale))
        if "color" in fields:
            c = fields["color"]
            rgb = cv.json_to_color({"red": c[0], "green": c[1], "blue": c[2]})
            if obj.type == "LIGHT":
                obj.data.color = rgb
            elif obj.type == "MESH":
                _set_object_color(obj, rgb)
        if obj.type == "LIGHT":
            light = obj.data
            if "isSpotlight" in fields:
                light.type = "SPOT" if fields["isSpotlight"] else "POINT"
            if "intensity" in fields:
                light.energy = fields["intensity"] * LIGHT_WATTS_PER_INTENSITY
            if "falloffRadius" in fields:
                light.shadow_soft_size = fields["falloffRadius"]
            if light.type == "SPOT":
                if "cutoff" in fields:
                    light.spot_size = min(math.radians(fields["cutoff"] * 2.0), math.pi)
                if "exponent" in fields:
                    e = max(fields["exponent"], 0.0)
                    light.spot_blend = min(e / (1.0 + e), 1.0)
        if "modelURL" in fields:
            obj[P_URL] = fields["modelURL"]
        if "text" in fields:
            obj[P_TEXT] = fields["text"]

    def replace_object_data(self, old, data, base, center) -> bpy.types.Object:
        """Swap a linked object for a new object with ``data`` (e.g. a loaded model
        mesh replacing a cube empty), keeping the link, hierarchy and placement."""
        eid = norm_id(old.get(P_ID))
        rec = self.records.get(eid)
        name = old.name
        new = bpy.data.objects.new(name + ".overte_new", data)
        for k, v in old.items():
            new[k] = v
        new[P_BASE] = tuple(base)
        new[P_CENTER] = tuple(center)
        for col in old.users_collection:
            col.objects.link(new)
        new.hide_render = old.hide_render
        new.lock_location, new.lock_rotation, new.lock_scale = old.lock_location, old.lock_rotation, old.lock_scale
        children = [(c, c.matrix_world.copy()) for c in old.children]
        new.parent = old.parent
        new.matrix_parent_inverse = Matrix.Identity(4)
        etype = rec.type if rec else old.get(P_TYPE, "Model")
        transform = rec.remote.get("transform") if rec else None
        if transform is None:
            transform = self.local_fields(old, etype)["transform"]
        was_active = bpy.context.view_layer.objects.active == old
        was_selected = old.select_get()
        bpy.data.objects.remove(old, do_unlink=True)
        new.name = name
        self._apply_fields(new, etype, {"transform": transform})
        bpy.context.view_layer.update()
        for child, mw in children:
            child.parent = new
            child.matrix_parent_inverse = Matrix.Identity(4)
            child.matrix_world = mw
        bpy.context.view_layer.update()
        try:
            new.select_set(was_selected)
            if was_active:
                bpy.context.view_layer.objects.active = new
        except RuntimeError:
            pass  # not in the active view layer
        if rec:
            rec.obj_name = new.name
            rec.local = self.local_fields(new, etype)
        return new

    # --------------------------------------------------- user actions
    def unlink(self, obj) -> None:
        eid = obj.get(P_ID)
        if eid:
            self.records.pop(norm_id(eid), None)
            self.missing_ids.discard(norm_id(eid))
        for k in (P_ID, P_TMP):
            if k in obj:
                del obj[k]

    def recreate_missing(self) -> int:
        """Re-add entities for objects whose entity no longer exists in the domain."""
        n = 0
        by_id, _ = self._index()
        for eid in list(self.missing_ids):
            obj = by_id.get(eid)
            self.missing_ids.discard(eid)
            if obj is None:
                continue
            del obj[P_ID]
            obj[P_TMP] = str(uuid.uuid4())
            n += 1
        return n


# ------------------------------------------------------------- helpers
def new_tmp_id() -> str:
    return str(uuid.uuid4())


def _store_registration(obj, props: dict) -> None:
    if "registrationPoint" not in props:
        return
    reg = _vec(props.get("registrationPoint"), DEFAULT_REG)
    if cv.close(reg, DEFAULT_REG, 1e-6):
        if P_REG in obj:
            del obj[P_REG]
    else:
        obj[P_REG] = reg


def overte_collection(scene) -> bpy.types.Collection:
    """The "Overte" collection entities from the domain are placed in."""
    col = bpy.data.collections.get(COLLECTION)
    if col is None:
        col = bpy.data.collections.new(COLLECTION)
    if scene.collection.children.get(col.name) is None:
        scene.collection.children.link(col)
    return col


def _depth(obj) -> int:
    d = 0
    while obj.parent is not None:
        obj = obj.parent
        d += 1
    return d


def _principled(mat):
    try:
        nt = mat.node_tree
    except AttributeError:
        return None
    if nt is None:
        return None
    for node in nt.nodes:
        if node.type == "BSDF_PRINCIPLED":
            return node
    return None


def _set_object_color(obj, rgb) -> None:
    if not obj.material_slots:
        obj.data.materials.append(None)
    slot = obj.material_slots[0]
    slot.link = "OBJECT"
    mat = slot.material
    if mat is None:
        mat = bpy.data.materials.new(f"Overte {obj.name}")
        if bpy.app.version < (5, 0, 0):
            mat.use_nodes = True  # always on (and deprecated) from Blender 5
        slot.material = mat
    mat.diffuse_color = (rgb[0], rgb[1], rgb[2], 1.0)
    node = _principled(mat)
    if node is not None:
        node.inputs["Base Color"].default_value = (rgb[0], rgb[1], rgb[2], 1.0)


def _remove_object(obj) -> None:
    data = obj.data
    mats = [s.material for s in obj.material_slots if s.link == "OBJECT" and s.material]
    bpy.data.objects.remove(obj, do_unlink=True)
    for m in mats:
        if m.users == 0:
            bpy.data.materials.remove(m)
    if isinstance(data, bpy.types.Light) and data.users == 0:
        bpy.data.lights.remove(data)


def unit_mesh(shape: str) -> bpy.types.Mesh:
    """Shared unit-size (1 x 1 x 1, centred) mesh for an Overte shape."""
    kind = {
        "Cube": "Cube", "Sphere": "Sphere", "Cylinder": "Cylinder", "Cone": "Cone",
        "Quad": "Quad", "Circle": "Circle", "Triangle": "Triangle", "Hexagon": "Hexagon",
        "Octagon": "Octagon", "Tetrahedron": "Sphere", "Octahedron": "Sphere",
        "Dodecahedron": "Sphere", "Icosahedron": "Sphere",
    }.get(shape, "Cube")
    name = MESH_PREFIX + kind
    mesh = bpy.data.meshes.get(name)
    if mesh is not None:
        return mesh
    bm = bmesh.new()
    if kind == "Cube":
        bmesh.ops.create_cube(bm, size=1.0)
    elif kind == "Sphere":
        bmesh.ops.create_uvsphere(bm, u_segments=32, v_segments=16, radius=0.5)
    elif kind in ("Cylinder", "Cone", "Triangle", "Hexagon", "Octagon"):
        segs = {"Cylinder": 32, "Cone": 32, "Triangle": 3, "Hexagon": 6, "Octagon": 8}[kind]
        bmesh.ops.create_cone(bm, cap_ends=True, segments=segs, radius1=0.5,
                              radius2=0.0 if kind == "Cone" else 0.5, depth=1.0)
    elif kind == "Quad":
        bmesh.ops.create_grid(bm, x_segments=1, y_segments=1, size=0.5)
    elif kind == "Circle":
        bmesh.ops.create_circle(bm, cap_ends=True, segments=32, radius=0.5)
    mesh = bpy.data.meshes.new(name)
    bm.to_mesh(mesh)
    bm.free()
    mesh.materials.append(None)  # one slot, linked per object for colours
    mesh.use_fake_user = True
    return mesh
